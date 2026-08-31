from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.release import ReleasePublisher
from knowflow_analytics.catalog.store import CatalogError, CatalogStore
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.domain import DomainLifecycle
from knowflow_analytics.modeling.revision import RevisionConflictError, RevisionEditor
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


class _FakeEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="fake-embedding-v1",
            dimension=2,
            vectors=tuple((float(index), 1.0) for index, _ in enumerate(texts)),
        )


def test_publish_atomically_binds_release_and_index(schema_snapshot, sales_catalog):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    catalog.save_schema_snapshot(project_id="sales", snapshot=schema_snapshot)
    editor = RevisionEditor()
    revision = editor.create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_catalog=sales_catalog,
    )
    validated = editor.validate_for_publish(revision)
    catalog.save_revision(validated)

    published = ReleasePublisher(
        catalog=catalog,
        revision_editor=editor,
        index_builder=SemanticIndexBuilder(_FakeEmbeddingGateway()),
        require_evaluation=False,
        require_quality_report=False,
    ).publish(validated)
    active = catalog.get_active_release("sales")

    assert active.release.id == published.release.id
    assert active.release.index_snapshot_id == active.index_snapshot.id
    assert active.release.spec_hash == active.index_snapshot.release_spec_hash
    assert active.index_snapshot.embedding_model_id == "fake-embedding-v1"

    governance = catalog.get_domain_governance("sales")
    assert governance.lifecycle.value == "online"
    offline = catalog.update_domain_governance(
        project_id="sales",
        expected_etag=governance.etag,
        classifications=("销售",),
        lifecycle=DomainLifecycle.OFFLINE,
        updated_by="owner",
    )
    with pytest.raises(CatalogError) as offline_error:
        catalog.get_active_release("sales")
    assert offline_error.value.code == "DOMAIN_OFFLINE"
    with pytest.raises(CatalogError) as transition_error:
        catalog.update_domain_governance(
            project_id="sales",
            expected_etag=offline.etag,
            classifications=("销售",),
            lifecycle=DomainLifecycle.INITIALIZED,
            updated_by="owner",
        )
    assert transition_error.value.code == "DOMAIN_LIFECYCLE_INVALID"

    with pytest.raises(RevisionConflictError, match="published revisions are immutable"):
        ReleasePublisher(
            catalog=catalog,
            revision_editor=editor,
            index_builder=SemanticIndexBuilder(_FakeEmbeddingGateway()),
            require_evaluation=False,
            require_quality_report=False,
        ).publish(catalog.get_revision(validated.id))


def test_evaluated_index_snapshot_can_be_persisted_idempotently(sales_release):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    snapshot = SemanticIndexBuilder(_FakeEmbeddingGateway()).build(sales_release)

    catalog.save_index_snapshot(project_id="sales", index_snapshot=snapshot)
    catalog.save_index_snapshot(project_id="sales", index_snapshot=snapshot)

    assert catalog.get_index_snapshot(snapshot.id, project_id="sales") == snapshot


def test_a_rebuilt_index_with_drifted_vectors_reuses_its_stored_snapshot(sales_release):
    """Remote embeddings are not bit-reproducible; that must not be a collision.

    The snapshot id is derived from the indexed semantics plus the embedding
    model, so a rebuild whose floats differ in their last digits addresses the
    same row.  Raising INDEX_SNAPSHOT_CONFLICT here would make every rebuild of
    an unchanged release fail instead of reusing the index behind it.
    """

    class _DriftingEmbeddingGateway:
        def __init__(self) -> None:
            self.calls = 0

        def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
            self.calls += 1
            drift = self.calls * 1e-9
            return EmbeddingBatch(
                model_id="fake",
                dimension=1,
                vectors=tuple((1.0 + drift,) for _ in texts),
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    builder = SemanticIndexBuilder(_DriftingEmbeddingGateway())
    first = builder.build(sales_release)
    second = builder.build(sales_release)
    assert first.vectors != second.vectors
    assert first.id == second.id

    catalog.save_index_snapshot(project_id="sales", index_snapshot=first)
    catalog.save_index_snapshot(project_id="sales", index_snapshot=second)

    assert catalog.get_index_snapshot(first.id, project_id="sales") == first


def test_projection_only_revision_cannot_be_persisted(sales_release):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    revision = ModelingRevision(
        id="projection-only",
        project_id="sales",
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_spec=sales_release,
    )

    with pytest.raises(CatalogError) as exc_info:
        catalog.save_revision(revision)

    assert exc_info.value.code == "MODELING_CATALOG_REQUIRED"


def test_schema_snapshot_is_idempotent_across_capture_times(schema_snapshot):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")

    catalog.save_schema_snapshot(project_id="sales", snapshot=schema_snapshot)
    catalog.save_schema_snapshot(
        project_id="sales",
        snapshot=schema_snapshot.model_copy(
            update={"captured_at": schema_snapshot.captured_at.replace(year=2027)}
        ),
    )

    assert catalog.get_schema_snapshot(schema_snapshot.id, project_id="sales") == schema_snapshot


def test_identical_schema_snapshot_can_be_owned_by_multiple_projects(schema_snapshot):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    catalog.create_project(project_id="finance", name="财务分析")

    catalog.save_schema_snapshot(project_id="sales", snapshot=schema_snapshot)
    catalog.save_schema_snapshot(project_id="finance", snapshot=schema_snapshot)

    assert catalog.get_schema_snapshot(schema_snapshot.id, project_id="sales") == schema_snapshot
    assert catalog.get_schema_snapshot(schema_snapshot.id, project_id="finance") == schema_snapshot


def test_schema_snapshot_rejects_identifier_collision(schema_snapshot):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售分析")
    catalog.save_schema_snapshot(project_id="sales", snapshot=schema_snapshot)
    changed_table = schema_snapshot.tables[0].model_copy(update={"name": "other"})
    collided = schema_snapshot.model_copy(
        update={"tables": (changed_table, *schema_snapshot.tables[1:])}
    )

    with pytest.raises(CatalogError) as exc_info:
        catalog.save_schema_snapshot(project_id="sales", snapshot=collided)

    assert exc_info.value.code == "SNAPSHOT_CONFLICT"
