from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    DatasetTimeDefaultConfig,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    QueryAggregationOverride,
    QueryResult,
    SemanticQuery,
    SemanticQueryType,
)
from knowflow_analytics.errors import QueryExecutionError
from knowflow_analytics.query.contracts import (
    MapMode,
    QueryRequest,
    QueryState,
    StructuredQueryRequest,
)
from knowflow_analytics.query.corrector import LlmSqlCorrector
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.multi_turn import MultiTurnRewriter, QueryHistoryTurn
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _CapturingExecutor:
    def __init__(self) -> None:
        self.last_query = None

    def execute(self, *, query, release):
        self.last_query = query
        return QueryResult(columns=("region", "net_revenue"), rows=(("华东", 300),), row_count=1)


def test_query_request_rejects_duplicate_dataset_scope() -> None:
    with pytest.raises(ValueError, match="dataset scope must be unique"):
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset", "sales_dataset"),
        )


def test_query_request_bounds_dataset_scope_cardinality() -> None:
    with pytest.raises(ValueError):
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=tuple(f"scope-{index}" for index in range(101)),
        )


class _FailingExecutor:
    def execute(self, **_kwargs):
        raise QueryExecutionError(
            "PostgreSQL query failed",
            details={"sqlstate": "42703", "database_message": "column does not exist"},
        )


class _HallucinatingModelGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "bad first attempt",
            "sql": 'SELECT "未知指标" FROM "销售经营"',
        }


class _CountModelGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "按 S2SQL 生成 COUNT 聚合",
            "sql": 'SELECT COUNT("退款金额") FROM "销售经营"',
        }


class _DetailMeasureModelGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "筛选退款明细",
            "sql": ('SELECT "退款金额" FROM "销售经营" WHERE "退款金额" > 0'),
        }


class _FixedS2SqlGateway:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def generate_json(self, **_kwargs):
        return {"thought": "使用受治理分析函数", "sql": self.sql}


class _AverageCorrectionGateway:
    def generate_json(self, **_kwargs):
        return {
            "opinion": "negative",
            "sql": 'SELECT AVG("净收入") FROM "销售经营"',
        }


class _ConstantEmbeddingGateway:
    def encode(self, texts):
        return EmbeddingBatch(
            model_id="constant",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


class _ExplodingOrchestrator:
    corrector_registry = ()

    def discover(self, **_kwargs):
        raise RuntimeError("unexpected parser failure")


class _ReplacingPhysicalSqlCorrector:
    registry = ("LLMPhysicalSqlCorrector",)
    enabled_correctors = ("LLMPhysicalSqlCorrector",)

    def __init__(self) -> None:
        self.original_sql = None

    def correct(self, *, query, **_kwargs):
        self.original_sql = query.sql
        return query.model_copy(update={"sql": f"{query.sql} /* optimized */"})


class _MultiTurnGateway:
    def __init__(self) -> None:
        self.purposes: list[str] = []

    def generate_json(self, **kwargs):
        purpose = kwargs["purpose"]
        self.purposes.append(purpose)
        if purpose == "analytics.multi_turn_rewrite":
            return {"rewritten_question": "华东地区的净收入是多少？"}
        assert "华东地区的净收入是多少？" in kwargs["messages"][-1]["content"]
        return {
            "thought": "沿用上一轮净收入指标，并增加华东过滤",
            "sql": ('SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\''),
        }


class _RecordingGateway:
    """只记录调用目的，不对问句做断言——用于"不该改写"的场景。"""

    def __init__(self) -> None:
        self.purposes: list[str] = []

    def generate_json(self, **kwargs):
        self.purposes.append(kwargs["purpose"])
        return {
            "thought": "按区域汇总净收入",
            "sql": 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
        }


class _QueryHistory:
    def __init__(self, previous: QueryHistoryTurn | None = None) -> None:
        self.previous = previous
        self.saved: list[QueryHistoryTurn] = []

    def last_success(self, **_kwargs):
        return self.previous

    def save_success(self, turn: QueryHistoryTurn, **_kwargs) -> None:
        self.saved.append(turn)


def test_semantically_invalid_llm_s2sql_fails_in_translator_without_rule_resurrection(
    sales_release,
    sales_index,
):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_HallucinatingModelGateway()),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入 Top 10",
            dataset_ids=("sales_dataset",),
            include_debug_sql=True,
            include_diagnostics=True,
        )
    )

    assert response.state is QueryState.FAILED
    assert response.error.code == "LLM_S2SQL_AST_INVALID"
    assert response.diagnostics is not None
    assert response.diagnostics.category == "final_parsing"
    assert [item["event"] for item in response.trace[-1].detail["parse_events"]] == [
        "final_mapping",
        "llm_candidate",
        "llm_candidate_rejected",
        "all_mapping",
        # 第一趟候选被拒的原因先带给模型，再跑 ALL 那趟。
        "retry_feedback",
        "all_candidate",
        "all_candidate_rejected",
    ]
    assert executor.last_query is None


def test_structured_query_bypasses_mapping_and_executes_governed_semantic_ids(
    sales_release,
    sales_index,
):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query_structured(
        StructuredQueryRequest(
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
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert response.semantic_query.dimension_ids == ("region",)
    assert response.physical_sql is not None
    assert executor.last_query is not None
    assert [item.stage.value for item in response.trace] == [
        "PRECHECK",
        "S2SQL_CORRECTING",
        "ROUTE_BINDING",
        "TRANSLATING",
        "PHYSICAL_SQL_VALIDATING",
        "EXECUTING",
        "POST_PROCESSING",
        "FINISHED",
    ]


def test_multi_turn_rewrite_runs_before_candidate_discovery(
    sales_release,
    sales_index,
) -> None:
    gateway = _MultiTurnGateway()
    previous_mapping = SemanticMapper().map(
        question="各区域净收入是多少？",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    history = _QueryHistory(
        QueryHistoryTurn(
            question="各区域净收入是多少？",
            effective_question="各区域净收入是多少？",
            corrected_s2sql=('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'),
            mapping=previous_mapping,
            dataset_id="sales_dataset",
            release_id="release_sales_v1",
            spec_hash="fixture-v1",
            index_snapshot_id=sales_index.id,
        )
    )
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(gateway),
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        multi_turn_rewriter=MultiTurnRewriter(gateway, enabled=True),
        query_history=history,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="华东呢？",
            dataset_ids=("sales_dataset",),
            conversation_id="conversation-1",
        ),
        actor_id="user-1",
    )

    assert response.state is QueryState.COMPLETED
    # 改写在解析之前：先 rewrite，后 s2sql。
    assert gateway.purposes == ["analytics.multi_turn_rewrite", "analytics.s2sql"]
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert len(history.saved) == 1
    assert history.saved[0].question == "华东呢？"
    assert history.saved[0].effective_question == "华东地区的净收入是多少？"


def test_purely_referential_follow_up_is_rewritten_instead_of_failing_to_map(
    sales_release,
    sales_index,
) -> None:
    """「那环比呢」映射不到任何语义对象，改写必须仍然发生。

    改写原本在候选选中之后，等于让"当前问句自己能选出作用域"成为前置条件——
    纯指代型追问在 CANDIDATE_DISCOVERY 就抛 NO_SEMANTIC_MAPPING，永远走不到
    改写。与上游 NL2SQLParser.rewriteMultiTurn 同序后（映射之前、按会话取上一轮、
    当前映射只用于拼 Prompt），这类追问才能继承上一轮口径。
    """

    gateway = _MultiTurnGateway()
    previous_mapping = SemanticMapper().map(
        question="各区域净收入是多少？",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    history = _QueryHistory(
        QueryHistoryTurn(
            question="各区域净收入是多少？",
            effective_question="各区域净收入是多少？",
            corrected_s2sql=('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'),
            mapping=previous_mapping,
            dataset_id="sales_dataset",
            release_id="release_sales_v1",
            spec_hash="fixture-v1",
            index_snapshot_id=sales_index.id,
        )
    )
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(gateway),
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        multi_turn_rewriter=MultiTurnRewriter(gateway, enabled=True),
        query_history=history,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            # 这句话里没有任何受治理成员的说法。
            question="那呢",
            dataset_ids=("sales_dataset",),
            conversation_id="conversation-1",
        ),
        actor_id="user-1",
    )

    assert response.state is QueryState.COMPLETED
    assert gateway.purposes == ["analytics.multi_turn_rewrite", "analytics.s2sql"]
    assert history.saved[0].effective_question == "华东地区的净收入是多少？"


class _DimensionAddingGateway:
    """改写把上一轮的分组维度塞进了一个只带过滤值的追问。"""

    def __init__(self) -> None:
        self.purposes: list[str] = []
        self.s2sql_questions: list[str] = []

    def generate_json(self, **kwargs):
        purpose = kwargs["purpose"]
        self.purposes.append(purpose)
        if purpose == "analytics.multi_turn_rewrite":
            return {"rewritten_question": "华东各渠道的净收入是多少？"}
        self.s2sql_questions.append(kwargs["messages"][-1]["content"])
        return {
            "thought": "华东的净收入",
            "sql": 'SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\'',
        }


def _previous_turn(sales_index) -> QueryHistoryTurn:
    return QueryHistoryTurn(
        question="各区域净收入是多少？",
        effective_question="各区域净收入是多少？",
        corrected_s2sql='SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
        mapping=SemanticMapper().map(
            question="各区域净收入是多少？",
            dataset_id="sales_dataset",
            index=sales_index,
            mode=MapMode.STRICT,
        ),
        dataset_id="sales_dataset",
        release_id="release_sales_v1",
        spec_hash="fixture-v1",
        index_snapshot_id=sales_index.id,
    )


def _final_parsing_detail(response) -> dict:
    return next(step for step in response.trace if step.stage.value == "FINAL_PARSING").detail


def test_a_complete_question_is_not_rewritten_even_with_history(
    sales_release,
    sales_index,
) -> None:
    """原话自带指标 → 不改写（实机：「各门店上个月销售额是多少」被续上了「其中太古里店
    的销售额占比」，一个完整的问题被上一轮的目标改写）。"""

    gateway = _RecordingGateway()
    history = _QueryHistory(_previous_turn(sales_index))
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(), llm_parser=LlmS2SqlParser(gateway)
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        multi_turn_rewriter=MultiTurnRewriter(gateway, enabled=True),
        query_history=history,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入是多少？",
            dataset_ids=("sales_dataset",),
            conversation_id="conversation-1",
            include_diagnostics=True,
        ),
        actor_id="user-1",
    )

    assert response.state is QueryState.COMPLETED
    # 改写模型根本没被调用。
    assert gateway.purposes == ["analytics.s2sql"]
    assert history.saved[0].effective_question == "各区域净收入是多少？"
    assert _final_parsing_detail(response)["multi_turn_gate"] == "own_metric"


def test_a_rewrite_that_adds_a_grouping_dimension_falls_back_to_the_raw_question(
    sales_release,
    sales_index,
) -> None:
    """改写新增了原话没有的分组维度 → 作废，按原话继续（实机：「2024 年开业的门店
    有多少家」被续上「各城市」）。第二道门只做词表匹配，不调向量模型。"""

    gateway = _DimensionAddingGateway()
    history = _QueryHistory(_previous_turn(sales_index))
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(), llm_parser=LlmS2SqlParser(gateway)
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        multi_turn_rewriter=MultiTurnRewriter(gateway, enabled=True),
        query_history=history,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="华东呢？",
            dataset_ids=("sales_dataset",),
            conversation_id="conversation-1",
            include_diagnostics=True,
        ),
        actor_id="user-1",
    )

    assert response.state is QueryState.COMPLETED
    assert gateway.purposes == ["analytics.multi_turn_rewrite", "analytics.s2sql"]
    # 生成 S2SQL 时看到的是原话，不是带「各渠道」的改写。
    assert "华东呢？" in gateway.s2sql_questions[-1]
    assert "各渠道" not in gateway.s2sql_questions[-1]
    assert history.saved[0].effective_question == "华东呢？"
    assert _final_parsing_detail(response)["multi_turn_gate"] == "added_dimension"


def test_follow_up_rewrite_skips_a_previous_scope_outside_the_allowed_range(
    sales_release,
    sales_index,
) -> None:
    """上一轮的作用域不在本次允许范围内时不改写。

    Prompt 会带上该作用域里的受治理成员名；超出授权范围就不是"补上下文"，
    而是越权展示。这时回退原问句，按原问句正常处理。
    """

    gateway = _RecordingGateway()
    previous_mapping = SemanticMapper().map(
        question="各区域净收入是多少？",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    history = _QueryHistory(
        QueryHistoryTurn(
            question="各区域净收入是多少？",
            effective_question="各区域净收入是多少？",
            corrected_s2sql=('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'),
            mapping=previous_mapping,
            dataset_id="retired_dataset",
            release_id="release_sales_v1",
            spec_hash="fixture-v1",
            index_snapshot_id=sales_index.id,
        )
    )
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(gateway),
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        multi_turn_rewriter=MultiTurnRewriter(gateway, enabled=True),
        query_history=history,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入是多少？",
            dataset_ids=("sales_dataset",),
            conversation_id="conversation-1",
        ),
        actor_id="user-1",
    )

    assert response.state is QueryState.COMPLETED
    # 没有改写调用，直接进解析。
    assert gateway.purposes == ["analytics.s2sql"]


@pytest.mark.parametrize(
    ("question", "s2sql", "sql_marker"),
    (
        (
            "按月净收入同比",
            (
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
                'RATIO_OVER("净收入") AS "同比" FROM "销售经营" '
                "GROUP BY DATE_TRUNC('month', \"下单日期\")"
            ),
            "1 year",
        ),
        (
            "按月净收入环比",
            (
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
                'RATIO_ROLL("净收入") AS "环比" FROM "销售经营" '
                "GROUP BY DATE_TRUNC('month', \"下单日期\")"
            ),
            "1 month",
        ),
        (
            "各区域净收入占比",
            ('SELECT "区域", RATIO_TO_TOTAL("净收入") AS "占比" FROM "销售经营" GROUP BY "区域"'),
            "OVER ()",
        ),
    ),
)
def test_natural_language_analytic_functions_execute_governed_s2sql(
    sales_release,
    question: str,
    s2sql: str,
    sql_marker: str,
) -> None:
    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    release = sales_release.model_copy(update={"datasets": (dataset,)})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_FixedS2SqlGateway(s2sql)),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question=question,
            dataset_ids=("sales_dataset",),
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert sql_marker.upper() in response.physical_sql.upper()


def test_unexpected_failure_preserves_the_bound_semantic_revision(
    sales_release,
    sales_index,
):
    provider = _ReleaseProvider(sales_release, sales_index)
    service = AnalyticsQueryService(
        releases=provider,
        orchestrator=_ExplodingOrchestrator(),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert response.release_id == provider.published.release.id
    assert response.spec_hash == provider.published.release.spec_hash
    assert response.index_snapshot_id == provider.published.index_snapshot.id
    assert response.error.code == "INTERNAL_ERROR"


def test_unexpected_failure_exposes_a_safe_internal_diagnosis_when_requested(
    sales_release,
    sales_index,
):
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=_ExplodingOrchestrator(),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.FAILED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "internal"
    assert response.diagnostics.stage == "CANDIDATE_DISCOVERY"
    assert response.trace[-1].stage.value == "CANDIDATE_DISCOVERY"
    assert response.trace[-1].status == "failed"
    assert "unexpected parser failure" not in response.model_dump_json()


def test_unknown_question_fails_without_calling_database(sales_release, sales_index):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="帮我看看明天的天气",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert response.error.code == "NO_SEMANTIC_MAPPING"
    assert executor.last_query is None


def test_failed_mapping_diagnostics_keep_every_mapper_attempt(sales_release, sales_index):
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="帮我看看明天的天气",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.FAILED
    failed = response.trace[-1]
    assert failed.stage.value == "CANDIDATE_DISCOVERY"
    assert [item["mode"] for item in failed.detail["mapping_attempts"]] == [
        "strict",
        "moderate",
        "loose",
    ]


def test_modeling_preview_diagnostics_restore_every_completed_stage(
    sales_release,
    sales_index,
) -> None:
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "success"
    discovery = next(item for item in response.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["mapping_attempts"][0]["matches"]
    parsing = next(item for item in response.trace if item.stage.value == "FINAL_PARSING")
    assert parsing.detail["parsed_s2sql"]
    translating = next(item for item in response.trace if item.stage.value == "TRANSLATING")
    assert "SELECT" in translating.detail["physical_sql"]


def test_diagnostics_cannot_bypass_the_debug_sql_gate(sales_release, sales_index) -> None:
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=False,
        )
    )

    translating = next(item for item in response.trace if item.stage.value == "TRANSLATING")
    correcting = next(
        item for item in response.trace if item.stage.value == "PHYSICAL_SQL_CORRECTING"
    )
    assert "physical_sql" not in translating.detail
    assert "original_physical_sql" not in correcting.detail
    assert "corrected_physical_sql" not in correcting.detail
    assert response.physical_sql is None


def test_failed_execution_keeps_physical_sql_and_safe_database_diagnosis(
    sales_release,
    sales_index,
) -> None:
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_FailingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.FAILED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "database_execution"
    assert response.diagnostics.summary == "数据库拒绝执行生成的只读 SQL"
    failed = response.trace[-1]
    assert failed.stage.value == "EXECUTING"
    assert failed.detail["sqlstate"] == "42703"
    translating = next(item for item in response.trace if item.stage.value == "TRANSLATING")
    assert "SELECT" in translating.detail["physical_sql"]


def test_diagnostic_trace_keeps_the_final_sql_after_physical_correction(
    sales_release,
    sales_index,
) -> None:
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        physical_sql_corrector=_ReplacingPhysicalSqlCorrector(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    correcting = next(
        item for item in response.trace if item.stage.value == "PHYSICAL_SQL_CORRECTING"
    )
    assert correcting.detail["sql_changed"] is True
    assert "optimized" not in correcting.detail["original_physical_sql"]
    assert "optimized" in correcting.detail["corrected_physical_sql"]


def test_llm_s2sql_count_is_executed_without_python_count_inference(
    sales_release,
    sales_index,
):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_CountModelGateway()),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="退款金额共有几笔",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert 'COUNT("退款金额")' in response.parsed_s2sql
    assert executor.last_query is not None


def test_natural_language_query_runs_physical_corrector_after_translation(
    sales_release,
    sales_index,
):
    executor = _CapturingExecutor()
    physical_corrector = _ReplacingPhysicalSqlCorrector()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        physical_sql_corrector=physical_corrector,
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入",
            dataset_ids=("sales_dataset",),
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert physical_corrector.original_sql is not None
    assert executor.last_query.sql.endswith("/* optimized */")
    assert response.physical_sql == executor.last_query.sql
    assert [item.stage.value for item in response.trace] == [
        "PRECHECK",
        "CANDIDATE_DISCOVERY",
        "FINAL_PARSING",
        "S2SQL_CORRECTING",
        "ROUTE_BINDING",
        "TRANSLATING",
        "PHYSICAL_SQL_CORRECTING",
        "PHYSICAL_SQL_VALIDATING",
        "EXECUTING",
        "POST_PROCESSING",
        "FINISHED",
    ]
    physical_step = next(
        item for item in response.trace if item.stage.value == "PHYSICAL_SQL_CORRECTING"
    )
    assert physical_step.detail == {
        "registry": ["LLMPhysicalSqlCorrector"],
        "enabled": ["LLMPhysicalSqlCorrector"],
        "sql_changed": True,
    }


def test_enabled_s2sql_corrector_runs_before_translation(sales_release, sales_index):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_CountModelGateway()),
            textual_corrector=TextualS2SqlCorrector(
                llm_sql_corrector=LlmSqlCorrector(
                    _AverageCorrectionGateway(),
                    enabled=True,
                )
            ),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入平均值",
            dataset_ids=("sales_dataset",),
            include_debug_sql=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.parsed_s2sql == 'SELECT COUNT("退款金额") FROM "销售经营"'
    assert response.corrected_s2sql == 'SELECT AVG("净收入") FROM "销售经营"'
    assert "AVG" in response.physical_sql
    s2sql_step = next(item for item in response.trace if item.stage.value == "S2SQL_CORRECTING")
    assert s2sql_step.detail == {
        "registry": ["RuleSqlCorrector", "LLMSqlCorrector"],
        "enabled": ["LLMSqlCorrector"],
        "query_rule_ids": [],
    }


def test_analysis_word_is_not_misclassified_as_grouping(sales_release, sales_index):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="帮我分析净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert response.semantic_query.dimension_ids == ()


def test_detail_measure_filter_is_explained_with_business_names(
    sales_release,
    sales_index,
):
    class _DetailExecutor:
        def execute(self, *, query, release):
            return QueryResult(
                columns=tuple(item.element_id for item in query.columns),
                rows=((5,), (10,)),
                row_count=2,
            )

    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_DetailMeasureModelGateway()),
        ),
        translator=SemanticTranslator(),
        executor=_DetailExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="退款金额大于0的有哪些",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.query_type is SemanticQueryType.DETAIL
    assert response.interpretation.query_type is SemanticQueryType.DETAIL
    assert response.interpretation.metrics == ("退款金额",)
    assert response.interpretation.dimensions == ()
    assert response.interpretation.filters == ("退款金额 > 0",)
    assert response.visualization == {
        "type": "table",
        "x": None,
        "series": None,
        "x_time": False,
        "x_grain": None,
        "y": ("refund_amount",),
        "y_units": [None],
        "y_formats": ["number"],
    }


def test_grouping_phrase_starting_with_fen_is_still_supported(sales_release, sales_index):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="分区域统计净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert response.semantic_query.dimension_ids == ("region",)


def test_same_surface_mapping_asks_instead_of_silently_taking_the_sorted_candidate(
    sales_release,
    sales_index,
):
    """「净收入」同时是 net_revenue 的名称和 order_count 的别名（priority 300 > 默认 100）。

    此前按排序静默取胜者 —— 用户敲的是 net_revenue 的本名，拿到的却是
    order_count。建模期诊断 DATASET_SEMANTIC_NAME_AMBIGUOUS 早已承诺"问数时可能需要
    澄清"，问数侧必须兑现。
    """

    order_count_index = next(
        index
        for index, entry in enumerate(sales_index.entries)
        if entry.element_id == "order_count"
    )
    conflicting_entry = sales_index.entries[order_count_index].model_copy(
        update={
            "id": "entry_conflicting_revenue",
            "phrase": "净收入",
            "normalized_phrase": "净收入",
            "source": "alias",
            "priority": 300,
        }
    )
    ambiguous_index = sales_index.model_copy(
        update={
            "id": "idx_ambiguous",
            "entries": (*sales_index.entries, conflicting_entry),
            "vectors": (*sales_index.vectors, sales_index.vectors[order_count_index]),
        }
    )
    releases = _ReleaseProvider(sales_release, ambiguous_index)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=releases,
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.CLARIFICATION_REQUIRED
    assert {(option.element_type, option.element_id) for option in response.options} == {
        ("metric", "net_revenue"),
        ("metric", "order_count"),
    }
    # 没有执行任何 SQL：不能先算出一个数再问。
    assert executor.last_query is None


def test_picking_one_of_the_same_name_metrics_completes_without_asking_again(
    sales_release,
    sales_index,
):
    """用户点选 net_revenue 后，同一个问题必须直接算出结果，不能再问一遍。"""

    order_count_index = next(
        index
        for index, entry in enumerate(sales_index.entries)
        if entry.element_id == "order_count"
    )
    conflicting_entry = sales_index.entries[order_count_index].model_copy(
        update={
            "id": "entry_conflicting_revenue",
            "phrase": "净收入",
            "normalized_phrase": "净收入",
            "source": "alias",
            "priority": 300,
        }
    )
    ambiguous_index = sales_index.model_copy(
        update={
            "id": "idx_ambiguous",
            "entries": (*sales_index.entries, conflicting_entry),
            "vectors": (*sales_index.vectors, sales_index.vectors[order_count_index]),
        }
    )
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, ambiguous_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )
    clarification = service.query(
        QueryRequest(project_id="sales", question="净收入", dataset_ids=("sales_dataset",))
    )
    assert clarification.state is QueryState.CLARIFICATION_REQUIRED
    selected = next(
        option for option in clarification.options if option.element_id == "net_revenue"
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
            selected_candidate_id=selected.candidate_id,
            expected_release_id=clarification.release_id,
            expected_spec_hash=clarification.spec_hash,
            expected_index_snapshot_id=clarification.index_snapshot_id,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert executor.last_query is not None


def test_multiple_query_scopes_require_confirmation_instead_of_picking_first(
    sales_release,
):
    """隐藏 Analysis Topic 后，请求默认会扫描全部内部 QueryScope。

    同一句话若在两个不同事实作用域都能形成候选，按排序取 candidates[0]
    会把作用域选择变成不可见的在线语义推断。发现阶段必须先让用户确认；
    确认的 candidate id 再沿原 textual-S2SQL 流水线执行。
    """

    original = sales_release.datasets[0].model_copy(
        update={
            "default_time_dimension_id": "order_date",
            "aggregate_time_default": DatasetTimeDefaultConfig(
                unit=1,
                period="DAY",
                time_mode="LAST",
            ),
        }
    )
    alternate = original.model_copy(
        update={
            "id": "alternate_sales_scope",
            "name": "备用销售经营",
            "biz_name": "alternate_sales_scope",
        }
    )
    release = sales_release.model_copy(update={"datasets": (original, alternate)})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    clarification = service.query(
        QueryRequest(project_id="sales", question="净收入"),
        now=datetime(2026, 8, 27, 23, 59, tzinfo=UTC),
    )

    assert clarification.state is QueryState.CLARIFICATION_REQUIRED
    assert clarification.options == ()
    assert "重新发布" in clarification.question
    assert executor.last_query is None


def test_unique_exact_metric_scope_excludes_a_value_only_scope(
    sales_release,
):
    """Reviewed Scope admission contract at CANDIDATE_DISCOVERY.

    A country/region value can be reachable from several facts, but a scope that
    only grounds that filter cannot compete with the sole scope that exactly
    grounds the requested governed metric.
    """

    primary = sales_release.datasets[0]
    value_only = primary.model_copy(
        update={
            "id": "region_value_only_scope",
            "name": "仅区域范围",
            "biz_name": "region_value_only_scope",
            "metric_ids": (),
            "dimension_ids": ("region",),
        }
    )
    release = sales_release.model_copy(update={"datasets": (primary, value_only)})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="华东净收入",
            include_diagnostics=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.dataset_id == "sales_dataset"
    discovery = next(item for item in response.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["scope_admission"] == {
        "rule": "unique_exact_metric_match",
        "discovered_dataset_ids": ["sales_dataset", "region_value_only_scope"],
        "admitted_dataset_ids": ["sales_dataset"],
    }
    assert executor.last_query is not None


def test_scope_confirmation_precedes_ambiguity_inside_one_scope(sales_release):
    conflicting_metric = next(
        item for item in sales_release.metrics if item.id == "refund_amount"
    ).model_copy(update={"name": "净收入", "aliases": ()})
    metrics = tuple(
        conflicting_metric if item.id == conflicting_metric.id else item
        for item in sales_release.metrics
    )
    ambiguous = sales_release.datasets[0].model_copy(
        update={"id": "ambiguous_scope", "name": "含同名指标的范围"}
    )
    unambiguous = sales_release.datasets[0].model_copy(
        update={
            "id": "unambiguous_scope",
            "name": "唯一指标范围",
            "metric_ids": ("net_revenue",),
        }
    )
    release = sales_release.model_copy(
        update={"metrics": metrics, "datasets": (ambiguous, unambiguous), "terms": ()}
    )
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    scopes = service.query(QueryRequest(project_id="sales", question="净收入"))

    assert scopes.state is QueryState.CLARIFICATION_REQUIRED
    assert scopes.options == ()
    assert "重新发布" in scopes.question


def test_multiple_values_of_one_dimension_form_an_in_filter(
    sales_release,
):
    values = tuple(
        value.model_copy(update={"aliases": (*value.aliases, "大区")})
        for value in sales_release.dimension_values
    )
    release = sales_release.model_copy(update={"dimension_values": values})
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="大区净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.semantic_query.dimension_ids == ("region",)
    assert [
        (item.dimension_id, item.operator.value, item.value)
        for item in response.semantic_query.filters
    ] == [("region", "in", ("华东", "华南"))]


def test_selected_time_dimension_survives_final_correction(sales_release):
    shipped_field = FieldSpec(
        id="orders.shipped_date",
        model_id="orders",
        name="发货日期",
        column="shipped_date",
        data_type="date",
        kind=FieldKind.TIME,
    )
    shipped_dimension = DimensionSpec(
        id="shipped_date",
        name="发货日期",
        model_id="orders",
        field_id=shipped_field.id,
        semantic_type="time",
    )
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
                shipped_dimension.id,
            ),
        }
    )
    release = sales_release.model_copy(
        update={
            "fields": (*sales_release.fields, shipped_field),
            "dimensions": (*sales_release.dimensions, shipped_dimension),
            "datasets": (dataset,),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=dataset.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
            ),
        }
    )
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    clarification = service.query(
        QueryRequest(
            project_id="sales",
            question="本季度净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert clarification.state is QueryState.CLARIFICATION_REQUIRED
    assert {(item.element_type, item.element_id) for item in clarification.options} == {
        ("dimension", "order_date"),
        ("dimension", "shipped_date"),
    }
    selected = next(item for item in clarification.options if item.element_id == "order_date")

    resolved = service.query(
        QueryRequest(
            project_id="sales",
            question="本季度净收入",
            dataset_ids=("sales_dataset",),
            selected_candidate_id=selected.candidate_id,
            expected_release_id=clarification.release_id,
            expected_spec_hash=clarification.spec_hash,
            expected_index_snapshot_id=clarification.index_snapshot_id,
        )
    )

    assert resolved.state is QueryState.COMPLETED
    assert {item.dimension_id for item in resolved.semantic_query.filters} == {"order_date"}


@pytest.mark.parametrize(
    "question",
    [
        "净收入增长了多少",
        "预计下季度净收入",
    ],
)
def test_unsupported_analysis_is_rejected_instead_of_silently_querying_total(
    sales_release,
    sales_index,
    question,
):
    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question=question,
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert response.error.code == "UNSUPPORTED_ANALYTIC_OPERATION"
    assert executor.last_query is None
    # 拒绝理由必须原样到用户面前：套用阶段通用文案会退化成「没能理解这个问题」，
    # 用户既不知道为什么被拒，也拿不到可照做的替代说法。
    assert response.diagnostics is not None
    assert response.diagnostics.user_hint == response.error.message


class _OrFilterModelGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "两个区域取并集",
            "sql": (
                'SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\' OR "区域" = \'华南\''
            ),
        }


def test_or_filter_warns_that_the_interpretation_is_incomplete(
    sales_release,
    sales_index,
):
    """An OR filter executes correctly but has no QueryFilter projection. The
    user must not be shown a filter-free interpretation of a filtered query."""

    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_OrFilterModelGateway()),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="华东和华南的净收入",
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
        )
    )

    assert response.state is QueryState.COMPLETED
    assert response.interpretation.filters == ()
    assert response.diagnostics is not None
    assert response.diagnostics.category == "translation"
    assert response.diagnostics.severity == "warning"
    assert "语义解释" in response.diagnostics.summary


class _ExplainingExecutor:
    """Executor that supports the optional EXPLAIN pre-flight contract."""

    def __init__(self, *, explain_error: Exception | None = None) -> None:
        self.explained: list[object] = []
        self.executed: list[object] = []
        self._explain_error = explain_error

    def explain(self, *, query, release):
        self.explained.append(query)
        if self._explain_error is not None:
            raise self._explain_error
        return {"Plan": {"Node Type": "Aggregate"}}

    def execute(self, *, query, release):
        self.executed.append(query)
        return QueryResult(columns=("net_revenue",), rows=((300,),), row_count=1)


def _dry_run_service(sales_release, sales_index, executor):
    return AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_NetRevenueGateway()),
        ),
        translator=SemanticTranslator(),
        executor=executor,
        dry_run_before_execute=True,
    )


class _NetRevenueGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "汇总净收入",
            "sql": 'SELECT SUM("净收入") FROM "销售经营"',
        }


def test_dry_run_validates_the_plan_before_executing(sales_release, sales_index):
    executor = _ExplainingExecutor()

    response = _dry_run_service(sales_release, sales_index, executor).query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert len(executor.explained) == 1
    assert len(executor.executed) == 1


def test_dry_run_failure_prevents_execution(sales_release, sales_index):
    executor = _ExplainingExecutor(
        explain_error=QueryExecutionError(
            "PostgreSQL query planning failed",
            details={"sqlstate": "42703"},
        )
    )

    response = _dry_run_service(sales_release, sales_index, executor).query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert executor.executed == []


def test_dry_run_is_skipped_when_the_executor_cannot_explain(sales_release, sales_index):
    """A plain executor must keep working; the pre-flight is optional."""

    executor = _CapturingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_NetRevenueGateway()),
        ),
        translator=SemanticTranslator(),
        executor=executor,
        dry_run_before_execute=True,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED
    assert executor.last_query is not None


def test_failures_always_carry_an_attribution_diagnosis(sales_release, sales_index):
    """A production failure is reported once. Without a diagnosis the operator
    cannot tell mapping from parsing from execution, and the request is gone."""

    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_HallucinatingModelGateway()),
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="各区域净收入 Top 10",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "final_parsing"


def test_execution_failure_carries_a_database_diagnosis(sales_release, sales_index):
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(_NetRevenueGateway()),
        ),
        translator=SemanticTranslator(),
        executor=_FailingExecutor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.FAILED
    assert response.diagnostics is not None
    assert response.diagnostics.category == "database_execution"
