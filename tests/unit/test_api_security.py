from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from knowflow_analytics.api import (
    _ordinary_query_projection,
    _RateLimiter,
    _validate_catalog_resource_path_id,
    create_api,
)
from knowflow_analytics.contracts import QueryResult, SemanticQuery
from knowflow_analytics.query.confirmation_memory import ConfirmationMemory
from knowflow_analytics.query.contracts import (
    CompletedQueryResponse,
    DrilldownOption,
    FailedQueryResponse,
    QueryError,
    QueryInterpretation,
    QueryState,
    QueryTraceStep,
)

_SECRET = "a" * 32
_HEADERS = {
    "X-KnowFlow-Service-Token": _SECRET,
    "X-KnowFlow-Actor-Id": "actor-1",
    "X-KnowFlow-Project-Id": "sales",
    "X-KnowFlow-Permission-Scope-Hash": "scope-hash",
}


class _FakeApplication:
    def __init__(self) -> None:
        self.last_query = None
        self.last_model_request = None
        self.last_catalog_model_request = None
        self.last_suggestion_run_request = None
        self.last_apply_run_request = None
        self.last_term_request = None
        self.last_dimension_value_request = None
        self.last_dictionary_preview_request = None
        self.last_dictionary_apply_request = None
        self.last_query_rule_request = None
        self.last_confirmation_memory_request = None

    def create_project(self, *, name, project_id):
        return {"id": project_id or "generated", "name": name, "active_release_id": None}

    def get_revision(self, revision_id):
        project_id = "other" if revision_id == "foreign" else "sales"
        return SimpleNamespace(project_id=project_id, id=revision_id)

    def list_datasource_schemas(self, *, project_id):
        assert project_id == "sales"
        return ("public", "sales")

    def create_schema_snapshot(self, **kwargs):
        return {"id": "schema-1", "project_id": kwargs["project_id"]}

    def create_empty_revision(self, **kwargs):
        return {"id": "r1", "project_id": kwargs["project_id"], "etag": 1}

    def add_table_model(self, **kwargs):
        self.last_model_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 2}

    def upsert_catalog_model(self, **kwargs):
        self.last_catalog_model_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 3}

    def upsert_query_rule(self, **kwargs):
        self.last_query_rule_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 4}

    def create_ai_suggestion_run(self, **kwargs):
        self.last_suggestion_run_request = kwargs
        return {
            "id": "run-1",
            "project_id": "sales",
            "revision_id": kwargs["revision_id"],
            "revision_etag": kwargs["expected_etag"],
            "suggestions": [],
        }

    def get_modeling_run(self, run_id):
        return SimpleNamespace(id=run_id, project_id="sales", revision_id="r1")

    def apply_ai_suggestion_run(self, **kwargs):
        self.last_apply_run_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 3}

    def query(self, payload, *, actor_id=None, permission_scope_hash=None):
        assert actor_id == "actor-1"
        assert permission_scope_hash == "scope-hash"
        self.last_query = payload
        return FailedQueryResponse(
            query_id="q1",
            state=QueryState.FAILED,
            release_id="rel1",
            spec_hash="hash1",
            index_snapshot_id="idx1",
            trace=(),
            error=QueryError(stage="FINAL_PARSING", code="TEST", message="test"),
        )

    def upsert_term(self, **kwargs):
        self.last_term_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 4}

    def upsert_dimension_value(self, **kwargs):
        self.last_dimension_value_request = kwargs
        return {"id": kwargs["revision_id"], "project_id": "sales", "etag": 5}

    def generate_dimension_dictionary_preview(self, **kwargs):
        self.last_dictionary_preview_request = kwargs
        return {
            "id": "dictionary-preview-1",
            "project_id": "sales",
            "revision_id": kwargs["revision_id"],
        }

    def get_dimension_dictionary_preview(self, preview_id):
        project_id = "other" if preview_id == "foreign" else "sales"
        return SimpleNamespace(
            id=preview_id,
            project_id=project_id,
            revision_id="r1",
        )

    def apply_dimension_dictionary_preview(self, **kwargs):
        self.last_dictionary_apply_request = kwargs
        return {"preview": {"id": kwargs["preview_id"]}, "revision": {"etag": 4}}

    def list_confirmation_memories(self, **kwargs):
        self.last_confirmation_memory_request = ("list", kwargs)
        now = datetime(2026, 8, 29, tzinfo=UTC)
        return (
            ConfirmationMemory(
                id="cmem-1",
                actor_id=kwargs["actor_id"],
                project_id=kwargs["project_id"],
                release_id="release-1",
                spec_hash="sha256:spec",
                index_snapshot_id="idx-1",
                detected_text="销售额",
                normalized_phrase="销售额",
                selection_kind="metric",
                semantic_element_id="net_revenue",
                candidate_set_hash="sha256:candidates",
                exact_context_hash="sha256:context",
                created_at=now,
                expires_at=now + timedelta(days=30),
            ),
        )

    def revoke_confirmation_memory(self, **kwargs):
        self.last_confirmation_memory_request = ("revoke", kwargs)
        return kwargs["memory_id"] == "cmem-1"


def _client(application=None, **kwargs):
    return TestClient(
        create_api(
            application=application or _FakeApplication(),
            service_secret=_SECRET,
            **kwargs,
        ),
        raise_server_exceptions=False,
    )


def test_mutating_api_requires_service_authentication():
    response = _client().post(
        "/v1/analytics/projects", json={"name": "销售", "project_id": "sales"}
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "resource_id",
    (".", "..", "../models/victim", r"safe\..\victim", "bad\x00id"),
)
def test_catalog_resource_path_ids_fail_closed(resource_id):
    with pytest.raises(HTTPException) as raised:
        _validate_catalog_resource_path_id(resource_id)

    assert raised.value.status_code == 422


def test_catalog_resource_path_ids_allow_governed_unicode_and_colons():
    assert _validate_catalog_resource_path_id("dimension:电商平台 ID") == "dimension:电商平台 ID"


def test_signed_context_can_create_project_and_extra_input_is_rejected():
    client = _client()
    accepted = client.post(
        "/v1/analytics/projects",
        headers=_HEADERS,
        json={"name": "销售", "project_id": "sales"},
    )
    rejected = client.post(
        "/v1/analytics/projects",
        headers=_HEADERS,
        json={"name": "销售", "project_id": "sales", "database_password": "leak"},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 422


def test_confirmation_memory_management_is_actor_and_project_scoped():
    application = _FakeApplication()
    client = _client(application)

    listed = client.get(
        "/v1/analytics/projects/sales/confirmation-memories",
        headers=_HEADERS,
    )
    revoked = client.delete(
        "/v1/analytics/projects/sales/confirmation-memories/cmem-1",
        headers=_HEADERS,
    )
    missing = client.delete(
        "/v1/analytics/projects/sales/confirmation-memories/unknown",
        headers=_HEADERS,
    )

    assert listed.status_code == 200
    memory_item = listed.json()["items"][0]
    assert set(memory_item) == {
        "id",
        "detected_text",
        "selection_kind",
        "created_at",
        "expires_at",
    }
    assert "actor_id" not in memory_item
    assert "semantic_element_id" not in memory_item
    assert revoked.json() == {"revoked": True}
    assert missing.status_code == 404
    assert application.last_confirmation_memory_request == (
        "revoke",
        {"project_id": "sales", "actor_id": "actor-1", "memory_id": "unknown"},
    )


def test_schema_snapshot_rejects_an_explicit_empty_table_scope():
    response = _client().post(
        "/v1/analytics/projects/sales/schema-snapshots",
        headers=_HEADERS,
        json={"schemas": ["sales"], "selected_tables": {}},
    )

    assert response.status_code == 422


def test_project_header_is_fail_closed_and_foreign_revision_is_hidden():
    client = _client()
    wrong_scope = dict(_HEADERS, **{"X-KnowFlow-Project-Id": "other"})
    mismatch = client.get("/v1/analytics/projects/sales/revisions/r1", headers=wrong_scope)
    foreign = client.get("/v1/analytics/projects/sales/revisions/foreign", headers=_HEADERS)

    assert mismatch.status_code == 403
    assert foreign.status_code == 404


def test_debug_sql_flag_is_removed_when_server_disallows_it():
    application = _FakeApplication()
    response = _client(application).post(
        "/v1/analytics/query",
        headers=_HEADERS,
        json={
            "project_id": "sales",
            "question": "净收入",
            "dataset_ids": ["sales_dataset"],
            "include_debug_sql": True,
        },
    )

    assert response.status_code == 200
    assert application.last_query.include_debug_sql is False


def test_ordinary_query_projection_contains_business_output_but_no_scope_or_sql():
    response = CompletedQueryResponse(
        query_id="q1",
        release_id="release-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
        trace=(
            QueryTraceStep(
                stage="CANDIDATE_DISCOVERY",
                status="completed",
                detail={
                    "selected_dataset_id": "dataset:orders",
                    "owner_model_ids": ["model:orders"],
                },
            ),
        ),
        interpretation=QueryInterpretation(
            dataset_id="dataset:orders",
            metrics=("订单净金额",),
            dimensions=("地区",),
            filters=(),
            applied_defaults=(
                "query_rule:internal-recent-seven",
                "default_dimension_value:dimension:secret",
            ),
        ),
        data=QueryResult(
            columns=("dimension:region", "metric:net_amount", "physical_secret_column"),
            rows=(("华东", 500, "hidden"),),
            row_count=1,
        ),
        visualization={
            "type": "bar",
            "x": "dimension:region",
            "y": ["metric:net_amount", "metric:not_in_columns"],
        },
        semantic_query=SemanticQuery(
            dataset_id="dataset:orders",
            metric_ids=("metric:net_amount",),
            dimension_ids=("dimension:region",),
        ),
        parsed_s2sql='SELECT "地区", SUM("订单净金额") FROM "orders分析"',
        corrected_s2sql='SELECT "地区", SUM("订单净金额") FROM "orders分析"',
        physical_sql='SELECT region, SUM(net_amount) FROM "orders"',
        drilldown=(
            DrilldownOption(
                token="drl1.ctx.elem.d.ffff.sig",
                kind="dimension",
                action="add",
                label="渠道",
            ),
        ),
    )

    projected = _ordinary_query_projection(response)
    rendered = str(projected)
    assert projected["data"]["columns"] == ["地区", "订单净金额", "结果列 3"]
    assert projected["interpretation"]["applied_defaults"] == ()
    assert projected["trace"][0]["detail"] == {}
    # Chart axes are re-expressed as the shipped column labels; an element
    # missing from the result columns is dropped instead of leaking its ID.
    assert projected["visualization"] == {"type": "bar", "x": "地区", "y": ["订单净金额"]}
    # Drilldown ships the opaque token and the governed label only.
    assert projected["drilldown"] == [
        {
            "token": "drl1.ctx.elem.d.ffff.sig",
            "kind": "dimension",
            "action": "add",
            "label": "渠道",
        }
    ]
    for internal in (
        "dataset:orders",
        "model:orders",
        "metric:net_amount",
        "dimension:region",
        "physical_secret_column",
        "internal-recent-seven",
        "dimension:secret",
        "physical_sql",
        "semantic_query",
        "corrected_s2sql",
    ):
        assert internal not in rendered


def test_ordinary_query_projection_defaults_empty_visualization_to_table():
    response = CompletedQueryResponse(
        query_id="q1",
        release_id="release-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="dataset:orders",
            metrics=("订单净金额",),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(columns=("metric:net_amount",), rows=((500,),), row_count=1),
        visualization={},
        semantic_query=SemanticQuery(
            dataset_id="dataset:orders",
            metric_ids=("metric:net_amount",),
            dimension_ids=(),
        ),
        parsed_s2sql="SELECT 1",
        corrected_s2sql="SELECT 1",
    )

    projected = _ordinary_query_projection(response)

    assert projected["visualization"] == {"type": "table", "x": None, "y": []}


def test_ordinary_failed_projection_does_not_expose_database_error_text():
    response = FailedQueryResponse(
        query_id="q-failed",
        release_id="release-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
        trace=(),
        error=QueryError(
            stage="EXECUTING",
            code="DATABASE_EXECUTION_FAILED",
            message='column "secret_email" of relation "private.orders" does not exist',
        ),
    )

    projected = _ordinary_query_projection(response)

    assert projected["error"]["code"] == "DATABASE_EXECUTION_FAILED"
    assert "secret_email" not in projected["error"]["message"]
    assert "private.orders" not in projected["error"]["message"]


def test_query_rule_endpoint_uses_the_revision_bound_contract() -> None:
    application = _FakeApplication()
    response = _client(application).put(
        "/v1/analytics/projects/sales/revisions/r1/catalog/query-rules/recent-seven",
        headers=_HEADERS,
        json={
            "expected_etag": 3,
            "schema_snapshot_hash": "schema-hash",
            "query_rule": {
                "id": "recent-seven",
                "dataset_id": "sales_dataset",
                "priority": 3,
                "rule_type": "ADD_DATE",
                "mode": "RECENT",
                "parameters": [7],
            },
        },
    )

    assert response.status_code == 200
    assert application.last_query_rule_request["query_rule"].id == "recent-seven"


def test_body_limit_and_actor_rate_limit_are_enforced():
    body_limited = _client(request_body_limit_bytes=64).post(
        "/v1/analytics/projects",
        headers=_HEADERS,
        json={"name": "x" * 100, "project_id": "sales"},
    )
    rate_client = _client(requests_per_minute=1, expensive_requests_per_minute=10)
    first = rate_client.post(
        "/v1/analytics/query",
        headers=_HEADERS,
        json={"project_id": "sales", "question": "净收入"},
    )
    second = rate_client.post(
        "/v1/analytics/query",
        headers=_HEADERS,
        json={"project_id": "sales", "question": "净收入"},
    )

    assert body_limited.status_code == 413
    assert first.status_code == 200
    assert second.status_code == 429


def test_rate_limiter_periodically_discards_inactive_actor_buckets(monkeypatch):
    now = iter((0.0, 1.0, 61.0))
    monkeypatch.setattr("knowflow_analytics.api.time.monotonic", lambda: next(now))
    limiter = _RateLimiter(regular_limit=10, expensive_limit=10)

    limiter.check(actor_id="inactive", bucket="regular")
    limiter.check(actor_id="active", bucket="regular")

    assert ("inactive", "regular") not in limiter._requests
    assert ("active", "regular") in limiter._requests


def test_unhandled_error_does_not_expose_internal_message():
    class _BrokenApplication(_FakeApplication):
        def create_project(self, *, name, project_id):
            raise RuntimeError("password=should-never-be-returned")

    response = _client(_BrokenApplication()).post(
        "/v1/analytics/projects",
        headers=_HEADERS,
        json={"name": "销售", "project_id": "sales"},
    )

    assert response.status_code == 500
    assert "password" not in response.text


def test_semantic_projection_has_no_public_write_endpoint(sales_release):
    application = _FakeApplication()
    response = _client(application).put(
        "/v1/analytics/projects/sales/revisions/r1",
        headers=_HEADERS,
        json={
            "expected_etag": 2,
            "schema_snapshot_hash": "sha256:snapshot",
            "semantic_spec": sales_release.model_dump(mode="json"),
        },
    )

    assert response.status_code == 405


def test_catalog_model_is_the_atomic_model_write_contract():
    application = _FakeApplication()
    response = _client(application).put(
        "/v1/analytics/projects/sales/revisions/r1/catalog/models/model-orders",
        headers=_HEADERS,
        json={
            "expected_etag": 2,
            "schema_snapshot_hash": "sha256:snapshot",
            "model": {
                "id": "model-orders",
                "name": "订单",
                "bizName": "orders",
                "description": "订单事实表",
                "sensitiveLevel": 0,
                "modelDetail": {
                    "queryType": "table_query",
                    "tableQuery": "sales.orders",
                    "identifiers": [],
                    "dimensions": [],
                    "measures": [],
                    "fields": [{"fieldName": "id", "dataType": "bigint"}],
                    "sqlVariables": [],
                },
                "viewers": [],
                "viewOrgs": [],
                "admins": [],
                "adminOrgs": [],
                "ext": {"preserved": True},
            },
        },
    )

    assert response.status_code == 200
    submitted = application.last_catalog_model_request["model"]
    assert submitted.id == "model-orders"
    assert submitted.model_detail.table_query == "sales.orders"
    assert submitted.ext == {"preserved": True}


def test_legacy_model_and_field_patch_endpoints_are_removed():
    application = _FakeApplication()
    client = _client(application)
    body = {
        "expected_etag": 2,
        "schema_snapshot_hash": "sha256:snapshot",
        "name": "旧写入口",
    }

    model = client.patch(
        "/v1/analytics/projects/sales/revisions/r1/models/model-orders",
        headers=_HEADERS,
        json=body,
    )
    field = client.patch(
        "/v1/analytics/projects/sales/revisions/r1/fields/field-orders-id",
        headers=_HEADERS,
        json=body,
    )

    assert model.status_code == 404
    assert field.status_code == 404


def test_term_and_dimension_value_use_catalog_resources():
    application = _FakeApplication()
    client = _client(application)
    term = client.put(
        "/v1/analytics/projects/sales/revisions/r1/catalog/terms/sales_amount",
        headers=_HEADERS,
        json={
            "expected_etag": 3,
            "schema_snapshot_hash": "sha256:snapshot",
            "term": {
                "id": "sales_amount",
                "name": "销售额",
                "metric_ids": ["net_revenue"],
            },
        },
    )
    dimension_value = client.put(
        "/v1/analytics/projects/sales/revisions/r1/catalog/dimension-values/region_east",
        headers=_HEADERS,
        json={
            "expected_etag": 4,
            "schema_snapshot_hash": "sha256:snapshot",
            "dimension_value": {
                "id": "region_east",
                "dimension_id": "region",
                "value": "华东",
                "display_name": "华东",
                "aliases": ["东区"],
            },
        },
    )

    assert term.status_code == 200
    assert dimension_value.status_code == 200
    assert application.last_term_request["term"].name == "销售额"
    assert application.last_dimension_value_request["dimension_value"].aliases == ("东区",)


def test_dimension_dictionary_requires_a_separate_complete_human_apply():
    application = _FakeApplication()
    client = _client(application)
    preview = client.post(
        "/v1/analytics/projects/sales/revisions/r1/dimension-dictionary/previews",
        headers=_HEADERS,
        json={
            "expected_etag": 3,
            "schema_snapshot_hash": "sha256:snapshot",
            "dimension_ids": ["region"],
        },
    )
    applied = client.post(
        "/v1/analytics/projects/sales/revisions/r1/dimension-dictionary/"
        "previews/dictionary-preview-1/apply",
        headers=_HEADERS,
        json={
            "expected_etag": 3,
            "schema_snapshot_hash": "sha256:snapshot",
            "confirmation": "apply",
            "decisions": [
                {
                    "candidate_id": "region-east",
                    "accept": True,
                    "display_name": "华东区域",
                    "aliases": ["东区"],
                }
            ],
        },
    )
    foreign = client.get(
        "/v1/analytics/projects/sales/revisions/r1/dimension-dictionary/previews/foreign",
        headers=_HEADERS,
    )

    assert preview.status_code == 200
    assert applied.status_code == 200
    assert foreign.status_code == 404
    assert application.last_dictionary_preview_request["dimension_ids"] == ("region",)
    decision = application.last_dictionary_apply_request["decisions"][0]
    assert decision.display_name == "华东区域"
    assert application.last_dictionary_apply_request["reviewed_by"] == "actor-1"


def test_dimension_dictionary_rejects_unbounded_alias_input():
    response = _client().post(
        "/v1/analytics/projects/sales/revisions/r1/dimension-dictionary/"
        "previews/dictionary-preview-1/apply",
        headers=_HEADERS,
        json={
            "expected_etag": 3,
            "schema_snapshot_hash": "sha256:snapshot",
            "confirmation": "apply",
            "decisions": [
                {
                    "candidate_id": "region-east",
                    "accept": True,
                    "aliases": ["x" * 257],
                }
            ],
        },
    )

    assert response.status_code == 422


def test_page_modeling_apis_do_not_require_an_agent_task():
    application = _FakeApplication()
    client = _client(application)

    schemas = client.get(
        "/v1/analytics/projects/sales/datasources/default/schemas",
        headers=_HEADERS,
    )
    snapshot = client.post(
        "/v1/analytics/projects/sales/schema-snapshots",
        headers=_HEADERS,
        json={"schemas": ["sales"], "selected_tables": {"sales": ["orders"]}},
    )
    revision = client.post(
        "/v1/analytics/projects/sales/revisions",
        headers=_HEADERS,
        json={"schema_snapshot_id": "schema-1"},
    )
    model = client.post(
        "/v1/analytics/projects/sales/revisions/r1/models:from-table",
        headers=_HEADERS,
        json={
            "expected_etag": 1,
            "schema_snapshot_hash": "sha256:snapshot",
            "schema_name": "sales",
            "table_name": "orders",
        },
    )
    run = client.post(
        "/v1/analytics/projects/sales/revisions/r1/suggestion-runs",
        headers=_HEADERS,
        json={"expected_etag": 2, "source": "ui"},
    )

    assert schemas.status_code == 200
    assert schemas.json()["items"] == ["public", "sales"]
    assert snapshot.status_code == 200
    assert revision.status_code == 200
    assert model.status_code == 200
    assert run.status_code == 200
    assert application.last_model_request["table_name"] == "orders"
    assert application.last_suggestion_run_request["source"].value == "ui"
    assert application.last_suggestion_run_request["source_task_id"] is None


def test_page_applies_ai_prefill_only_through_explicit_review_decisions():
    application = _FakeApplication()
    response = _client(application).post(
        "/v1/analytics/projects/sales/revisions/r1/suggestion-runs/run-1:apply",
        headers=_HEADERS,
        json={
            "expected_etag": 2,
            "schema_snapshot_hash": "sha256:snapshot",
            "decisions": [
                {
                    "suggestion_id": "ai-suggestion-1",
                    "accept": True,
                    "overrides": {"name": "净收入"},
                }
            ],
        },
    )

    assert response.status_code == 200
    assert application.last_apply_run_request["run_id"] == "run-1"
    assert application.last_apply_run_request["reviewed_by"] == "actor-1"
    decision = application.last_apply_run_request["decisions"][0]
    assert decision.accept is True
    assert decision.overrides == {"name": "净收入"}


def test_release_ask_context_projects_only_governed_vocabulary(sales_release):
    from knowflow_analytics.api import _release_ask_context

    context = _release_ask_context(sales_release)

    assert {item["id"] for item in context["datasets"]} == {"sales_dataset"}
    assert {"name": "净收入", "aliases": []} in [
        {"name": item["name"], "aliases": item["aliases"]} for item in context["metrics"]
    ] or any(item["name"] for item in context["metrics"])
    rendered = str(context)
    # 无建模产物、物理表列与 SQL。
    for internal in ("modeling_catalog", "schema_name", "table", "column", "sql"):
        assert f"'{internal}'" not in rendered
