from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from test_api_first_modeling import _EmbeddingGateway, _StaticIntrospector

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogError, CatalogStore
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.contracts import RevisionState


def _application(schema_snapshot):
    catalog = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    catalog.create_schema()
    return AnalyticsApplication(
        catalog=catalog,
        introspector=_StaticIntrospector(schema_snapshot),
        executor=object(),
        embedding_gateway=_EmbeddingGateway(),
    )


def _revision(application):
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    return application.create_empty_revision(project_id="sales", schema_snapshot_id=snapshot.id)


def test_derive_candidate_keeps_the_source_revision_untouched(schema_snapshot) -> None:
    """已发布/已冻结版本不可改，用户必须能基于它开新草稿继续工作。

    此前 UI 上没有出路：验证门禁只说"published 不能重新校验"，没有按钮。
    """

    application = _application(schema_snapshot)
    source = _revision(application)

    derived = application.derive_candidate_revision(revision_id=source.id)

    assert derived.id != source.id
    assert derived.parent_revision_id == source.id
    assert derived.state is RevisionState.DRAFT
    assert derived.etag == 1
    # 完整继承语义模型，不是空白草稿。spec_hash 含 revision_id，必然不同，
    # 因此比对内容本身。
    assert derived.semantic_spec.models == source.semantic_spec.models
    assert derived.semantic_spec.fields == source.semantic_spec.fields
    assert derived.semantic_spec.metrics == source.semantic_spec.metrics
    assert derived.semantic_spec.datasets == source.semantic_spec.datasets
    assert derived.schema_snapshot_hash == source.schema_snapshot_hash
    # 来源版本原样保留
    assert application.get_revision(source.id).etag == source.etag


def test_derived_revision_is_persisted_and_retrievable(schema_snapshot) -> None:
    application = _application(schema_snapshot)
    source = _revision(application)
    derived = application.derive_candidate_revision(revision_id=source.id)
    assert application.get_revision(derived.id).id == derived.id


def test_derive_rejects_an_unknown_revision(schema_snapshot) -> None:
    application = _application(schema_snapshot)
    _revision(application)
    with pytest.raises((CatalogError, SemanticValidationError)):
        application.derive_candidate_revision(revision_id="rev_missing")
