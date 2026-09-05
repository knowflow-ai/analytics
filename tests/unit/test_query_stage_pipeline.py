from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DimensionSpec,
    SemanticQueryType,
)
from knowflow_analytics.query.contracts import (
    MapMode,
    MappingResult,
    MatchMethod,
    ParsedSemanticCandidate,
    SchemaMatch,
)
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator, _cross_dataset_sort_key
from knowflow_analytics.query.parser import (
    GOVERNANCE_BLOCKING_S2SQL_CODES,
    LlmS2SqlParser,
    RuleS2SqlParser,
    _LlmS2SqlOutput,
)
from knowflow_analytics.query.s2sql_ast import validate_textual_s2sql
from knowflow_analytics.semantic.index import SemanticElementType

_EXPECTED_GOVERNANCE_BLOCKING_CODES = (
    "LLM_S2SQL_GROUNDED_VALUE_REQUIRED",
    "S2SQL_DEFAULT_COUNT_METRIC_INVALID",
)


class _Gateway:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        return self.payload


class _SequenceGateway:
    def __init__(self, *payloads: dict) -> None:
        self.payloads = payloads
        self.calls = 0

    def generate_json(self, **_kwargs):
        payload = self.payloads[self.calls]
        self.calls += 1
        return payload


class _ParsedLlmCandidate:
    def __init__(self) -> None:
        self.calls = 0
        self.template: ParsedSemanticCandidate | None = None

    def parse(self, **kwargs):
        self.calls += 1
        assert self.template is not None
        return self.template.model_copy(
            update={
                "id": f"llm-{self.calls}",
                "mapping": kwargs["mapping"],
                "map_mode": kwargs["mapping"].mode,
                "parser": "llm",
            }
        )


class _CandidateThenErrorLlmParser:
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        self.calls = 0
        self.template: ParsedSemanticCandidate | None = None

    def parse(self, **kwargs):
        self.calls += 1
        if self.calls == 1 and self.template is not None:
            return self.template.model_copy(
                update={
                    "id": "llm-before-error",
                    "mapping": kwargs["mapping"],
                    "map_mode": kwargs["mapping"].mode,
                    "parser": "llm",
                }
            )
        raise SemanticParsingError("synthetic LLM parser failure", code=self.error_code)


class _RejectLlmCorrector:
    registry = ("SyntheticCorrector",)

    def correct(self, *, candidate, **_kwargs):
        if candidate.parser == "llm":
            raise SemanticParsingError(
                "parsed LLM candidate failed correction",
                code="SYNTHETIC_CORRECTION_FAILED",
            )
        return candidate


class _RuleParserWithoutAllCandidate:
    def __init__(self) -> None:
        self._delegate = RuleS2SqlParser()

    def parse(self, **kwargs):
        if kwargs["mapping"].mode is MapMode.ALL:
            return None
        return self._delegate.parse(**kwargs)


class _RuleParserBlockingOnAll:
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        self._delegate = RuleS2SqlParser()

    def parse(self, **kwargs):
        if kwargs["mapping"].mode is MapMode.ALL:
            raise SemanticParsingError("synthetic ALL Rule block", code=self.error_code)
        return self._delegate.parse(**kwargs)


def _mapping(dataset_id: str = "sales_dataset") -> MappingResult:
    return MappingResult(
        dataset_id=dataset_id,
        mode=MapMode.ALL,
        normalized_question="",
        matches=(),
        config_version="test",
    )


def _candidate(dataset_id: str, *, score: float = 1.0) -> ParsedSemanticCandidate:
    return ParsedSemanticCandidate(
        id=f"candidate-{dataset_id}",
        dataset_id=dataset_id,
        parsed_s2sql=f'SELECT "指标" FROM "{dataset_id}"',
        corrected_s2sql=f'SELECT "指标" FROM "{dataset_id}"',
        query_type=SemanticQueryType.DETAIL,
        score=score,
        map_mode=MapMode.ALL,
        mapping=_mapping(dataset_id),
        parser="rule",
    )


def _release_with_order_primary_dimension(sales_release):
    primary_dimension = DimensionSpec(
        id="order_id",
        name="订单ID",
        model_id="orders",
        field_id="orders.id",
        semantic_type="identifier",
    )
    return sales_release.model_copy(
        update={
            "fields": tuple(
                field.model_copy(update={"identifier_type": "primary"})
                if field.id == "orders.id"
                else field
                for field in sales_release.fields
            ),
            "dimensions": (*sales_release.dimensions, primary_dimension),
            "datasets": (
                sales_release.datasets[0].model_copy(
                    update={
                        "dimension_ids": (
                            *sales_release.datasets[0].dimension_ids,
                            primary_dimension.id,
                        )
                    }
                ),
            ),
        }
    )


def test_llm_contract_and_query_type_follow_textual_s2sql(sales_release):
    properties = _LlmS2SqlOutput.model_json_schema()["properties"]
    # inferred_terms 是给反馈页做术语预填的旁路观察项，不参与查询语义。
    assert set(properties) == {"thought", "sql", "inferred_terms"}

    aggregate = LlmS2SqlParser(
        _Gateway(
            {
                "thought": "汇总",
                "sql": 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
            }
        )
    ).parse(
        question="各区域净收入",
        release=sales_release,
        mapping=_mapping(),
        query_id="aggregate",
    )
    detail = LlmS2SqlParser(
        _Gateway(
            {
                "thought": "明细",
                "sql": 'SELECT "退款金额" FROM "销售经营" WHERE "退款金额" > 0',
            }
        )
    ).parse(
        question="退款金额大于0的有哪些",
        release=sales_release,
        mapping=_mapping(),
        query_id="detail",
    )

    assert aggregate.query_type is SemanticQueryType.AGGREGATE
    assert detail.query_type is SemanticQueryType.DETAIL
    assert not hasattr(aggregate, "semantic_query")


@pytest.mark.parametrize(
    "valid_sql",
    (
        "SELECT 1",
        "SELECT * FROM governed_scope",
        "WITH seed AS (SELECT 1) SELECT * FROM seed",
        "SELECT 1 UNION SELECT 2",
        "SELECT (SELECT 1)",
    ),
)
def test_candidate_admission_keeps_queries_whose_every_select_has_a_projection(valid_sql):
    """The JSQLParser parity check is not a broader SQL AST allowlist."""

    validate_textual_s2sql(valid_sql)


@pytest.mark.parametrize(
    "operator",
    ("UNION", "UNION ALL", "INTERSECT", "EXCEPT"),
)
@pytest.mark.parametrize("empty_side", ("left", "right"))
def test_candidate_admission_rejects_an_empty_select_on_every_set_branch(
    operator,
    empty_side,
):
    invalid_sql = (
        f"SELECT {operator} SELECT 1" if empty_side == "left" else f"SELECT 1 {operator} SELECT"
    )

    with pytest.raises(SemanticParsingError) as raised:
        validate_textual_s2sql(invalid_sql)

    assert raised.value.code == "LLM_S2SQL_AST_INVALID"


def test_prompt_uses_business_names_and_mapped_scope(sales_release, sales_index):
    mapping = SemanticMapper().map(
        question="净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    messages = LlmS2SqlParser._messages(
        "净收入",
        sales_release,
        sales_release.datasets[0],
        mapping,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert "业务名称" in messages[0]["content"]
    assert "语义标识符" not in messages[0]["content"]
    member_names = {
        line.split("|", 1)[0] for line in messages[1]["content"].splitlines() if "|" in line
    }
    assert "净收入" in member_names
    # 2026-08-28 合同修订：最终 LLM 拿到选定 Scope 的全部成员，而不是 Mapper
    # 命中的子集。过滤版让一次漏召回直接等于模型表达不出来——「各图书馆的藏品
    # 数量」召不回实体名维度就丢掉 GROUP BY 返回总数（城市/图书馆 r2 实测）。
    # 未命中的成员照样在 schema 里；「用户的话命中了什么」由 constraints 表达。
    assert "退款金额" in member_names
    assert "净收入" in messages[1]["content"]
    assert "'id':" not in messages[1]["content"]
    assert "rule_seed" not in messages[1]["content"]


def test_prompt_passes_only_mapped_values_and_all_retry_fields(sales_release, sales_index):
    mapped = SemanticMapper().map(
        question="华东净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    mapped_content = LlmS2SqlParser._messages(
        "华东净收入",
        sales_release,
        sales_release.datasets[0],
        mapped,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )[1]["content"]
    assert "raw_value': '华东'" in mapped_content
    assert "华南" not in mapped_content

    all_mapping = SemanticMapper().map(
        question="未知问题",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.ALL,
    )
    all_content = LlmS2SqlParser._messages(
        "未知问题",
        sales_release,
        sales_release.datasets[0],
        all_mapping,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )[1]["content"]
    # 指标/维度以带表头的竖线表给出，每行第一格是业务名。
    member_names = {line.split("|", 1)[0] for line in all_content.splitlines() if "|" in line}
    assert {"净收入", "退款金额", "下单日期"} <= member_names


def test_prompt_keeps_primary_key_partition_time_and_count_binding_outside_mapping(
    sales_release,
    sales_index,
):
    release = _release_with_order_primary_dimension(sales_release)
    dataset = release.datasets[0].model_copy(update={"default_time_dimension_id": "order_date"})
    route = AnalysisTopicRouteSpec(
        dataset_id=dataset.id,
        root_model_id="orders",
        default_count_metric_id="order_count",
    )
    release = release.model_copy(update={"datasets": (dataset,), "analysis_topic_routes": (route,)})
    mapping = SemanticMapper().map(
        question="净收入",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    content = LlmS2SqlParser._messages(
        "净收入",
        release,
        dataset,
        mapping,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )[1]["content"]

    assert "primary_key={'name': '订单ID'}" in content
    assert "partition_time={'name': '下单日期'}" in content
    assert "default_count_metric={'name': '订单数', 'aggregation': 'count_distinct'}" in content


@pytest.mark.parametrize(
    "invalid_sql",
    (
        "SELECT (",
        "SELECT",
        "  select \n",
        "SELECT /* no projection */",
        "SELECT FROM sales",
        "WITH seed AS (SELECT 1) SELECT",
        "WITH seed AS (SELECT) SELECT 1",
        "SELECT (SELECT)",
        "SELECT 1 UNION SELECT",
        "SELECT UNION SELECT 1",
        "SELECT * FROM (SELECT) AS empty_query",
    ),
)
def test_incomplete_llm_sql_never_forms_a_candidate_and_rule_takes_over(
    sales_release,
    sales_index,
    invalid_sql,
):
    """Reviewed candidate-admission parity with JSQLParser 4.9.

    This freezes the LLMSqlParser input boundary, before QueryTypeParser or any
    Corrector runs.  Rejecting an empty SELECT does not invent query meaning: it
    leaves the candidate list empty so the existing RuleSqlParser can run.
    """

    gateway = _Gateway({"thought": "invalid", "sql": invalid_sql})
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=LlmS2SqlParser(
            gateway,
            max_attempts=1,
            self_consistency_number=1,
        ),
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )

    events: list[str] = []
    parsed = orchestrator.final_parse(
        question="各区域净收入",
        query_id="rule-fallback",
        release=sales_release,
        index=sales_index,
        selected=candidates.candidates[0],
        diagnostic_sink=lambda event, _detail: events.append(event),
    )

    assert parsed.parser == "rule"
    assert parsed.map_mode is MapMode.STRICT
    assert parsed.mapping.mode is MapMode.STRICT
    assert gateway.calls == 1
    assert events == [
        "final_mapping",
        "llm_parse_failed",
        "rule_fallback_candidate",
        "selected_candidate",
    ]


def test_all_retry_runs_rule_on_all_mapping_when_all_llm_has_no_candidate(
    sales_release,
    sales_index,
):
    """Reviewed parity: the ALL pass remains LLM_OR_RULE, not LLM-only.

    The first valid LLM candidate is rejected after parsing, so the discovery
    Rule candidate must not be resurrected.  The ALL LLM then produces no
    candidate; only then may RuleSqlParser build a new candidate from ALL mapping.
    """

    gateway = _SequenceGateway(
        {
            "thought": "valid candidate rejected by the complete workflow",
            "sql": 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
        },
        {"thought": "invalid ALL candidate", "sql": "SELECT"},
    )
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=LlmS2SqlParser(
            gateway,
            max_attempts=1,
            self_consistency_number=1,
        ),
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    validations: list[tuple[str, MapMode]] = []
    events: list[str] = []

    def validate(candidate):
        validations.append((candidate.parser, candidate.map_mode))
        if len(validations) == 1:
            raise SemanticParsingError(
                "translator rejected the first complete workflow",
                code="SYNTHETIC_TRANSLATION_FAILED",
            )

    parsed = orchestrator.final_parse(
        question="各区域净收入",
        query_id="all-rule-fallback",
        release=sales_release,
        index=sales_index,
        selected=candidates.candidates[0],
        candidate_validator=validate,
        diagnostic_sink=lambda event, _detail: events.append(event),
    )

    assert parsed.parser == "rule"
    assert parsed.map_mode is MapMode.ALL
    assert parsed.mapping.mode is MapMode.ALL
    assert parsed.id != candidates.candidates[0].id
    assert gateway.calls == 2
    assert validations == [("llm", MapMode.STRICT), ("rule", MapMode.ALL)]
    assert events == [
        "final_mapping",
        "llm_candidate",
        "llm_candidate_rejected",
        "all_mapping",
        # 第一趟候选被拒的原因先带给模型，再跑 ALL 那趟。
        "retry_feedback",
        "all_llm_parse_failed",
        "all_rule_fallback_candidate",
        "selected_candidate",
    ]


def test_all_rule_with_no_semantic_candidate_fails_cleanly(
    sales_release,
    sales_index,
):
    """An empty ALL Rule result is not a candidate and must never become a 500."""

    gateway = _SequenceGateway(
        {
            "thought": "valid candidate rejected by the complete workflow",
            "sql": 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
        },
        {"thought": "invalid ALL candidate", "sql": "SELECT"},
    )
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        rule_parser=_RuleParserWithoutAllCandidate(),
        llm_parser=LlmS2SqlParser(
            gateway,
            max_attempts=1,
            self_consistency_number=1,
        ),
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    validations = 0
    events: list[str] = []

    def validate(_candidate):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise SemanticParsingError(
                "translator rejected the first complete workflow",
                code="SYNTHETIC_TRANSLATION_FAILED",
            )

    with pytest.raises(SemanticParsingError) as raised:
        orchestrator.final_parse(
            question="各区域净收入",
            query_id="empty-all-rule",
            release=sales_release,
            index=sales_index,
            selected=candidates.candidates[0],
            candidate_validator=validate,
            diagnostic_sink=lambda event, _detail: events.append(event),
        )

    assert raised.value.code == "LLM_S2SQL_INVALID"
    assert "all_rule_fallback_empty" in events


def test_governance_blocking_code_registry_is_frozen():
    assert frozenset(_EXPECTED_GOVERNANCE_BLOCKING_CODES) == GOVERNANCE_BLOCKING_S2SQL_CODES


@pytest.mark.parametrize("blocking_code", _EXPECTED_GOVERNANCE_BLOCKING_CODES)
def test_governance_blocking_first_llm_failure_never_runs_rule(
    sales_release,
    sales_index,
    blocking_code,
):
    llm_parser = _CandidateThenErrorLlmParser(blocking_code)
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=llm_parser,
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )

    with pytest.raises(SemanticParsingError) as raised:
        orchestrator.final_parse(
            question="各区域净收入",
            query_id="blocking-first-llm",
            release=sales_release,
            index=sales_index,
            selected=candidates.candidates[0],
        )

    assert raised.value.code == blocking_code
    assert llm_parser.calls == 1


@pytest.mark.parametrize("blocking_code", _EXPECTED_GOVERNANCE_BLOCKING_CODES)
def test_governance_blocking_all_llm_failure_never_runs_all_rule(
    sales_release,
    sales_index,
    blocking_code,
):
    llm_parser = _CandidateThenErrorLlmParser(blocking_code)
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=llm_parser,
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    llm_parser.template = candidates.candidates[0]

    def reject_first_candidate(_candidate):
        raise SemanticParsingError(
            "translator rejected the first complete workflow",
            code="SYNTHETIC_TRANSLATION_FAILED",
        )

    with pytest.raises(SemanticParsingError) as raised:
        orchestrator.final_parse(
            question="各区域净收入",
            query_id="blocking-all-llm",
            release=sales_release,
            index=sales_index,
            selected=candidates.candidates[0],
            candidate_validator=reject_first_candidate,
        )

    assert raised.value.code == blocking_code
    assert llm_parser.calls == 2


@pytest.mark.parametrize("blocking_code", _EXPECTED_GOVERNANCE_BLOCKING_CODES)
def test_governance_blocking_all_rule_failure_is_rethrown(
    sales_release,
    sales_index,
    blocking_code,
):
    llm_parser = _CandidateThenErrorLlmParser("LLM_S2SQL_INVALID")
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        rule_parser=_RuleParserBlockingOnAll(blocking_code),
        llm_parser=llm_parser,
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    llm_parser.template = candidates.candidates[0]

    def reject_first_candidate(_candidate):
        raise SemanticParsingError(
            "translator rejected the first complete workflow",
            code="SYNTHETIC_TRANSLATION_FAILED",
        )

    with pytest.raises(SemanticParsingError) as raised:
        orchestrator.final_parse(
            question="各区域净收入",
            query_id="blocking-all-rule",
            release=sales_release,
            index=sales_index,
            selected=candidates.candidates[0],
            candidate_validator=reject_first_candidate,
        )

    assert raised.value.code == blocking_code
    assert llm_parser.calls == 2


def test_corrector_failure_after_llm_candidate_skips_rule_and_retries_all(
    sales_release,
    sales_index,
):
    llm_parser = _ParsedLlmCandidate()
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=llm_parser,
        textual_corrector=_RejectLlmCorrector(),
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    llm_parser.template = candidates.candidates[0]

    with pytest.raises(SemanticParsingError) as raised:
        orchestrator.final_parse(
            question="各区域净收入",
            query_id="corrector-failure",
            release=sales_release,
            index=sales_index,
            selected=candidates.candidates[0],
        )

    assert raised.value.code == "SYNTHETIC_CORRECTION_FAILED"
    assert llm_parser.calls == 2


def test_translation_failure_after_llm_candidate_triggers_the_all_retry(
    sales_release,
    sales_index,
):
    """Parity: NL2SQLParser retries ALL when the complete doParse state is FAILED."""

    llm_parser = _ParsedLlmCandidate()
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=llm_parser,
    )
    candidates = orchestrator.discover(
        question="各区域净收入",
        release=sales_release,
        index=sales_index,
        dataset_ids=("sales_dataset",),
    )
    llm_parser.template = candidates.candidates[0]
    validations = 0

    def validate(candidate):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise SemanticParsingError(
                "translator rejected selected mapping",
                code="SYNTHETIC_TRANSLATION_FAILED",
            )

    parsed = orchestrator.final_parse(
        question="各区域净收入",
        query_id="translation-failure-all-retry",
        release=sales_release,
        index=sales_index,
        selected=candidates.candidates[0],
        candidate_validator=validate,
    )

    assert parsed.parser == "llm"
    assert llm_parser.calls == 2
    assert validations == 2


def test_rule_candidate_score_matches_type_weighting(
    sales_release,
    sales_index,
):
    mapping = SemanticMapper().map(
        question="区域净收入最高",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    candidate = RuleS2SqlParser().parse(
        question="区域净收入最高",
        release=sales_release,
        mapping=mapping,
    )

    assert candidate is not None
    assert candidate.score == 9.0
    assert 'ORDER BY "净收入" DESC' in candidate.parsed_s2sql
    assert "LIMIT 100" in candidate.parsed_s2sql


def test_rule_top_intent_keeps_the_upstream_topn_function(sales_release, sales_index):
    mapping = SemanticMapper().map(
        question="区域净收入 top",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )
    candidate = RuleS2SqlParser().parse(
        question="区域净收入 top",
        release=sales_release,
        mapping=mapping,
    )

    assert candidate is not None
    assert 'TOPN("净收入")' in candidate.parsed_s2sql


def test_cross_dataset_sort_uses_semantic_parse_comparator():
    metric_only = _candidate("dataset_a", score=100.0).model_copy(
        update={
            "mapping": MappingResult(
                dataset_id="dataset_a",
                mode=MapMode.STRICT,
                normalized_question="收入",
                matches=(
                    SchemaMatch(
                        entry_id="entry_metric_a",
                        dataset_id="dataset_a",
                        element_type=SemanticElementType.METRIC,
                        element_id="metric_a",
                        phrase="收入",
                        detected_text="收入",
                        method=MatchMethod.KEYWORD,
                        score=0.8,
                        priority=300,
                    ),
                ),
                config_version="test",
            )
        }
    )
    dataset_exact = _candidate("dataset_b", score=1.0).model_copy(
        update={
            "mapping": MappingResult(
                dataset_id="dataset_b",
                mode=MapMode.STRICT,
                normalized_question="收入",
                matches=(
                    SchemaMatch(
                        entry_id="entry_dataset_b",
                        dataset_id="dataset_b",
                        element_type=SemanticElementType.DATASET,
                        element_id="dataset_b",
                        phrase="经营分析",
                        detected_text="经营分析",
                        method=MatchMethod.EXACT,
                        score=1.0,
                        priority=400,
                    ),
                ),
                config_version="test",
            )
        }
    )

    assert _cross_dataset_sort_key(dataset_exact) < _cross_dataset_sort_key(metric_only)
