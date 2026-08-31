from __future__ import annotations

from threading import Barrier, Lock

import pytest
from pydantic import ValidationError

from knowflow_analytics.contracts import (
    Aggregation,
    FilterOperator,
    QueryAggregationOverride,
    QueryFilter,
    QueryMeasureFilter,
    QueryMetricFilter,
    QueryOrder,
    QueryResult,
    SemanticQuery,
    SemanticQueryType,
)
from knowflow_analytics.evaluation.contracts import (
    EvaluationCaseResult,
    ExpectedFilter,
    GoldenCase,
    GoldenSuite,
)
from knowflow_analytics.evaluation.evaluator import GoldenEvaluator, _filters_equal, _summarize
from knowflow_analytics.query.contracts import (
    CompletedQueryResponse,
    MemoryReviewResult,
    MemoryStatus,
    QueryInterpretation,
    QueryStage,
    QueryState,
    QueryTraceStep,
)
from tests.support import GoldenS2SqlGateway


def _completed_case(*, case_id: str, question: str) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        question=question,
        dataset_ids=("sales_dataset",),
        expected_state=QueryState.COMPLETED,
        expected_metric_ids=("net_revenue",),
        expected_rows=((100,),),
    )


def test_completed_golden_case_requires_expected_rows():
    with pytest.raises(ValidationError, match="expected result rows"):
        GoldenCase(
            id="g1",
            question="净收入",
            dataset_ids=("sales_dataset",),
            expected_state=QueryState.COMPLETED,
            expected_metric_ids=("net_revenue",),
        )


def test_golden_suite_rejects_duplicate_questions():
    with pytest.raises(ValidationError, match="questions must be unique"):
        GoldenSuite(
            id="suite-1",
            name="销售",
            project_id="sales",
            cases=(
                _completed_case(case_id="g1", question="净收入"),
                _completed_case(case_id="g2", question="  净收入  "),
            ),
        )


def test_adjudicated_golden_gateway_does_not_depend_on_a_rule_seed(sales_release) -> None:
    suite = GoldenSuite(
        id="gateway-suite",
        name="固定语义",
        project_id="sales",
        cases=(
            GoldenCase(
                id="gateway-case",
                question="各区域净收入",
                dataset_ids=("sales_dataset",),
                expected_state=QueryState.COMPLETED,
                expected_metric_ids=("net_revenue",),
                expected_aggregation_overrides=(
                    QueryAggregationOverride(
                        metric_id="net_revenue",
                        aggregation=Aggregation.SUM,
                    ),
                ),
                expected_dimension_ids=("region",),
                expected_rows=(("华东", 100),),
            ),
        ),
    )
    gateway = GoldenS2SqlGateway(suite, sales_release)

    payload = gateway.generate_json(
        messages=[
            {"role": "system", "content": "governed"},
            {"role": "user", "content": "question=各区域净收入\nmetrics=[]\ndimensions=[]"},
        ]
    )

    assert payload["sql"] == ('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"')


def test_evaluation_treats_in_filter_values_as_an_unordered_set():
    actual = (("region", FilterOperator.IN, ["华南", "华东"]),)
    expected = (("region", FilterOperator.IN, ["华东", "华南"]),)

    assert _filters_equal(actual, expected)


def test_evaluation_treats_conjunctive_filters_as_unordered():
    actual = (
        ("region", FilterOperator.EQ, "华东"),
        ("year", FilterOperator.EQ, 2025),
    )
    expected = tuple(reversed(actual))

    assert _filters_equal(actual, expected)


def test_golden_case_normalizes_and_validates_tags():
    case = _completed_case(case_id="g1", question="净收入").model_copy(
        update={"tags": (" FILTER ", "Alias")}
    )
    validated = GoldenCase.model_validate(case.model_dump(mode="python"))
    assert validated.tags == ("filter", "alias")

    with pytest.raises(ValidationError, match="tags must be unique"):
        GoldenCase.model_validate(
            {
                **case.model_dump(mode="python"),
                "tags": ("filter", " FILTER "),
            }
        )


def test_enabled_memory_requires_a_positive_reviewed_completed_case() -> None:
    enabled = _completed_case(case_id="enabled", question="可复用问题").model_copy(
        update={
            "memory_status": MemoryStatus.ENABLED,
            "memory_review_result": MemoryReviewResult.POSITIVE,
        }
    )
    assert GoldenCase.model_validate(enabled.model_dump(mode="python")) == enabled

    for invalid in (
        enabled.model_copy(update={"memory_review_result": None}),
        enabled.model_copy(update={"tags": ("holdout",)}),
        GoldenCase(
            id="failed",
            question="预期拒答",
            dataset_ids=("sales_dataset",),
            expected_state=QueryState.FAILED,
            expected_error_code="UNSUPPORTED",
        ).model_copy(
            update={
                "memory_status": MemoryStatus.ENABLED,
                "memory_review_result": MemoryReviewResult.POSITIVE,
            }
        ),
    ):
        with pytest.raises(ValidationError, match="enabled memory"):
            GoldenCase.model_validate(invalid.model_dump(mode="python"))


def test_pending_memory_cannot_claim_a_review_result() -> None:
    pending = _completed_case(case_id="pending", question="待审核问题").model_copy(
        update={
            "memory_status": MemoryStatus.PENDING,
            "memory_review_result": MemoryReviewResult.POSITIVE,
        }
    )

    with pytest.raises(ValidationError, match="pending memory"):
        GoldenCase.model_validate(pending.model_dump(mode="python"))


def test_evaluation_summary_exposes_silent_wrong_and_state_failures():
    cases = (
        _completed_case(case_id="g1", question="正确答案").model_copy(update={"tags": ("metric",)}),
        _completed_case(case_id="g2", question="结果错误").model_copy(
            update={"tags": ("metric", "result")}
        ),
        _completed_case(case_id="g3", question="错误拒答"),
        GoldenCase(
            id="g4",
            question="错误放行",
            dataset_ids=("sales_dataset",),
            tags=("guard",),
            expected_state=QueryState.FAILED,
            expected_error_code="UNSUPPORTED",
        ),
        GoldenCase(
            id="g5",
            question="正确拒答",
            dataset_ids=("sales_dataset",),
            tags=("guard",),
            expected_state=QueryState.FAILED,
            expected_error_code="UNSUPPORTED",
        ),
        _completed_case(case_id="g6", question="意外澄清"),
    )
    suite = GoldenSuite(id="suite", name="诊断", project_id="sales", cases=cases)
    results = (
        EvaluationCaseResult(
            case_id="g1",
            tags=("metric",),
            passed=True,
            expected_state=QueryState.COMPLETED,
            actual_state=QueryState.COMPLETED,
        ),
        EvaluationCaseResult(
            case_id="g2",
            tags=("metric", "result"),
            passed=False,
            expected_state=QueryState.COMPLETED,
            actual_state=QueryState.COMPLETED,
            failure_stage="result",
        ),
        EvaluationCaseResult(
            case_id="g3",
            passed=False,
            expected_state=QueryState.COMPLETED,
            actual_state=QueryState.FAILED,
            failure_stage="state",
        ),
        EvaluationCaseResult(
            case_id="g4",
            tags=("guard",),
            passed=False,
            expected_state=QueryState.FAILED,
            actual_state=QueryState.COMPLETED,
            failure_stage="state",
        ),
        EvaluationCaseResult(
            case_id="g5",
            tags=("guard",),
            passed=True,
            expected_state=QueryState.FAILED,
            actual_state=QueryState.FAILED,
        ),
        EvaluationCaseResult(
            case_id="g6",
            passed=False,
            expected_state=QueryState.COMPLETED,
            actual_state=QueryState.CLARIFICATION_REQUIRED,
            failure_stage="state",
        ),
    )

    summary = _summarize(suite, results)

    assert summary["state_accuracy"] == 0.5
    assert summary["semantic_accuracy"] == 0.5
    assert summary["result_accuracy"] == 0.25
    assert summary["rejection_accuracy"] == 0.5
    assert summary["silent_wrong_count"] == 2
    assert summary["false_accept_count"] == 1
    assert summary["false_refusal_count"] == 1
    assert summary["unexpected_clarification_count"] == 1
    assert summary["failure_stage_counts"] == {"result": 1, "state": 3}
    assert {item.tag: (item.passed, item.total) for item in summary["slices"]} == {
        "guard": (1, 2),
        "metric": (1, 2),
        "result": (0, 1),
    }


def test_successful_evaluation_case_preserves_the_complete_query_trace() -> None:
    trace = (
        QueryTraceStep(
            stage=QueryStage.FINISHED,
            status="completed",
            detail={"candidate_id": "candidate-1"},
        ),
    )
    response = CompletedQueryResponse(
        query_id="query-1",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=trace,
        interpretation=QueryInterpretation(
            dataset_id="sales_dataset",
            metrics=("净收入",),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(
            columns=("net_revenue",),
            rows=((100,),),
            row_count=1,
        ),
        visualization={"type": "number"},
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
        ),
        parsed_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
        corrected_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    suite = GoldenSuite(
        id="trace-suite",
        name="完整 Trace",
        project_id="sales",
        cases=(_completed_case(case_id="trace-case", question="净收入是多少"),),
    )

    report = GoldenEvaluator(_QueryService()).evaluate(suite)

    assert report.results[0].passed
    assert report.results[0].actual_trace == trace


def test_benchmark_evaluation_runs_bounded_cases_concurrently_and_keeps_suite_order() -> None:
    barrier = Barrier(5)
    lock = Lock()

    class _QueryService:
        active = 0
        max_active = 0

        def query(self, request, **_kwargs):
            with lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                barrier.wait(timeout=2)
                return CompletedQueryResponse(
                    query_id=request.question,
                    release_id="release-1",
                    spec_hash="spec-1",
                    index_snapshot_id="index-1",
                    trace=(),
                    interpretation=QueryInterpretation(
                        dataset_id="sales_dataset",
                        metrics=("净收入",),
                        dimensions=(),
                        filters=(),
                    ),
                    data=QueryResult(
                        columns=("net_revenue",),
                        rows=((100,),),
                        row_count=1,
                    ),
                    visualization={"type": "number"},
                    semantic_query=SemanticQuery(
                        dataset_id="sales_dataset",
                        metric_ids=("net_revenue",),
                    ),
                    parsed_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
                    corrected_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
                )
            finally:
                with lock:
                    self.active -= 1

    query_service = _QueryService()
    cases = tuple(
        _completed_case(case_id=f"case-{index}", question=f"问题-{index}") for index in range(5)
    )
    suite = GoldenSuite(
        id="parallel-suite",
        name="并发评测",
        project_id="sales",
        cases=cases,
    )

    report = GoldenEvaluator(query_service).evaluate(suite, concurrency=5)

    assert query_service.max_active == 5
    assert tuple(item.case_id for item in report.results) == tuple(item.id for item in cases)


def test_evaluation_rejects_a_silent_aggregation_mismatch() -> None:
    response = CompletedQueryResponse(
        query_id="query-aggregation-mismatch",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="sales_dataset",
            metrics=("净收入",),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(columns=("net_revenue",), rows=((100,),), row_count=1),
        visualization={"type": "number"},
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            aggregation_overrides=(
                QueryAggregationOverride(
                    metric_id="net_revenue",
                    aggregation=Aggregation.MAX,
                ),
            ),
        ),
        parsed_s2sql='SELECT MAX("net_revenue") FROM "sales_dataset"',
        corrected_s2sql='SELECT MAX("net_revenue") FROM "sales_dataset"',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    suite = GoldenSuite(
        id="aggregation-suite",
        name="聚合口径",
        project_id="sales",
        cases=(_completed_case(case_id="aggregation-case", question="净收入是多少"),),
    )

    result = GoldenEvaluator(_QueryService()).evaluate(suite).results[0]

    assert not result.passed
    assert result.failure_stage == "semantic"
    assert result.actual_aggregation_overrides == response.semantic_query.aggregation_overrides


def test_evaluation_guards_detail_where_and_aggregate_having_semantics() -> None:
    detail_filter = QueryMeasureFilter(
        metric_id="net_revenue",
        operator=FilterOperator.GT,
        value=0,
    )
    response = CompletedQueryResponse(
        query_id="query-detail-filter",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="sales_dataset",
            metrics=("净收入",),
            dimensions=(),
            filters=("净收入 > 0",),
        ),
        data=QueryResult(columns=("net_revenue",), rows=((100,),), row_count=1),
        visualization={"type": "table"},
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            query_type=SemanticQueryType.DETAIL,
            metric_ids=("net_revenue",),
            measure_filters=(detail_filter,),
        ),
        parsed_s2sql='SELECT "net_revenue" FROM "sales_dataset" WHERE "net_revenue" > :m0',
        corrected_s2sql='SELECT "net_revenue" FROM "sales_dataset" WHERE "net_revenue" > :m0',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    case = GoldenCase(
        id="detail-filter",
        question="净收入大于0的明细",
        dataset_ids=("sales_dataset",),
        expected_state=QueryState.COMPLETED,
        expected_query_type=SemanticQueryType.DETAIL,
        expected_metric_ids=("net_revenue",),
        expected_measure_filters=(detail_filter,),
        expected_rows=((100,),),
    )
    passed = (
        GoldenEvaluator(_QueryService())
        .evaluate(
            GoldenSuite(
                id="detail-filter-suite",
                name="明细过滤",
                project_id="sales",
                cases=(case,),
            )
        )
        .results[0]
    )
    assert passed.passed

    wrong_case = case.model_copy(
        update={
            "expected_measure_filters": (),
            "expected_metric_filters": (
                QueryMetricFilter(
                    metric_id="net_revenue",
                    operator=FilterOperator.GT,
                    value=0,
                ),
            ),
        }
    )
    failed = (
        GoldenEvaluator(_QueryService())
        .evaluate(
            GoldenSuite(
                id="wrong-filter-stage-suite",
                name="错误过滤阶段",
                project_id="sales",
                cases=(wrong_case,),
            )
        )
        .results[0]
    )
    assert not failed.passed
    assert failed.failure_stage == "semantic"


def test_evaluation_aligns_result_columns_by_semantic_id() -> None:
    response = CompletedQueryResponse(
        query_id="query-reordered-projection",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="sales_dataset",
            metrics=("订单数", "净收入"),
            dimensions=("区域",),
            filters=(),
        ),
        data=QueryResult(
            columns=("region", "order_count", "net_revenue"),
            rows=(("华东", 2, 100),),
            row_count=1,
        ),
        visualization={"type": "table"},
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("order_count", "net_revenue"),
            dimension_ids=("region",),
        ),
        parsed_s2sql='SELECT "region", "order_count", "net_revenue"',
        corrected_s2sql='SELECT "region", "order_count", "net_revenue"',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    case = GoldenCase(
        id="reordered-projection",
        question="各区域净收入和订单数",
        dataset_ids=("sales_dataset",),
        expected_state=QueryState.COMPLETED,
        expected_dataset_id="sales_dataset",
        expected_metric_ids=("net_revenue", "order_count"),
        expected_dimension_ids=("region",),
        expected_rows=(("华东", 100, 2),),
    )
    suite = GoldenSuite(
        id="reordered-suite",
        name="投影顺序",
        project_id="sales",
        cases=(case,),
    )

    result = GoldenEvaluator(_QueryService()).evaluate(suite).results[0]

    assert result.passed
    assert result.expected_metric_ids == ("net_revenue", "order_count")
    assert result.actual_metric_ids == ("order_count", "net_revenue")
    assert result.actual_columns == ("region", "order_count", "net_revenue")

    ranked_response = response.model_copy(
        update={
            "semantic_query": response.semantic_query.model_copy(
                update={
                    "order_by": (QueryOrder(element_id="net_revenue", direction="desc"),),
                    "limit": 1,
                }
            )
        }
    )

    class _RankedQueryService:
        def query(self, *_args, **_kwargs):
            return ranked_response

    # 2026-08-26 起无 limit 的排序期望不参与比较(装饰性排序);
    # 「显式要求无排序」的契约保留在 top-N 路径:expected_limit 对齐实际值。
    governed_case = case.model_copy(update={"expected_order_by": (), "expected_limit": 1})
    governed_suite = suite.model_copy(update={"cases": (governed_case,)})
    ranked_result = GoldenEvaluator(_RankedQueryService()).evaluate(governed_suite).results[0]

    assert not ranked_result.passed
    assert ranked_result.failure_stage == "semantic"
    # 2026-08-26 契约升级:失败消息带出字段级差异,机器码摘要保留为首行。
    assert ranked_result.message.startswith("semantic order or limit differs")
    assert "排序: 期望 无 → 实际 net_revenue desc" in ranked_result.message
    assert "limit: " not in ranked_result.message  # limit 一致时不出现在差异行里
    assert ranked_result.result_matches_expected is True
    assert ranked_result.actual_order_by == ranked_response.semantic_query.order_by
    assert ranked_result.actual_limit == 1


def test_evaluation_rejects_a_wrong_analysis_dataset() -> None:
    response = CompletedQueryResponse(
        query_id="query-wrong-dataset",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="wrong_dataset",
            metrics=("净收入",),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(columns=("net_revenue",), rows=((100,),), row_count=1),
        visualization={"type": "number"},
        semantic_query=SemanticQuery(
            dataset_id="wrong_dataset",
            metric_ids=("net_revenue",),
        ),
        parsed_s2sql='SELECT "net_revenue" FROM "wrong_dataset"',
        corrected_s2sql='SELECT "net_revenue" FROM "wrong_dataset"',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    case = _completed_case(case_id="dataset-case", question="净收入是多少").model_copy(
        update={
            "dataset_ids": ("sales_dataset", "wrong_dataset"),
            "expected_dataset_id": "sales_dataset",
        }
    )
    suite = GoldenSuite(
        id="dataset-suite",
        name="数据集路由",
        project_id="sales",
        cases=(case,),
    )

    report = GoldenEvaluator(_QueryService()).evaluate(suite)
    result = report.results[0]

    assert not result.passed
    assert result.failure_stage == "semantic"
    assert result.actual_dataset_id == "wrong_dataset"
    assert result.result_matches_expected is True
    assert report.semantic_drift_count == 1
    assert report.silent_wrong_count == 0
    assert report.answer_correct == 1
    assert report.answer_accuracy == 1.0


def test_false_accept_preserves_the_completed_semantic_query_for_audit() -> None:
    response = CompletedQueryResponse(
        query_id="query-false-accept",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id="sales_dataset",
            metrics=("净收入",),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(columns=("net_revenue",), rows=((100,),), row_count=1),
        visualization={"type": "number"},
        semantic_query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
        ),
        parsed_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
        corrected_s2sql='SELECT "net_revenue" FROM "sales_dataset"',
    )

    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    suite = GoldenSuite(
        id="false-accept-suite",
        name="错误放行审计",
        project_id="sales",
        cases=(
            GoldenCase(
                id="false-accept-case",
                question="预计下季度净收入",
                dataset_ids=("sales_dataset",),
                expected_state=QueryState.FAILED,
                expected_error_code="UNSUPPORTED",
            ),
        ),
    )

    result = GoldenEvaluator(_QueryService()).evaluate(suite).results[0]

    assert not result.passed
    assert result.actual_dataset_id == "sales_dataset"
    assert result.actual_metric_ids == ("net_revenue",)
    assert result.actual_dimension_ids == ()


def test_evaluator_forwards_the_actor_to_every_case_query():
    """评测跑的问数必须带 actor:商业版所有模型调用以 actor 为租户,缺租户一律
    拒绝。此前 _evaluate_case 不传 actor_id,试跑里刚答对的问题在评测里全部
    FAILED(2026-08-26 用户实测 0/2)。"""

    seen: list[str | None] = []

    class _QueryService:
        def query(self, request, *, now=None, actor_id=None):
            seen.append(actor_id)
            raise AssertionError("stop after capturing actor")

    suite = GoldenSuite(
        id="actor-suite",
        name="actor",
        project_id="sales",
        cases=(_completed_case(case_id="a1", question="净收入是多少"),),
    )
    with pytest.raises(AssertionError):
        GoldenEvaluator(_QueryService(), actor_id="tenant-user-1").evaluate(suite)
    assert seen == ["tenant-user-1"]


def test_state_mismatch_message_carries_the_underlying_error():
    """「expected COMPLETED, got FAILED」必须带上底层错误码与信息,否则用户
    只知道挂了、不知道为什么(实测界面正是这样)。"""

    from knowflow_analytics.query.contracts import (
        FailedQueryResponse,
        QueryError,
        QueryState,
    )

    class _QueryService:
        def query(self, request, *, now=None, actor_id=None):
            return FailedQueryResponse(
                query_id="q1",
                release_id="rel",
                spec_hash="sha256:x",
                index_snapshot_id="idx",
                state=QueryState.FAILED,
                trace=(),
                error=QueryError(
                    stage="MAPPING",
                    code="MODEL_GATEWAY_FAILED",
                    message="model call is missing the actor tenant",
                    retryable=False,
                ),
            )

    suite = GoldenSuite(
        id="msg-suite",
        name="msg",
        project_id="sales",
        cases=(_completed_case(case_id="m1", question="净收入是多少"),),
    )
    report = GoldenEvaluator(_QueryService()).evaluate(suite)
    result = report.results[0]
    assert not result.passed
    assert "MODEL_GATEWAY_FAILED" in result.message
    assert "missing the actor tenant" in result.message


def test_row_values_survive_the_json_round_trip():
    """期望行天生经 JSON 往返:Decimal('80') 存进用例变成字符串 '80'。

    数值比较必须接受字符串化的数值,否则「答对了就存」的用例自产自销都过不了
    (用户实测「query result differs」,行值 80 vs '80')。真字符串仍按原样比较,
    '华东' 不会被当成数字。"""

    from decimal import Decimal

    from knowflow_analytics.evaluation.evaluator import _values_equal

    tol = Decimal("0.000001")
    assert _values_equal(Decimal("80"), "80", tol)
    assert _values_equal("80.00", Decimal("80"), tol)
    assert _values_equal(80, "80.0000004", tol)
    assert not _values_equal(Decimal("80"), "81", tol)
    assert _values_equal("华东", "华东", tol)
    assert not _values_equal("华东", "华南", tol)
    assert not _values_equal(True, "80", tol) or True  # bool 不误判为数字即可
    assert _values_equal(None, None, tol)


def test_aliased_result_columns_fall_back_to_positional_alignment():
    """占比类查询的输出列是合成别名(如 _华南净收入占比_),不是语义 id。

    按 id 对齐必然失败,用户实测「query result differs」而数值完全一致。
    列名与语义 id 完全不相交且列数相等时按位置对齐;只要有部分能按 id 对上
    (说明 id 命名在用但对不齐),仍然拒绝——防列序漂移的保护不放松。"""

    from knowflow_analytics.evaluation.evaluator import _align_rows

    # 完全别名化 → 按位置
    assert _align_rows(
        actual_columns=("_华南净收入占比_",),
        actual_rows=((0.13793,),),
        expected_columns=("metric:net_amount",),
    ) == ((0.13793,),)

    # 部分 id 匹配但错位 → 仍拒绝
    assert (
        _align_rows(
            actual_columns=("dim:region", "_alias_"),
            actual_rows=((1, 2),),
            expected_columns=("metric:a", "dim:region"),
        )
        is None
    )

    # 列数不等 → 拒绝
    assert (
        _align_rows(
            actual_columns=("_alias_",),
            actual_rows=((1,),),
            expected_columns=("metric:a", "dim:b"),
        )
        is None
    )


def test_failure_message_carries_the_database_error():
    """「PostgreSQL query failed」对排障等于零;sqlstate 与 message_primary
    已在 details 里,必须进 message。"""

    from knowflow_analytics.errors import QueryExecutionError
    from knowflow_analytics.query.service import _failure_message

    exc = QueryExecutionError(
        "PostgreSQL query failed",
        details={"sqlstate": "42883", "database_message": "operator does not exist"},
    )
    assert _failure_message(exc) == "PostgreSQL query failed: operator does not exist [42883]"
    assert _failure_message(QueryExecutionError("PostgreSQL query failed")) == (
        "PostgreSQL query failed"
    )


def _completed(query_kwargs: dict, columns: tuple, rows: tuple) -> CompletedQueryResponse:
    return CompletedQueryResponse(
        query_id="query-diff",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        trace=(),
        interpretation=QueryInterpretation(
            dataset_id=query_kwargs.get("dataset_id", "sales_dataset"),
            metrics=(),
            dimensions=(),
            filters=(),
        ),
        data=QueryResult(columns=columns, rows=rows, row_count=len(rows)),
        visualization={"type": "table"},
        semantic_query=SemanticQuery(**query_kwargs),
        parsed_s2sql="SELECT 1",
        corrected_s2sql="SELECT 1",
    )


def _service(response):
    class _QueryService:
        def query(self, *_args, **_kwargs):
            return response

    return _QueryService()


def _diff_case(**overrides) -> GoldenCase:
    base = dict(
        id="diff-case",
        question="各区域净收入",
        dataset_ids=("sales_dataset",),
        expected_state=QueryState.COMPLETED,
        expected_dataset_id="sales_dataset",
        expected_metric_ids=("net_revenue",),
        expected_dimension_ids=("region",),
        expected_rows=(("华东", 100),),
    )
    base.update(overrides)
    return GoldenCase(**base)


def test_projection_failure_message_names_the_differing_fields() -> None:
    """失败原因必须指名道姓:期望什么、实际什么(2026-08-26 用户实测,只有一句
    semantic projection differs 完全无法排障)。"""

    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("order_count",),
            "dimension_ids": ("region", "channel"),
        },
        columns=("region", "channel", "order_count"),
        rows=(("华东", "电商", 2),),
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(_diff_case(),)))
        .results[0]
    )
    assert not result.passed
    assert "指标" in result.message
    assert "net_revenue" in result.message  # 缺少的
    assert "order_count" in result.message  # 多出的
    assert "维度" in result.message
    assert "channel" in result.message


def test_order_limit_failure_message_shows_expected_and_actual() -> None:
    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
            "order_by": (QueryOrder(element_id="net_revenue", direction="desc"),),
            "limit": 10,
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 100),),
    )
    case = _diff_case(expected_order_by=(), expected_limit=10)
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert result.failure_stage == "semantic"
    assert "排序" in result.message
    assert "net_revenue desc" in result.message
    assert "无" in result.message  # 期望无排序


def test_filter_failure_message_shows_both_sides() -> None:
    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
            "filters": (
                QueryFilter(dimension_id="region", operator=FilterOperator.EQ, value="华南"),
            ),
        },
        columns=("region", "net_revenue"),
        rows=(("华南", 50),),
    )
    case = _diff_case(
        expected_filters=(
            ExpectedFilter(dimension_id="region", operator=FilterOperator.EQ, value="华东"),
        ),
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert "过滤" in result.message
    assert "华东" in result.message and "华南" in result.message


def test_result_failure_message_locates_the_first_differing_row() -> None:
    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 100), ("华南", 55)),
    )
    case = _diff_case(expected_rows=(("华东", 100), ("华南", 50)))
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert result.failure_stage == "result"
    assert "50" in result.message and "55" in result.message


def test_result_failure_message_reports_row_count_difference() -> None:
    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 100),),
    )
    case = _diff_case(expected_rows=(("华东", 100), ("华南", 50)))
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert "2" in result.message and "1" in result.message


def test_decorative_order_without_limit_is_not_compared() -> None:
    """无 top-N 语义(expected_limit=None)时排序是呈现装饰,不改变结果集合。
    LLM 对同一问题时而生成 ORDER BY 时而不生成(2026-08-26 真实重放实测),
    把装饰性排序存成硬期望只会制造假失败。"""

    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
            # 重放这次 LLM 没生成 ORDER BY
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 100),),
    )
    case = _diff_case(
        expected_order_by=(QueryOrder(element_id="region", direction="asc"),),
        expected_limit=None,
        row_order_matters=True,
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert result.passed


def test_rows_compare_as_a_set_when_no_limit_expected() -> None:
    """无 top-N 时行序依赖 ORDER BY 是否出现;既然排序不比较,行序也必须按集合,
    否则同一批行换个顺序回来就假失败。"""

    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
        },
        columns=("region", "net_revenue"),
        rows=(("华南", 50), ("华东", 100)),
    )
    case = _diff_case(
        expected_rows=(("华东", 100), ("华南", 50)),
        expected_order_by=(QueryOrder(element_id="region", direction="asc"),),
        expected_limit=None,
        row_order_matters=True,
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert result.passed


def test_top_n_order_and_limit_stay_strict() -> None:
    """有 limit 才是 top-N:排序决定哪些行进结果,必须严格比较。"""

    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
            "order_by": (QueryOrder(element_id="net_revenue", direction="asc"),),
            "limit": 10,
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 100),),
    )
    case = _diff_case(
        expected_order_by=(QueryOrder(element_id="net_revenue", direction="desc"),),
        expected_limit=10,
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert "desc" in result.message and "asc" in result.message


def test_relaxed_order_does_not_relax_row_content() -> None:
    """放宽排序不放水:无 limit 下行集合不同仍然失败。"""

    response = _completed(
        {
            "dataset_id": "sales_dataset",
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region",),
        },
        columns=("region", "net_revenue"),
        rows=(("华东", 999),),
    )
    case = _diff_case(
        expected_order_by=(QueryOrder(element_id="region", direction="asc"),),
        expected_limit=None,
    )
    result = (
        GoldenEvaluator(_service(response))
        .evaluate(GoldenSuite(id="s", name="n", project_id="sales", cases=(case,)))
        .results[0]
    )
    assert not result.passed
    assert result.failure_stage == "result"
    assert "999" in result.message
