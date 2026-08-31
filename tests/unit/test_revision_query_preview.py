from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import (
    CatalogError,
    CatalogStore,
    query_diagnostics,
)
from knowflow_analytics.contracts import (
    Aggregation,
    QueryAggregationOverride,
    QueryResult,
    SemanticQuery,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.query.contracts import QueryRequest, QueryState
from knowflow_analytics.semantic.index import EmbeddingBatch


class _EmbeddingGateway:
    def for_tenant(self, _tenant_id: str):
        return self

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = tuple(self._vector(text) for text in texts)
        return EmbeddingBatch(model_id="preview-test", dimension=8, vectors=vectors)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        values = [0.0] * 8
        for index, character in enumerate(text):
            values[(ord(character) + index) % len(values)] += 1.0
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return tuple(value / norm for value in values)


class _ChangingEmbeddingGateway(_EmbeddingGateway):
    """Simulate a real embedding service whose floats vary between requests."""

    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.calls += 1
        batch = super().encode(texts)
        offset = self.calls * 1e-9
        return batch.model_copy(
            update={
                "vectors": tuple(
                    tuple(value + offset for value in vector) for vector in batch.vectors
                )
            }
        )


class _FailIfUsedEmbeddingGateway:
    def encode(self, _texts: tuple[str, ...]) -> EmbeddingBatch:
        raise AssertionError("structured preview must not call the embedding gateway")


class _TenantCapturingEmbeddingGateway(_EmbeddingGateway):
    def __init__(
        self,
        tenant_id: str = "",
        tenants: list[str] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.tenants = tenants if tenants is not None else []

    def for_tenant(self, tenant_id: str):
        return type(self)(tenant_id=tenant_id, tenants=self.tenants)

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.tenants.append(self.tenant_id)
        return super().encode(texts)


class _Executor:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, *, query, release):
        self.calls.append((query, release))
        return QueryResult(
            columns=("区域", "净收入"),
            rows=(("华东", 300),),
            row_count=1,
        )


def _application_with_revision(
    sales_catalog: SemanticCatalog,
    *,
    state=RevisionState.VALIDATED,
    embedding_gateway=None,
    **application_kwargs,
):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售")
    semantic_catalog = sales_catalog.model_copy(update={"revision_id": "revision-preview"})
    revision = ModelingRevision(
        id="revision-preview",
        project_id="sales",
        schema_snapshot_hash="sha256:schema",
        etag=7,
        state=state,
        semantic_catalog=semantic_catalog,
        semantic_spec=compile_semantic_catalog(semantic_catalog),
    )
    catalog.save_revision(revision)
    executor = _Executor()
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=object(),
        executor=executor,
        embedding_gateway=embedding_gateway or _EmbeddingGateway(),
        **application_kwargs,
    )
    return application, executor, engine


def test_validated_revision_preview_runs_the_normal_query_pipeline(sales_catalog):
    application, executor, engine = _application_with_revision(sales_catalog)
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            now=datetime(2026, 8, 15, tzinfo=UTC),
        )

        assert response.state is QueryState.COMPLETED
        assert response.release_id == "staged:revision-preview"
        assert response.semantic_query.metric_ids == ("net_revenue",)
        assert response.semantic_query.dimension_ids == ("region",)
        assert response.data.rows == (("华东", 300),)
        assert response.physical_sql is None
        assert len(executor.calls) == 1
        with pytest.raises(CatalogError) as raised:
            application.catalog.get_index_snapshot(
                response.index_snapshot_id,
                project_id="sales",
            )
        assert raised.value.code == "INDEX_SNAPSHOT_NOT_FOUND"
    finally:
        application.close()
        engine.dispose()


def test_revision_preview_persists_actor_bound_diagnostic_and_marks_draft_drift(
    sales_catalog,
):
    application, _executor, engine = _application_with_revision(sales_catalog)
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )

        application.export_query_diagnostic(
            project_id="sales",
            query_id=response.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        artifact = application.catalog.get_query_diagnostic(
            actor_id="reviewer",
            project_id="sales",
            permission_scope_hash="scope-reviewer-v1",
            query_id=response.query_id,
        )
        assert artifact.revision_id == "revision-preview"
        assert artifact.revision_etag == 7
        assert artifact.mode == "natural"

        revision = application.catalog.get_revision("revision-preview")
        current_metrics = tuple(
            metric.model_copy(
                update={"aggregation": Aggregation.AVG, "name": "CURRENT_DRAFT_POISON"}
            )
            if metric.id == "net_revenue"
            else metric
            for metric in revision.semantic_spec.metrics
        )
        application.catalog.update_revision(
            revision.model_copy(
                update={
                    "etag": 8,
                    "semantic_spec": revision.semantic_spec.model_copy(
                        update={"metrics": current_metrics}
                    ),
                }
            ),
            previous_etag=7,
        )
        exported = application.export_query_diagnostic(
            project_id="sales",
            query_id=response.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        assert exported.summary["version_status"] == "VERSION_STALE"
        assert exported.summary["aggregation_comparison"][0]["catalog_aggregation"] == "sum"
        assert "CURRENT_DRAFT_POISON" not in exported.model_dump_json()
        assert "VERSION_STALE" in exported.markdown
    finally:
        application.close()
        engine.dispose()


def test_diagnostic_write_failure_never_changes_the_preview_response(
    sales_catalog,
    monkeypatch,
    caplog,
):
    application, executor, engine = _application_with_revision(sales_catalog)

    def fail_to_save(_artifact):
        raise RuntimeError("diagnostic database unavailable")

    monkeypatch.setattr(application.catalog, "save_query_diagnostic", fail_to_save)
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )

        assert response.state is QueryState.COMPLETED
        assert response.data.rows == (("华东", 300),)
        assert len(executor.calls) == 1
        with pytest.raises(CatalogError):
            application.export_query_diagnostic(
                project_id="sales",
                query_id=response.query_id,
                actor_id="reviewer",
                permission_scope_hash="scope-reviewer-v1",
            )
        assert "query diagnostic persistence failed" in caplog.text
    finally:
        application.close()
        engine.dispose()


def test_query_response_does_not_wait_for_slow_diagnostic_store(sales_catalog, monkeypatch):
    application, _executor, engine = _application_with_revision(sales_catalog)
    entered = threading.Event()
    release = threading.Event()
    original_save = application.catalog.save_query_diagnostic

    def slow_save(artifact):
        entered.set()
        assert release.wait(timeout=2)
        original_save(artifact)

    monkeypatch.setattr(application.catalog, "save_query_diagnostic", slow_save)
    try:
        started = time.monotonic()
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        elapsed = time.monotonic() - started

        assert response.state is QueryState.COMPLETED
        assert elapsed < 0.2
        assert entered.wait(timeout=1)
        release.set()
        exported = application.export_query_diagnostic(
            project_id="sales",
            query_id=response.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        assert exported.summary["query_id"] == response.query_id
    finally:
        release.set()
        application.close()
        engine.dispose()


def test_full_diagnostic_queue_drops_new_record_without_blocking_query(
    sales_catalog,
    monkeypatch,
    caplog,
):
    application, _executor, engine = _application_with_revision(
        sales_catalog,
        query_diagnostic_queue_size=1,
    )
    entered = threading.Event()
    release = threading.Event()
    original_save = application.catalog.save_query_diagnostic
    save_calls = 0

    def block_first_save(artifact):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            entered.set()
            assert release.wait(timeout=2)
        original_save(artifact)

    monkeypatch.setattr(application.catalog, "save_query_diagnostic", block_first_save)

    def preview():
        return application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )

    try:
        first = preview()
        assert entered.wait(timeout=1)
        second = preview()
        started = time.monotonic()
        dropped = preview()
        assert time.monotonic() - started < 0.2
        assert "query diagnostic queue full" in caplog.text

        release.set()
        application.export_query_diagnostic(
            project_id="sales",
            query_id=first.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        application.export_query_diagnostic(
            project_id="sales",
            query_id=second.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        with pytest.raises(CatalogError) as raised:
            application.export_query_diagnostic(
                project_id="sales",
                query_id=dropped.query_id,
                actor_id="reviewer",
                permission_scope_hash="scope-reviewer-v1",
            )
        assert raised.value.code == "QUERY_DIAGNOSTIC_NOT_FOUND"
    finally:
        release.set()
        application.close()
        engine.dispose()


def test_same_query_id_generations_each_record_and_export_waits_for_the_latest(
    sales_catalog,
    monkeypatch,
):
    application, _executor, engine = _application_with_revision(
        sales_catalog,
        query_diagnostic_queue_size=4,
        query_diagnostic_export_wait_seconds=0.5,
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    original_save = application.catalog.save_query_diagnostic
    save_calls = 0

    def staged_save(artifact):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        elif save_calls == 2:
            second_entered.set()
            time.sleep(0.15)
        original_save(artifact)

    monkeypatch.setattr(application.catalog, "save_query_diagnostic", staged_save)

    def preview(question: str):
        return application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question=question,
                query_id="same-query-generation",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )

    try:
        first = preview("各区域净收入 Top 1")
        assert first_entered.wait(timeout=1)
        second = preview("各区域净收入 Top 2")
        assert first.query_id == second.query_id == "same-query-generation"
        release_first.set()
        assert second_entered.wait(timeout=1)

        started = time.monotonic()
        exported = application.export_query_diagnostic(
            project_id="sales",
            query_id=second.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        assert time.monotonic() - started >= 0.1
        assert exported.summary["question"] == "各区域净收入 Top 2"
        assert save_calls == 2
    finally:
        release_first.set()
        application.close()
        engine.dispose()


def test_idle_diagnostic_recorder_physically_purges_expired_rows_without_new_queries(
    sales_catalog,
):
    application, executor, engine = _application_with_revision(
        sales_catalog,
        query_diagnostic_ttl_seconds=1,
        query_diagnostic_purge_interval_seconds=60,
    )
    restarted = None
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入 Top 1",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        application.export_query_diagnostic(
            project_id="sales",
            query_id=response.query_id,
            actor_id="reviewer",
            permission_scope_hash="scope-reviewer-v1",
        )
        application.close()
        time.sleep(1.05)
        restarted = AnalyticsApplication(
            catalog=application.catalog,
            introspector=object(),
            executor=executor,
            embedding_gateway=_EmbeddingGateway(),
            query_diagnostic_purge_interval_seconds=0.05,
        )

        deadline = time.monotonic() + 2
        remaining = 1
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            with engine.connect() as connection:
                remaining = connection.execute(
                    select(func.count()).select_from(query_diagnostics)
                ).scalar_one()
        assert remaining == 0
    finally:
        if restarted is not None:
            restarted.close()
        application.close()
        engine.dispose()


def test_structured_revision_preview_bypasses_semantic_index_and_embedding(
    sales_catalog,
):
    application, executor, engine = _application_with_revision(
        sales_catalog,
        embedding_gateway=_FailIfUsedEmbeddingGateway(),
    )
    try:
        response = application.preview_revision_structured_query(
            revision_id="revision-preview",
            project_id="sales",
            semantic_query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("net_revenue",),
                aggregation_overrides=(
                    QueryAggregationOverride(
                        metric_id="net_revenue",
                        aggregation=Aggregation.SUM,
                    ),
                ),
                dimension_ids=("region",),
                limit=10,
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
        )

        assert response.state is QueryState.COMPLETED
        assert response.release_id == "staged:revision-preview"
        assert response.index_snapshot_id is None
        assert len(executor.calls) == 1
    finally:
        engine.dispose()


def test_natural_revision_preview_builds_index_as_the_signed_in_tenant(
    sales_catalog,
):
    embedding_gateway = _TenantCapturingEmbeddingGateway()
    application, _executor, engine = _application_with_revision(
        sales_catalog,
        embedding_gateway=embedding_gateway,
    )
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
            actor_id="tenant-reviewer",
        )

        assert response.state is QueryState.COMPLETED
        assert len(embedding_gateway.tenants) >= 2
        assert set(embedding_gateway.tenants) == {"tenant-reviewer"}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("etag", "snapshot_hash"),
    ((8, "sha256:schema"), (7, "sha256:changed")),
)
def test_revision_preview_rejects_a_stale_model_version(
    sales_catalog,
    etag,
    snapshot_hash,
):
    application, executor, engine = _application_with_revision(sales_catalog)
    try:
        with pytest.raises(SemanticValidationError, match="changed"):
            application.preview_revision_query(
                revision_id="revision-preview",
                request=QueryRequest(
                    project_id="sales",
                    question="净收入",
                    dataset_ids=("sales_dataset",),
                ),
                expected_etag=etag,
                expected_schema_snapshot_hash=snapshot_hash,
            )
        assert executor.calls == []
    finally:
        engine.dispose()


def test_draft_revision_cannot_be_previewed_before_structural_validation(sales_catalog):
    application, executor, engine = _application_with_revision(
        sales_catalog,
        state=RevisionState.DRAFT,
    )
    try:
        with pytest.raises(SemanticValidationError) as raised:
            application.preview_revision_query(
                revision_id="revision-preview",
                request=QueryRequest(
                    project_id="sales",
                    question="净收入",
                    dataset_ids=("sales_dataset",),
                ),
                expected_etag=7,
                expected_schema_snapshot_hash="sha256:schema",
            )
        assert raised.value.code == "NOT_VALIDATED"
        assert executor.calls == []
    finally:
        engine.dispose()


def test_internal_preview_api_does_not_accept_browser_sql(sales_catalog):
    application, executor, engine = _application_with_revision(sales_catalog)
    client = TestClient(
        create_api(
            application=application,
            service_secret="s" * 32,
            allow_debug_sql=True,
        ),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "reviewer",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "preview-scope-v1",
    }
    try:
        response = client.post(
            "/v1/analytics/projects/sales/revisions/revision-preview/query-preview",
            headers=headers,
            json={
                "expected_etag": 7,
                "schema_snapshot_hash": "sha256:schema",
                "question": "各区域净收入 Top 1",
                "dataset_ids": ["sales_dataset"],
                "fixed_now": "2026-08-15T00:00:00+00:00",
                "include_diagnostics": True,
                "include_debug_sql": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "COMPLETED"
        assert response.json()["physical_sql"]

        rejected = client.post(
            "/v1/analytics/projects/sales/revisions/revision-preview/query-preview",
            headers=headers,
            json={
                "expected_etag": 7,
                "schema_snapshot_hash": "sha256:schema",
                "question": "净收入",
                "dataset_ids": ["sales_dataset"],
                "physical_sql": "DROP TABLE orders",
            },
        )
        assert rejected.status_code == 422
        assert len(executor.calls) == 1
    finally:
        engine.dispose()


def test_revision_preview_asks_when_two_dimensions_share_a_name(sales_catalog):
    duplicate_region = next(
        item for item in sales_catalog.dimensions if item.id == "product"
    ).model_copy(update={"name": "区域", "alias": None})
    ambiguous_catalog = sales_catalog.model_copy(
        update={
            "dimensions": tuple(
                duplicate_region if item.id == "product" else item
                for item in sales_catalog.dimensions
            )
        }
    )
    embedding_gateway = _ChangingEmbeddingGateway()
    application, executor, engine = _application_with_revision(
        ambiguous_catalog,
        embedding_gateway=embedding_gateway,
    )
    try:
        response = application.preview_revision_query(
            revision_id="revision-preview",
            request=QueryRequest(
                project_id="sales",
                question="各区域净收入",
                dataset_ids=("sales_dataset",),
            ),
            expected_etag=7,
            expected_schema_snapshot_hash="sha256:schema",
        )

        # product 被改名成「区域」，与真正的 region 撞名；"各区域净收入"按哪个分组
        # 不能靠排序猜。
        assert response.state is QueryState.CLARIFICATION_REQUIRED
        assert response.index_snapshot_id
        assert {(option.element_type, option.element_id) for option in response.options} == {
            ("dimension", "region"),
            ("dimension", "product"),
        }
        assert executor.calls == []
    finally:
        engine.dispose()
