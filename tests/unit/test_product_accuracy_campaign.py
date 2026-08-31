from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from scripts.product_accuracy_campaign import (
    HiddenCase,
    HiddenSuite,
    HttpProductApi,
    ModelingInput,
    _all_routed_dataset_ids,
    _dataset_by_root_table,
    _relation_rejection_reason,
    _report_console_summary,
    _row_signature,
    _write_report,
    run_product_accuracy_campaign,
)


def test_result_signature_normalizes_postgres_numeric_values_after_json_transport():
    expected = (("华东", Decimal("300.0")),)
    actual = (("华东", "300"),)

    assert _row_signature(
        actual,
        ordered=False,
        numeric_columns=frozenset({1}),
    ) == _row_signature(
        expected,
        ordered=False,
        numeric_columns=frozenset({1}),
    )


def test_http_product_api_respects_bff_rate_limit_before_failing(monkeypatch):
    attempts = 0
    waits: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"detail": "request rate limit exceeded"})
        return httpx.Response(200, json={"state": "COMPLETED"})

    client = httpx.Client(
        base_url="http://127.0.0.1:9380",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("scripts.product_accuracy_campaign.sleep", waits.append)
    api = HttpProductApi(
        base_url="http://127.0.0.1:9380",
        authorization="signed-token",
        cookie=None,
        client=client,
    )

    try:
        result = api.request("POST", "/query-preview", {})
    finally:
        api.close()
        client.close()

    assert result == {"state": "COMPLETED"}
    assert attempts == 3
    assert waits == [5.0, 15.0]


def test_result_signature_keeps_identifier_like_numeric_text_as_text():
    for code in ("001", "6200"):
        assert _row_signature(((code,),), ordered=True) != _row_signature(
            ((int(code),),), ordered=True
        )


def test_campaign_confirms_only_database_foreign_key_relation_evidence():
    relation = {
        "id": "orders_customer",
        "fromModelId": "orders",
        "toModelId": "customers",
        "joinConditions": [{"leftField": "customer_id", "rightField": "id", "operator": "="}],
    }

    assert (
        _relation_rejection_reason({**relation, "knowflowEvidence": "database_foreign_key"}) is None
    )
    assert (
        _relation_rejection_reason({**relation, "knowflowEvidence": "name_convention"})
        == "RELATION_NOT_DATABASE_FOREIGN_KEY"
    )


def test_scope_discovery_keeps_same_table_names_from_different_schemas():
    revision = {
        "semantic_spec": {
            "models": [
                {"id": "orders-a", "schema_name": "schema_a", "table": "orders"},
                {"id": "orders-b", "schema_name": "schema_b", "table": "orders"},
            ],
            "analysis_topic_routes": [
                {"dataset_id": "scope-a", "root_model_id": "orders-a"},
                {"dataset_id": "scope-b", "root_model_id": "orders-b"},
            ],
        }
    }

    mapping = _dataset_by_root_table(revision)

    assert "orders" not in mapping
    assert mapping["schema_a.orders"] == "scope-a"
    assert mapping["schema_b.orders"] == "scope-b"
    assert _all_routed_dataset_ids(revision) == ("scope-a", "scope-b")


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


class _ReferenceExecutor:
    def execute(self, sql: str):
        assert sql == "SELECT region, SUM(amount) FROM sales GROUP BY region"
        return (("华东", 300), ("华南", 100))


class _ProductApi:
    def __init__(
        self,
        *,
        include_relation: bool = True,
        validation_fails: bool = False,
        expect_sensitive_diagnostics: bool = False,
    ) -> None:
        self.include_relation = include_relation
        self.validation_fails = validation_fails
        self.expect_sensitive_diagnostics = expect_sensitive_diagnostics
        self.validated = False
        self.calls: list[tuple[str, str, dict | None]] = []
        self.revision = self._revision(etag=1, include_topic=False)

    def request(self, method: str, path: str, body: dict | None = None):
        self.calls.append((method, path, body))
        if path == "/v1/analytics/core/projects":
            return {"id": "project-1", "name": "经营分析"}
        if path.endswith("/schema-snapshots"):
            return {
                "id": "snapshot-1",
                "content_hash": "sha256:schema",
                "tables": [
                    {"schema_name": "business", "name": "customers"},
                    {"schema_name": "business", "name": "sales"},
                ],
            }
        if path.endswith("/revisions") and method == "POST":
            return self.revision
        if path.endswith("/models:from-table"):
            return self.revision
        if "/catalog/relations/" in path:
            self.revision = self._revision(etag=2, include_topic=False)
            return self.revision
        if path.endswith("/modeling-jobs") and method == "POST":
            return {"id": "job-1", "status": "queued", "proposal_id": None}
        if path.endswith("/modeling-jobs/job-1") and method == "GET":
            return {"id": "job-1", "status": "completed", "proposal_id": "proposal-1"}
        if path.endswith("/modeling-proposals/proposal-1") and method == "GET":
            return self._proposal()
        if "/modeling-proposals/proposal-1" in path and method == "PUT":
            assert all(item["accept"] for item in body["decisions"])
            assert all(not item["overrides"] for item in body["decisions"])
            return {
                "id": "proposal-1",
                "etag": 2,
                "proposal_hash": "sha256:proposal-2",
                "suggestions": [
                    {"id": "suggestion-1", "changes": {"name": "销售金额"}},
                    {"id": "suggestion-2", "changes": {"kind": "measure"}},
                ],
                "decisions": body["decisions"],
                "artifact": self._artifact(),
            }
        if path.endswith("/modeling-proposals/proposal-1:apply"):
            self.revision = self._revision(etag=3, include_topic=True)
            return {
                "proposal": {"id": "proposal-1", "status": "applied"},
                "revision": self.revision,
            }
        if path.endswith("/validate"):
            assert body == {}
            if self.validation_fails:
                raise RuntimeError("revision validation failed")
            self.validated = True
            return {**self.revision, "state": "validated"}
        if path.endswith("/query-preview"):
            assert self.validated
            if self.expect_sensitive_diagnostics:
                assert body["include_diagnostics"] is True
                assert body["include_debug_sql"] is True
            else:
                assert "include_diagnostics" not in body
                assert "include_debug_sql" not in body
            return {
                "state": "COMPLETED",
                "data": {
                    "columns": ["sales-region", "sales-amount"],
                    "rows": [["华东", "300"], ["华南", "100"]],
                },
                "error": None,
                "semantic_query": {"dataset_id": "dataset-1"},
                "corrected_s2sql": 'SELECT "区域", SUM("销售金额") FROM "销售分析" GROUP BY "区域"',
                "physical_sql": "SELECT region, SUM(amount) FROM sales GROUP BY region",
                "trace": [
                    {"stage": "FINAL_PARSING", "status": "completed", "detail": {"parser": "llm"}}
                ],
            }
        raise AssertionError(f"unexpected request: {method} {path}")

    @classmethod
    def _proposal(cls) -> dict:
        return {
            "id": "proposal-1",
            "etag": 1,
            "proposal_hash": "sha256:proposal-1",
            "suggestions": [
                {"id": "suggestion-1", "changes": {"name": "销售金额"}},
                {"id": "suggestion-2", "changes": {"kind": "measure"}},
            ],
            "decisions": [
                {"suggestion_id": "suggestion-1", "accept": True, "overrides": {}},
                {"suggestion_id": "suggestion-2", "accept": True, "overrides": {}},
            ],
            "artifact": cls._artifact(),
        }

    @staticmethod
    def _artifact() -> dict:
        return {
            "base_semantic_spec_hash": "sha256:base",
            "dimension_values": [],
            "alias_drafts": [],
            "default_count_metrics": [{"id": "sales-count", "name": "销售数量"}],
            "analysis_topic_datasets": [{"id": "dataset-1", "name": "销售分析"}],
            "analysis_topic_routes": [
                {
                    "dataset_id": "dataset-1",
                    "root_model_id": "sales-model",
                    "default_count_metric_id": "sales-count",
                }
            ],
            "semantic_context": [
                {
                    "id": "context-1",
                    "target_type": "query_scope",
                    "target_id": "dataset-1",
                    "kind": "scope",
                    "text": "销售分析范围",
                    "source_type": "human_convention",
                }
            ],
            "query_scope_compiler_version": "knowflow-query-scope-v1",
            "query_scope_compilation_hash": "sha256:scope",
            "query_scope_diagnostics": [
                {
                    "dataset_id": "dataset-1",
                    "root_model_id": "sales-model",
                }
            ],
            "artifact_hash": "sha256:artifact",
        }

    def _revision(self, *, etag: int, include_topic: bool) -> dict:
        relations = (
            [
                {
                    "id": "relation-sales-customer",
                    "fromModelId": "sales-model",
                    "toModelId": "customer-model",
                    "joinType": "left join",
                    "joinConditions": [
                        {"leftField": "customer_id", "rightField": "id", "operator": "="}
                    ],
                    "knowflowCardinality": None,
                    "knowflowEvidence": "database_foreign_key",
                }
            ]
            if self.include_relation
            else []
        )
        return {
            "id": "revision-1",
            "etag": etag,
            "schema_snapshot_hash": "sha256:schema",
            "state": "draft",
            "semantic_catalog": {"modelRelations": relations},
            "semantic_spec": {
                "spec_hash": f"sha256:spec-{etag}",
                "models": [
                    {"id": "sales-model", "table": "sales"},
                    {"id": "customer-model", "table": "customers"},
                ],
                "metrics": [{"id": "sales-amount"}],
                "datasets": ([{"id": "dataset-1"}] if include_topic else []),
                "analysis_topic_routes": (
                    [{"dataset_id": "dataset-1", "root_model_id": "sales-model"}]
                    if include_topic
                    else []
                ),
            },
        }


class _MultiScopeProductApi(_ProductApi):
    @staticmethod
    def _artifact() -> dict:
        artifact = _ProductApi._artifact()
        artifact["analysis_topic_datasets"].append({"id": "dataset-2", "name": "库存分析"})
        artifact["analysis_topic_routes"].append(
            {
                "dataset_id": "dataset-2",
                "root_model_id": "inventory-model",
                "default_count_metric_id": None,
            }
        )
        artifact["semantic_context"].append(
            {
                "id": "context-2",
                "target_type": "query_scope",
                "target_id": "dataset-2",
                "kind": "scope",
                "text": "库存分析范围",
                "source_type": "human_convention",
            }
        )
        artifact["query_scope_diagnostics"].append(
            {"dataset_id": "dataset-2", "root_model_id": "inventory-model"}
        )
        return artifact

    def _revision(self, *, etag: int, include_topic: bool) -> dict:
        revision = super()._revision(etag=etag, include_topic=include_topic)
        revision["semantic_spec"]["models"].append({"id": "inventory-model", "table": "inventory"})
        if include_topic:
            revision["semantic_spec"]["datasets"].append({"id": "dataset-2"})
            revision["semantic_spec"]["analysis_topic_routes"].append(
                {"dataset_id": "dataset-2", "root_model_id": "inventory-model"}
            )
        return revision

    def request(self, method: str, path: str, body: dict | None = None):
        if not path.endswith("/query-preview"):
            return super().request(method, path, body)
        self.calls.append((method, path, body))
        assert body is not None
        assert set(body["dataset_ids"]) == {"dataset-1", "dataset-2"}
        if "selected_candidate_id" not in body:
            return {
                "state": "CLARIFICATION_REQUIRED",
                "release_id": "staged:revision-1",
                "spec_hash": "sha256:spec-3",
                "index_snapshot_id": "index-1",
                "options": [
                    {
                        "candidate_id": "scope-sales",
                        "kind": "analysis_object",
                        "label": "销售",
                        "description": "销售业务记录",
                    },
                    {
                        "candidate_id": "scope-stock",
                        "kind": "analysis_object",
                        "label": "库存",
                        "description": "库存业务记录",
                    },
                ],
                "trace": [
                    {
                        "stage": "CANDIDATE_DISCOVERY",
                        "status": "clarification",
                        "detail": {
                            "scope_resolution": {
                                "status": "clarification",
                                "code": "AMBIGUOUS_QUERY_SCOPE",
                            }
                        },
                    }
                ],
            }
        assert body["selected_candidate_id"] == "scope-sales"
        assert body["expected_release_id"] == "staged:revision-1"
        return {
            "state": "COMPLETED",
            "data": {
                "columns": ["sales-region", "sales-amount"],
                "rows": [["华东", "300"], ["华南", "100"]],
            },
            "semantic_query": {"dataset_id": "dataset-1"},
            "error": None,
            "trace": [
                {"stage": "FINAL_PARSING", "status": "completed", "detail": {"parser": "llm"}}
            ],
        }


class _RefusalProductApi(_ProductApi):
    def __init__(self, query_result):
        self.query_result = query_result
        super().__init__()

    def request(self, method: str, path: str, body: dict | None = None):
        if not path.endswith("/query-preview"):
            return super().request(method, path, body)
        self.calls.append((method, path, body))
        if isinstance(self.query_result, Exception):
            raise self.query_result
        return self.query_result


def _modeling_input() -> ModelingInput:
    return ModelingInput(
        project_name="经营分析",
        schemas=("business",),
        selected_tables={"business": ("customers", "sales")},
    )


def _hidden_suite() -> HiddenSuite:
    return HiddenSuite(
        id="hidden-1",
        cases=(
            HiddenCase(
                id="question-1",
                question="各区域销售金额是多少？",
                root_table="sales",
                expected_clarification_label="销售",
                reference_sql="SELECT region, SUM(amount) FROM sales GROUP BY region",
            ),
        ),
    )


def test_campaign_uses_product_order_and_loads_hidden_suite_after_validation():
    api = _ProductApi(expect_sensitive_diagnostics=True)
    loader_calls = 0

    def load_hidden_suite():
        nonlocal loader_calls
        loader_calls += 1
        assert api.validated is True
        return _hidden_suite()

    report = run_product_accuracy_campaign(
        api=api,
        modeling_input=_modeling_input(),
        load_hidden_suite=load_hidden_suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
        include_sensitive_diagnostics=True,
    )

    assert loader_calls == 1
    assert report.modeling_completed is True
    assert report.auto_confirmed_relation_count == 1
    assert report.ai_suggestion_count == 2
    assert report.ai_override_count == 0
    assert report.total_questions == 1
    assert report.correct_answers == 1
    assert report.accuracy == 1.0
    assert report.silent_wrong_count == 0
    assert report.results[0].corrected_s2sql == (
        'SELECT "区域", SUM("销售金额") FROM "销售分析" GROUP BY "区域"'
    )
    assert report.results[0].physical_sql == (
        "SELECT region, SUM(amount) FROM sales GROUP BY region"
    )
    assert report.results[0].expected_row_count == 2
    assert report.results[0].actual_row_count == 2
    assert report.results[0].expected_rows_preview == (("华东", 300), ("华南", 100))
    assert report.results[0].actual_rows_preview == (("华东", "300"), ("华南", "100"))
    console_summary = _report_console_summary(report)
    serialized_summary = str(console_summary)
    assert console_summary["accuracy"] == 1.0
    assert console_summary["total_questions"] == 1
    assert "physical_sql" not in serialized_summary
    assert "expected_rows_preview" not in serialized_summary
    assert "SELECT region" not in serialized_summary
    assert "华东" not in serialized_summary
    paths = [path for _method, path, _body in api.calls]
    assert paths[:5] == [
        "/v1/analytics/core/projects",
        "/v1/analytics/core/projects/project-1/schema-snapshots",
        "/v1/analytics/core/projects/project-1/revisions",
        "/v1/analytics/core/projects/project-1/revisions/revision-1/models:from-table",
        "/v1/analytics/core/projects/project-1/revisions/revision-1/models:from-table",
    ]
    assert paths.index(next(item for item in paths if "/catalog/relations/" in item)) < paths.index(
        next(item for item in paths if item.endswith("/modeling-jobs"))
    )
    assert not any("analysis-topic-proposals" in path for path in paths)
    assert not any("/analysis-topics/" in path for path in paths)


def test_campaign_redacts_sql_and_rows_unless_private_diagnostics_are_opted_in():
    report = run_product_accuracy_campaign(
        api=_ProductApi(),
        modeling_input=_modeling_input(),
        load_hidden_suite=_hidden_suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
    )

    result = report.results[0]
    assert result.correct is True
    assert result.expected_result_hash is not None
    assert result.actual_result_hash is not None
    assert result.corrected_s2sql is None
    assert result.physical_sql is None
    assert result.expected_rows_preview == ()
    assert result.actual_rows_preview == ()


def test_campaign_discovers_all_scopes_before_using_hidden_root_adjudication():
    api = _MultiScopeProductApi()

    report = run_product_accuracy_campaign(
        api=api,
        modeling_input=_modeling_input(),
        load_hidden_suite=_hidden_suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
    )

    preview_calls = [body for _method, path, body in api.calls if path.endswith("/query-preview")]
    assert len(preview_calls) == 2
    assert set(preview_calls[0]["dataset_ids"]) == {"dataset-1", "dataset-2"}
    assert "selected_candidate_id" not in preview_calls[0]
    assert preview_calls[1]["selected_candidate_id"] == "scope-sales"
    assert preview_calls[1]["expected_index_snapshot_id"] == "index-1"
    assert report.results[0].correct is True
    assert report.results[0].first_turn_clarification is True
    assert report.results[0].manual_selection_count == 1
    assert report.first_turn_clarification_count == 1
    assert report.manual_selection_count == 1


@pytest.mark.parametrize(
    ("query_result", "expected_correct"),
    [
        (
            {
                "state": "FAILED",
                "error": {"code": "UNSUPPORTED_INTENT", "stage": "PRECHECK"},
                "trace": [],
            },
            True,
        ),
        (
            {
                "state": "FAILED",
                "error": {"code": "AUTHENTICATION_FAILED", "stage": "PRECHECK"},
                "trace": [],
            },
            False,
        ),
        (
            {
                "state": "FAILED",
                "error": {"code": "INTERNAL_ERROR", "stage": "FINISHED"},
                "trace": [],
            },
            False,
        ),
        (
            {
                "state": "FAILED",
                "error": {"code": "WRONG_BUSINESS_REJECTION", "stage": "PRECHECK"},
                "trace": [],
            },
            False,
        ),
        (
            {"state": "CLARIFICATION_REQUIRED", "options": [], "trace": []},
            False,
        ),
        (RuntimeError("gateway timeout"), False),
    ],
)
def test_expected_refusal_requires_an_explicit_business_code_and_stage(
    query_result,
    expected_correct,
):
    suite = HiddenSuite(
        id="refusal-suite",
        cases=(
            HiddenCase(
                id="refusal-1",
                question="删除销售表",
                root_table="sales",
                expected_state="FAILED",
                allowed_error_codes=("UNSUPPORTED_INTENT",),
                allowed_error_stages=("PRECHECK",),
            ),
        ),
    )
    report = run_product_accuracy_campaign(
        api=_RefusalProductApi(query_result),
        modeling_input=_modeling_input(),
        load_hidden_suite=lambda: suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
    )

    assert report.results[0].correct is expected_correct
    assert report.results[0].correct_refusal is expected_correct


def test_no_fk_relation_is_not_invented_by_the_campaign():
    api = _ProductApi(include_relation=False)

    report = run_product_accuracy_campaign(
        api=api,
        modeling_input=_modeling_input(),
        load_hidden_suite=_hidden_suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
    )

    assert report.auto_confirmed_relation_count == 0
    assert not any("/catalog/relations/" in path for _method, path, _body in api.calls)


def test_modeling_failure_counts_every_hidden_question_as_incorrect():
    api = _ProductApi(validation_fails=True)
    loaded_after_failure = False

    def load_hidden_suite():
        nonlocal loaded_after_failure
        loaded_after_failure = True
        return _hidden_suite()

    report = run_product_accuracy_campaign(
        api=api,
        modeling_input=_modeling_input(),
        load_hidden_suite=load_hidden_suite,
        reference_executor=_ReferenceExecutor(),
        now=_Clock(),
    )

    assert loaded_after_failure is True
    assert report.modeling_completed is False
    assert report.total_questions == 1
    assert report.correct_answers == 0
    assert report.false_refusal_count == 1
    assert report.accuracy == 0.0


def test_product_report_is_written_atomically_with_private_permissions(tmp_path):
    output = tmp_path / "reports" / "product_accuracy_report.json"

    _write_report(output, '{"contract_version":"knowflow-product-accuracy-v1"}')

    assert output.read_text(encoding="utf-8") == (
        '{"contract_version":"knowflow-product-accuracy-v1"}\n'
    )
    assert output.stat().st_mode & 0o777 == 0o600


def test_numeric_dimension_strings_normalize_against_reference_integers():
    """NUMERIC 维度列经 API 变成字符串（"2017"），参考执行器返回整数 2017。

    只有规范回环的数字串（str(Decimal) == 原串）才提升为数字——"001" 这类
    业务代码保持文本，不会与 1 误判相等。
    """

    import scripts.product_accuracy_campaign as pac

    api_rows = (("2017", "1000"), ("2018", "1500"))
    reference_rows = ((2017, 1000), (2018, 1500))

    numeric_columns = pac._numeric_column_indexes(
        {
            "semantic_spec": {
                "metrics": [{"id": "metric:staff"}],
                "dimensions": [{"id": "dim:year", "data_type": "numeric"}],
            }
        },
        {"columns": ["dim:year", "metric:staff"]},
    )
    assert numeric_columns == frozenset({0, 1})
    assert pac._row_signature(api_rows, ordered=False, numeric_columns=numeric_columns) == (
        pac._row_signature(reference_rows, ordered=False, numeric_columns=numeric_columns)
    )
    # 文本类型维度不受影响："001" 与 1 仍然不同（列类型可信，字符串形状不可信）。
    assert pac._row_signature((("001",),), ordered=False) != pac._row_signature(
        ((1,),), ordered=False
    )
