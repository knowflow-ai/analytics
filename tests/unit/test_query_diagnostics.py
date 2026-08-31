from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from knowflow_analytics.api import create_api
from knowflow_analytics.application import (
    _diagnostic_index_snapshot_projection,
    _diagnostic_published_release_projection,
)
from knowflow_analytics.catalog.store import (
    CatalogError,
    CatalogStore,
    PublishedRelease,
    _query_diagnostic_advisory_lock_id,
    query_diagnostics,
)
from knowflow_analytics.contracts import QueryResult, SemanticQuery
from knowflow_analytics.query.contracts import (
    CompletedQueryResponse,
    FailedQueryResponse,
    QueryDiagnosis,
    QueryDiagnosticCategory,
    QueryError,
    QueryInterpretation,
    QueryRequest,
    QueryStage,
    QueryTraceStep,
    StructuredQueryRequest,
)
from knowflow_analytics.query.diagnostics import (
    QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT,
    QUERY_DIAGNOSTIC_MAX_TTL_SECONDS,
    QUERY_DIAGNOSTIC_TIMELINE_KEYS,
    QueryDiagnosticExport,
    build_query_diagnostic_artifact,
    render_query_diagnostic_export,
)
from knowflow_analytics.semantic.index import (
    IndexState,
    SemanticElementType,
    SemanticIndexEntry,
    SemanticIndexSnapshot,
)

_CREATED_AT = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
_SECRET = "d" * 32
_HEADERS = {
    "X-KnowFlow-Service-Token": _SECRET,
    "X-KnowFlow-Actor-Id": "actor-1",
    "X-KnowFlow-Project-Id": "sales",
    "X-KnowFlow-Permission-Scope-Hash": "scope-hash",
}
_FAKE_PASSWORD = "hunter" + "2"
_FAKE_DSN_PASSWORD = "secret-" + "password"


def _completed_response(*, physical_sql: str | None = None) -> CompletedQueryResponse:
    return CompletedQueryResponse(
        query_id="q_diag",
        release_id="rel_sales",
        spec_hash="sha256:spec",
        index_snapshot_id="idx_sales",
        trace=(
            QueryTraceStep(
                stage=QueryStage.PRECHECK,
                status="completed",
                detail={
                    "release_id": "rel_sales",
                    "Authorization": "Bearer should-never-survive",
                    "database_url": "postgresql://admin:unsafe@db.internal/sales",
                },
            ),
            QueryTraceStep(
                stage=QueryStage.CANDIDATE_DISCOVERY,
                status="completed",
                detail={"candidate_id": "opaque-candidate-reference"},
            ),
            QueryTraceStep(stage=QueryStage.FINAL_PARSING, status="completed"),
            QueryTraceStep(stage=QueryStage.S2SQL_CORRECTING, status="completed"),
            QueryTraceStep(stage=QueryStage.ROUTE_BINDING, status="completed"),
            QueryTraceStep(stage=QueryStage.TRANSLATING, status="completed"),
            QueryTraceStep(stage=QueryStage.PHYSICAL_SQL_CORRECTING, status="completed"),
            QueryTraceStep(
                stage=QueryStage.PHYSICAL_SQL_VALIDATING,
                status="completed",
                detail={"guard": "executor_preflight"},
            ),
            QueryTraceStep(
                stage=QueryStage.PHYSICAL_SQL_VALIDATING,
                status="completed",
                detail={"guard": "database_dry_run"},
            ),
            QueryTraceStep(stage=QueryStage.EXECUTING, status="completed"),
            QueryTraceStep(stage=QueryStage.POST_PROCESSING, status="completed"),
            QueryTraceStep(stage=QueryStage.FINISHED, status="completed"),
        ),
        diagnostics=QueryDiagnosis(
            category=QueryDiagnosticCategory.SUCCESS,
            stage=QueryStage.FINISHED.value,
            severity="info",
            summary="查询链路完成",
        ),
        interpretation=QueryInterpretation(
            dataset_id="sales_scope",
            metrics=("净收入",),
            dimensions=("区域",),
            filters=(),
        ),
        data=QueryResult(
            columns=("区域", "净收入"),
            rows=(("华东", 300), ("华南", 200), ("华北", 100)),
            row_count=3,
        ),
        visualization={"type": "bar"},
        semantic_query=SemanticQuery(
            dataset_id="sales_scope",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
        ),
        parsed_s2sql="SELECT 区域, 净收入 FROM 销售",
        corrected_s2sql="SELECT 区域, 净收入 FROM 销售",
        physical_sql=physical_sql,
    )


def _artifact(*, physical_sql: str | None = None, ttl_seconds: int = 3600):
    return build_query_diagnostic_artifact(
        request=QueryRequest(
            project_id="sales",
            question=(
                f"各区域净收入 password={_FAKE_PASSWORD} "
                f"postgresql://reader:{_FAKE_DSN_PASSWORD}@db.internal/sales"
            ),
            dataset_ids=("sales_scope",),
        ),
        response=_completed_response(physical_sql=physical_sql),
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="natural",
        revision_id="rev_sales",
        revision_etag=7,
        revision_schema_snapshot_hash="sha256:schema",
        revision_semantic_spec_hash="sha256:spec",
        created_at=_CREATED_AT,
        ttl_seconds=ttl_seconds,
        max_result_rows=2,
    )


def test_artifact_is_permanently_redacted_and_result_sample_is_bounded() -> None:
    artifact = _artifact(physical_sql="SELECT region, SUM(net_amount) FROM sales.orders")
    serialized = artifact.model_dump_json()

    assert artifact.response["data"]["rows"] == [["华东", 300], ["华南", 200]]
    assert artifact.response["data"]["sample_truncated"] is True
    assert artifact.response["physical_sql"].startswith("SELECT region")
    assert _FAKE_PASSWORD not in serialized
    assert _FAKE_DSN_PASSWORD not in serialized
    assert "db.internal" not in serialized
    assert "postgresql://" not in serialized
    assert "should-never-survive" not in serialized
    assert "opaque-candidate-reference" not in serialized
    assert serialized.count("[REDACTED]") >= 4


def test_artifact_redacts_common_bare_tokens_private_keys_and_sensitive_result_columns() -> None:
    jwt = ".".join(
        ("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "signatureSensitive123")
    )
    github = "ghp" + "_abcdefghijklmnopqrstuvwxyz1234567890"
    slack = "xoxb" + "-123456789012-123456789012-abcdefghijklmnop"
    aws = "AKI" + "AIOSFODNN7EXAMPLE"
    uri_query_secret = "uri-query-secret"
    sibling_secret = "sibling-default-secret"
    client_secret = "client-secret-value"
    service_token = "service-token-value"
    session_token = "session-token-value"
    bare_token = "bare-token-value"
    pem = "-----BEGIN " + "PRIVATE KEY-----\nprivate-material\n-----END " + "PRIVATE KEY-----"
    response = _completed_response().model_copy(
        update={
            "data": QueryResult(
                columns=("api_key", "safe", "access_token", "password"),
                rows=((github, "visible", slack, "cell-password"),),
                row_count=1,
            ),
            "trace": (
                QueryTraceStep(
                    stage=QueryStage.PRECHECK,
                    status="completed",
                    detail={
                        "free_text": (
                            f"{jwt} {github} {slack} {aws} {pem} "
                            'password="two words ' + 'secret" '
                            "private_key='inline-" + "private' "
                            "dsn='postgresql://reader:dsn-" + "secret@db/sales'"
                            f' client_secret="{client_secret}"'
                            f" service_token={service_token}"
                            f" session_token='{session_token}'"
                            f" token={bare_token}"
                            f" https://service.invalid/path?token={uri_query_secret}&safe=1"
                            f"&client_secret={client_secret}&service_token={service_token}"
                            f"&session_token={session_token}"
                        ),
                        "parameter": {
                            "name": "password",
                            "default_values": [sibling_secret],
                        },
                    },
                ),
            ),
        }
    )
    artifact = build_query_diagnostic_artifact(
        request=QueryRequest(project_id="sales", question=f"inspect {jwt}"),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="natural",
        created_at=_CREATED_AT,
        max_result_rows=1,
    )
    serialized = artifact.model_dump_json()

    for secret in (
        jwt,
        github,
        slack,
        aws,
        "private-material",
        "two words secret",
        "inline-private",
        "dsn-secret",
        "cell-password",
        uri_query_secret,
        sibling_secret,
        client_secret,
        service_token,
        session_token,
        bare_token,
    ):
        assert secret not in serialized
    assert artifact.response["data"]["rows"] == [
        ["[REDACTED]", "visible", "[REDACTED]", "[REDACTED]"]
    ]


def test_markdown_dynamic_text_cannot_inject_titles_links_or_remote_images() -> None:
    attack = "bad\n# injected ![pixel](https://evil.invalid/p.png) [click](https://evil.invalid)"
    response = _completed_response().model_copy(update={"query_id": attack})
    artifact = build_query_diagnostic_artifact(
        request=QueryRequest(project_id="sales", question=attack),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="natural",
        created_at=_CREATED_AT,
    )
    exported = render_query_diagnostic_export(
        artifact,
        context={"metric_catalog": [{"id": "net_revenue", "name": attack, "aggregation": "sum"}]},
    )

    outside_fences: list[str] = []
    in_fence = False
    for line in exported.markdown.splitlines():
        if re.fullmatch(r"`{3,}(?:json)?", line):
            in_fence = not in_fence
        elif not in_fence:
            outside_fences.append(line)
    rendered_text = "\n".join(outside_fences)
    assert "![pixel](" not in rendered_text
    assert "](https://evil.invalid" not in rendered_text
    assert not any(line.startswith("# injected") for line in outside_fences)


def test_revision_and_release_context_sql_obeys_export_debug_gate() -> None:
    sentinel = "SELECT diagnostic_context_secret FROM private_table"
    context = {
        "revision": {"catalog": {"models": [{"sql_query": sentinel}]}},
        "release": {"release": {"models": [{"filter_sql": sentinel}]}},
    }

    artifact = _artifact(physical_sql=sentinel).model_copy(
        update={
            "trace": (
                QueryTraceStep(
                    stage=QueryStage.PHYSICAL_SQL_CORRECTING,
                    status="completed",
                    detail={"original_sql": sentinel, "corrected_sql": sentinel},
                ),
                QueryTraceStep(
                    stage=QueryStage.PHYSICAL_SQL_VALIDATING,
                    status="completed",
                    detail={"sql": sentinel},
                ),
            )
        }
    )
    denied = render_query_diagnostic_export(artifact, context=context)
    allowed = render_query_diagnostic_export(
        artifact,
        context=context,
        allow_debug_sql=True,
    )

    assert sentinel not in denied.model_dump_json()
    assert denied.model_dump_json().count("[REDACTED]") >= 2
    assert "SELECT 区域, 净收入 FROM 销售" in denied.model_dump_json()
    assert sentinel in allowed.model_dump_json()


def test_structured_failure_without_diagnostics_has_unknown_category() -> None:
    response = FailedQueryResponse(
        query_id="structured/failure",
        release_id="staged:rev",
        spec_hash="sha256:spec",
        trace=(QueryTraceStep(stage=QueryStage.PRECHECK, status="failed"),),
        error=QueryError(
            stage=QueryStage.PRECHECK.value,
            code="STRUCTURED_REJECTED",
            message="invalid structured request",
        ),
    )
    artifact = build_query_diagnostic_artifact(
        request=StructuredQueryRequest(
            project_id="sales",
            semantic_query=SemanticQuery(
                dataset_id="sales_scope",
                metric_ids=("net_revenue",),
            ),
        ),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="structured",
        created_at=_CREATED_AT,
    )

    assert render_query_diagnostic_export(artifact).summary["diagnostic_category"] == "unknown"


def test_fixed_timeline_preserves_repeated_events_and_marks_missing_stages() -> None:
    export = render_query_diagnostic_export(
        _artifact(),
        context={
            "release": {"id": "rel_sales"},
            "revision": {"id": "rev_sales", "etag": 7},
            "catalog": {"metrics": [{"id": "net_revenue", "aggregation": "sum"}]},
            "schema_snapshot": {"content_hash": "sha256:schema"},
            "modeling_diagnostics": {"blocking_count": 0},
            "modeling_job": None,
            "modeling_proposal": None,
            "modeling_run": None,
        },
        version_status="CURRENT",
    )

    assert tuple(item.key for item in export.timeline) == QUERY_DIAGNOSTIC_TIMELINE_KEYS
    validation = next(
        item for item in export.timeline if item.key == QueryStage.PHYSICAL_SQL_VALIDATING.value
    )
    assert [event.detail["guard"] for event in validation.events] == [
        "executor_preflight",
        "database_dry_run",
    ]
    assert export.summary["query_id"] == "q_diag"
    assert export.summary["mode"] == "natural"
    assert export.summary["version_status"] == "CURRENT"
    assert export.summary["category"] == "success"
    assert export.summary["diagnostic_category"] == "success"
    assert export.summary["aggregation_comparison"] == [
        {
            "metric_id": "net_revenue",
            "metric_name": "net_revenue",
            "catalog_aggregation": "sum",
            "query_aggregation": "sum",
            "source": "Catalog default",
            "matches_default": True,
        }
    ]
    assert "指标聚合口径" in export.markdown
    assert "部署未授权导出" in export.markdown
    assert "SELECT region" not in export.markdown
    assert export.sha256.startswith("sha256:")


def test_markdown_limit_omits_complete_sections_without_cutting_code_fences() -> None:
    huge = "维度值" * 100_000
    export = render_query_diagnostic_export(
        _artifact(),
        context={
            "release": {"payload": huge},
            "revision": {"payload": huge},
            "catalog": {"payload": huge},
            "index_snapshot": {"payload": huge},
            "schema_snapshot": {"payload": huge},
            "modeling_diagnostics": {"payload": huge},
            "modeling_job": {"payload": huge},
            "modeling_proposal": {"payload": huge},
            "modeling_run": {"payload": huge},
        },
        version_status="CURRENT",
    )

    assert len(export.markdown.encode("utf-8")) <= 1024 * 1024
    fences: dict[int, int] = {}
    for line in export.markdown.splitlines():
        match = re.fullmatch(r"(`{3,})(?:json)?", line)
        if match is not None:
            length = len(match.group(1))
            fences[length] = fences.get(length, 0) + 1
    assert fences
    assert all(count % 2 == 0 for count in fences.values())


def test_index_and_published_release_context_never_exports_embedding_vectors(
    sales_release,
) -> None:
    sentinel_vector = 0.12345678912345678
    index = SemanticIndexSnapshot(
        id="idx_sentinel",
        release_spec_hash=sales_release.spec_hash,
        content_hash="sha256:index",
        state=IndexState.READY,
        embedding_model_id="embedding-sensitive",
        vector_dimension=2,
        entries=(
            SemanticIndexEntry(
                id="entry-1",
                phrase="净收入",
                normalized_phrase="净收入",
                element_type=SemanticElementType.METRIC,
                element_id="net_revenue",
                dataset_ids=("sales_dataset",),
                source="canonical",
            ),
        ),
        vectors=((sentinel_vector, -sentinel_vector),),
    )
    published = PublishedRelease(
        release=sales_release.model_copy(
            update={
                "id": "rel_sentinel",
                "index_snapshot_id": index.id,
            }
        ),
        index_snapshot=index,
        status="active",
    )
    context = {
        "release": _diagnostic_published_release_projection(published),
        "index_snapshot": _diagnostic_index_snapshot_projection(index),
    }
    export = render_query_diagnostic_export(
        _artifact(),
        context=context,
        version_status="CURRENT",
    )
    serialized = export.model_dump_json()

    assert '"entry_count":1' in serialized
    assert '"entry_counts_by_element_type":{"metric":1}' in serialized
    assert "vectors" not in serialized
    assert str(sentinel_vector) not in serialized


def test_failed_trace_marks_unobserved_before_failure_and_not_run_after_failure() -> None:
    response = _completed_response().model_copy(
        update={
            "trace": (
                QueryTraceStep(stage=QueryStage.PRECHECK, status="completed"),
                QueryTraceStep(
                    stage=QueryStage.FINAL_PARSING,
                    status="failed",
                    detail={"code": "PARSER_FAILED"},
                ),
            )
        }
    )
    artifact = build_query_diagnostic_artifact(
        request=QueryRequest(project_id="sales", question="净收入"),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="natural",
        created_at=_CREATED_AT,
    )
    export = render_query_diagnostic_export(artifact)
    by_key = {item.key: item for item in export.timeline}

    assert by_key[QueryStage.CANDIDATE_DISCOVERY.value].status == "not_recorded"
    assert by_key[QueryStage.FINAL_PARSING.value].status == "failed"
    assert by_key[QueryStage.ROUTE_BINDING.value].status == "not_run"


def test_started_then_completed_events_produce_completed_stage_status() -> None:
    response = _completed_response().model_copy(
        update={
            "trace": (
                QueryTraceStep(stage=QueryStage.PRECHECK, status="started"),
                QueryTraceStep(stage=QueryStage.PRECHECK, status="completed"),
                QueryTraceStep(stage=QueryStage.FINISHED, status="completed"),
            )
        }
    )
    artifact = build_query_diagnostic_artifact(
        request=QueryRequest(project_id="sales", question="净收入"),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="natural",
        created_at=_CREATED_AT,
    )
    precheck = next(
        item
        for item in render_query_diagnostic_export(artifact).timeline
        if item.key == QueryStage.PRECHECK.value
    )

    assert precheck.status == "completed"
    assert [event.status for event in precheck.events] == ["started", "completed"]


def test_structured_timeline_marks_natural_only_stages_not_run() -> None:
    response = _completed_response().model_copy(
        update={
            "trace": tuple(
                step
                for step in _completed_response().trace
                if step.stage
                not in {
                    QueryStage.CANDIDATE_DISCOVERY,
                    QueryStage.FINAL_PARSING,
                    QueryStage.PHYSICAL_SQL_CORRECTING,
                }
            )
        }
    )
    artifact = build_query_diagnostic_artifact(
        request=StructuredQueryRequest(
            project_id="sales",
            semantic_query=SemanticQuery(
                dataset_id="sales_scope",
                metric_ids=("net_revenue",),
            ),
        ),
        response=response,
        actor_id="actor-1",
        permission_scope_hash="scope-hash",
        mode="structured",
        created_at=_CREATED_AT,
    )
    export = render_query_diagnostic_export(artifact)
    by_key = {item.key: item for item in export.timeline}

    assert by_key[QueryStage.CANDIDATE_DISCOVERY.value].status == "not_run"
    assert by_key[QueryStage.FINAL_PARSING.value].status == "not_run"
    assert by_key[QueryStage.PHYSICAL_SQL_CORRECTING.value].status == "not_run"


def test_store_enforces_actor_project_query_id_and_ttl() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = CatalogStore(engine)
    store.create_schema()
    try:
        artifact = _artifact(ttl_seconds=60)
        store.save_query_diagnostic(artifact)

        injected = artifact.model_copy(
            update={
                "query_id": "persistence-boundary",
                "response": {
                    "data": {
                        "columns": ["api_key", "safe"],
                        "rows": [["late-secret", "visible"]],
                    }
                },
            }
        )
        store.save_query_diagnostic(injected)
        persisted = store.get_query_diagnostic(
            actor_id="actor-1",
            project_id="sales",
            permission_scope_hash="scope-hash",
            query_id="persistence-boundary",
            now=_CREATED_AT,
        )
        assert persisted.response["data"]["rows"] == [["[REDACTED]", "visible"]]

        loaded = store.get_query_diagnostic(
            actor_id="actor-1",
            project_id="sales",
            permission_scope_hash="scope-hash",
            query_id="q_diag",
            now=_CREATED_AT + timedelta(seconds=59),
        )
        assert loaded == artifact

        for actor_id, project_id, permission_scope_hash, now in (
            ("actor-2", "sales", "scope-hash", _CREATED_AT),
            ("actor-1", "other", "scope-hash", _CREATED_AT),
            ("actor-1", "sales", "other-scope", _CREATED_AT),
            ("actor-1", "sales", "scope-hash", _CREATED_AT + timedelta(seconds=61)),
        ):
            with pytest.raises(CatalogError) as raised:
                store.get_query_diagnostic(
                    actor_id=actor_id,
                    project_id=project_id,
                    permission_scope_hash=permission_scope_hash,
                    query_id="q_diag",
                    now=now,
                )
            assert raised.value.code == "QUERY_DIAGNOSTIC_NOT_FOUND"
    finally:
        engine.dispose()


def test_artifact_rejects_retention_beyond_the_hard_ttl() -> None:
    with pytest.raises(ValueError, match="retention"):
        _artifact(ttl_seconds=QUERY_DIAGNOSTIC_MAX_TTL_SECONDS + 1)


class _DiagnosticApiApplication:
    def __init__(self, *, expected_allow_debug_sql: bool = False) -> None:
        self.expected_allow_debug_sql = expected_allow_debug_sql

    def export_query_diagnostic(
        self,
        *,
        project_id: str,
        query_id: str,
        actor_id: str,
        permission_scope_hash: str,
        allow_debug_sql: bool,
    ):
        if (
            project_id,
            query_id,
            actor_id,
            permission_scope_hash,
            allow_debug_sql,
        ) != (
            "sales",
            "q/diag",
            "actor-1",
            "scope-hash",
            self.expected_allow_debug_sql,
        ):
            raise CatalogError(
                "query diagnostic was not found",
                code="QUERY_DIAGNOSTIC_NOT_FOUND",
            )
        return render_query_diagnostic_export(_artifact(), version_status="CURRENT")


def test_export_api_returns_markdown_and_hides_every_scope_mismatch() -> None:
    client = TestClient(
        create_api(application=_DiagnosticApiApplication(), service_secret=_SECRET),
        raise_server_exceptions=False,
    )

    response = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers=_HEADERS,
    )
    assert response.status_code == 200
    payload = QueryDiagnosticExport.model_validate(response.json())
    assert payload.filename == "knowflow-diagnostic-q_diag.md"
    assert payload.media_type == "text/markdown; charset=utf-8"
    assert payload.timeline[0].group == "context"
    assert payload.timeline[2].group == "query"

    wrong_project_headers = {
        **_HEADERS,
        "X-KnowFlow-Project-Id": "other",
    }
    wrong_actor_headers = {
        **_HEADERS,
        "X-KnowFlow-Actor-Id": "actor-2",
    }
    wrong_scope_headers = {
        **_HEADERS,
        "X-KnowFlow-Permission-Scope-Hash": "other-scope",
    }
    wrong_project = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers=wrong_project_headers,
    )
    wrong_actor = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers=wrong_actor_headers,
    )
    wrong_scope = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers=wrong_scope_headers,
    )

    assert wrong_project.status_code == 404
    assert wrong_actor.status_code == 404
    assert wrong_scope.status_code == 404
    assert wrong_project.json() == wrong_actor.json()
    assert wrong_scope.json() == wrong_actor.json()
    assert wrong_actor.json()["detail"]["code"] == "QUERY_DIAGNOSTIC_NOT_FOUND"


def test_export_api_passes_the_deployment_debug_sql_gate() -> None:
    client = TestClient(
        create_api(
            application=_DiagnosticApiApplication(expected_allow_debug_sql=True),
            service_secret=_SECRET,
            allow_debug_sql=True,
        ),
        raise_server_exceptions=False,
    )

    response = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers=_HEADERS,
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("header", "value"),
    (
        ("X-KnowFlow-Actor-Id", "actor-1 "),
        ("X-KnowFlow-Project-Id", " sales"),
        ("X-KnowFlow-Permission-Scope-Hash", "scope-hash "),
    ),
)
def test_api_rejects_signed_context_headers_with_edge_whitespace(header: str, value: str) -> None:
    client = TestClient(
        create_api(application=_DiagnosticApiApplication(), service_secret=_SECRET),
        raise_server_exceptions=False,
    )
    response = client.get(
        "/v1/analytics/projects/sales/query-diagnostics/export?query_id=q%2Fdiag",
        headers={**_HEADERS, header: value},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid signed request context"


def test_store_enforces_hard_actor_project_quota_and_batch_expiry_purge() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = CatalogStore(engine)
    store.create_schema()
    try:
        for index in range(QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT + 3):
            store.save_query_diagnostic(
                _artifact(ttl_seconds=60).model_copy(update={"query_id": f"quota-{index}"})
            )
        with engine.connect() as connection:
            stored = connection.execute(
                select(func.count()).select_from(query_diagnostics)
            ).scalar()
        assert stored == QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT

        with pytest.raises(CatalogError):
            store.get_query_diagnostic(
                actor_id="actor-1",
                project_id="sales",
                permission_scope_hash="scope-hash",
                query_id="quota-0",
                now=_CREATED_AT,
            )
        newest = store.get_query_diagnostic(
            actor_id="actor-1",
            project_id="sales",
            permission_scope_hash="scope-hash",
            query_id=f"quota-{QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT + 2}",
            now=_CREATED_AT,
        )
        assert newest.query_id.endswith("2")

        deleted = store.purge_expired_query_diagnostics(
            now=_CREATED_AT + timedelta(seconds=61),
            batch_size=2,
        )
        assert deleted == 2
        with engine.connect() as connection:
            remaining = connection.execute(
                select(func.count()).select_from(query_diagnostics)
            ).scalar()
        assert remaining == QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT - 2
    finally:
        engine.dispose()


def test_postgres_query_diagnostic_quota_uses_a_stable_scope_advisory_lock() -> None:
    first = _query_diagnostic_advisory_lock_id("actor-1", "sales")
    assert first == _query_diagnostic_advisory_lock_id("actor-1", "sales")
    assert first != _query_diagnostic_advisory_lock_id("actor-2", "sales")
    assert -(2**63) <= first < 2**63

    class _Dialect:
        name = "postgresql"

    class _Connection:
        dialect = _Dialect()

        def __init__(self) -> None:
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)

    connection = _Connection()
    CatalogStore._acquire_query_diagnostic_scope_lock(
        connection,
        actor_id="actor-1",
        project_id="sales",
    )
    assert len(connection.statements) == 1
    compiled = connection.statements[0].compile()
    assert "pg_advisory_xact_lock" in str(compiled)
    assert first in compiled.params.values()

    connection.dialect.name = "sqlite"
    CatalogStore._acquire_query_diagnostic_scope_lock(
        connection,
        actor_id="actor-1",
        project_id="sales",
    )
    assert len(connection.statements) == 1
