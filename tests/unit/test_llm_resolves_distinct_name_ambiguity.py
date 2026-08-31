from __future__ import annotations

import pytest

from knowflow_analytics.contracts import FilterOperator, QueryFilter, SemanticQuery
from knowflow_analytics.query.ambiguity import (
    SemanticDecisionObligation,
    SemanticValueBinding,
    settle_after_parse,
)
from knowflow_analytics.query.contracts import (
    ClarificationOption,
    MapMode,
    MappingResult,
    MatchMethod,
    QueryRequest,
    QueryState,
    SchemaMatch,
    SemanticAmbiguityGroup,
    SemanticAmbiguityMember,
)
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    SemanticElementType,
    SemanticIndexBuilder,
)
from tests.unit.test_query_failures_are_recorded import _CapturingFailures, _ReleaseProvider
from tests.unit.test_query_service import _CapturingExecutor


class _ConstantEmbeddingGateway:
    def encode(self, texts):
        return EmbeddingBatch(model_id="t", dimension=1, vectors=tuple((1.0,) for _ in texts))


class _ChoosingGateway:
    """The final LLM writes whichever metric names it is told to."""

    def __init__(self, *metric_names: str) -> None:
        self.metric_names = metric_names
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        projection = ", ".join(f'SUM("{name}")' for name in self.metric_names)
        return {"thought": "按来源模型判断", "sql": f'SELECT {projection} FROM "销售经营"'}


@pytest.fixture
def crash_release(sales_release):
    """两个指标异名但共享尾缀「人数」：生还人数 / 遇难人数。"""

    renamed = {"net_revenue": "生还人数", "refund_amount": "遇难人数"}
    return sales_release.model_copy(
        update={
            "spec_hash": "crash-release",
            "metrics": tuple(
                m.model_copy(update={"name": renamed[m.id], "aliases": ()})
                if m.id in renamed
                else m
                for m in sales_release.metrics
            ),
        }
    )


def _service(release, gateway, failures=None):
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    return AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(), llm_parser=LlmS2SqlParser(gateway)
        ),
        translator=SemanticTranslator(),
        executor=_CapturingExecutor(),
        query_failures=failures,
    )


def _ask(service, **overrides):
    return service.query(
        QueryRequest(
            project_id="sales",
            question="事故人数是多少",
            dataset_ids=("sales_dataset",),
            **overrides,
        )
    )


def test_the_llm_decides_between_distinct_names_and_the_answer_says_so(crash_release):
    """「人数」同时命中生还人数和遇难人数。昨天的规则对这种情况也直接问人，
    但 LLM 明明分得清「空难」更接近「遇难」——它只是从没被问到。
    现在放行给 LLM，并把它的选择随答案明示，附带可切换的另一项。"""

    gateway = _ChoosingGateway("遇难人数")
    response = _ask(_service(crash_release, gateway))

    assert response.state is QueryState.COMPLETED, response
    assert gateway.calls == 1
    assert response.interpretation.metrics == ("遇难人数",)
    (resolved,) = response.resolved_by_llm
    assert resolved.detected_text == "人数"
    assert (resolved.chosen.element_type, resolved.chosen.element_id) == (
        "metric",
        "refund_amount",
    )
    assert resolved.chosen.label == "遇难人数"
    assert [(o.element_type, o.element_id) for o in resolved.alternatives] == [
        ("metric", "net_revenue")
    ]


def test_a_hedging_llm_that_uses_both_is_sent_back_to_the_user(crash_release):
    """LLM 两个都算，等于没有裁决：问一次比悄悄给两列安全。"""

    response = _ask(_service(crash_release, _ChoosingGateway("生还人数", "遇难人数")))

    assert response.state is QueryState.CLARIFICATION_REQUIRED, response
    assert {(o.element_type, o.element_id) for o in response.options} == {
        ("metric", "net_revenue"),
        ("metric", "refund_amount"),
    }
    assert "人数" in response.question
    assert response.trace[-1].stage.value == "FINAL_PARSING"


def test_switching_the_llm_choice_is_kept_as_alias_evidence(crash_release):
    """用户点了另一项：这句话里「人数」指生还人数。这是别名候选，要进同一张
    失败问句表，而不是散落在日志里。"""

    failures = _CapturingFailures()
    gateway = _ChoosingGateway("遇难人数")
    service = _service(crash_release, gateway, failures)
    initial = _ask(service)
    selected = next(
        option
        for option in initial.resolved_by_llm[0].alternatives
        if option.element_id == "net_revenue"
    )
    gateway.metric_names = ("生还人数",)
    response = _ask(
        service,
        selected_candidate_id=selected.candidate_id,
        expected_release_id=initial.release_id,
        expected_spec_hash=initial.spec_hash,
        expected_index_snapshot_id=initial.index_snapshot_id,
    )

    assert response.state is QueryState.COMPLETED, response
    assert response.resolved_by_llm == ()
    (record, _actor, project_id) = failures.saved[-1]
    assert project_id == "sales"
    assert record.code == "SEMANTIC_ELEMENT_SELECTED"
    assert record.details == {"selected_element_id": "net_revenue"}
    assert "生还人数" in record.message


def test_same_name_members_still_ask_before_any_model_call(sales_release):
    """两个都叫「净收入」时 SUM("净收入") 回指不到唯一 ID，LLM 没法表达选择。"""

    release = sales_release.model_copy(
        update={
            "spec_hash": "same-name",
            "metrics": tuple(
                m.model_copy(update={"name": "净收入", "aliases": ()})
                if m.id in {"net_revenue", "refund_amount"}
                else m
                for m in sales_release.metrics
            ),
        }
    )
    gateway = _ChoosingGateway("净收入")
    response = _service(release, gateway).query(
        QueryRequest(project_id="sales", question="净收入是多少", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.CLARIFICATION_REQUIRED
    assert gateway.calls == 0
    assert response.trace[-1].stage.value == "CANDIDATE_DISCOVERY"


class _FilteringGateway:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        return {"thought": "按维度值过滤", "sql": self.sql}


def test_a_dimension_value_in_the_group_never_poisons_the_count(sales_release):
    """「东区」既是维度值 华东 的别名，也被改成了一个维度名。维度值永远不会出现在
    SemanticQuery 的 ID 里——若把它算进歧义组，LLM 写成 WHERE 区域='华东' 就会被
    判成"一个都没用"而无限追问（review 复现的死循环）。值成员直接剔除，剩下
    不足两个成员就不再是歧义。"""

    release = sales_release.model_copy(
        update={
            "spec_hash": "value-collision",
            "dimensions": tuple(
                d.model_copy(update={"name": "东区", "aliases": ()}) if d.id == "channel" else d
                for d in sales_release.dimensions
            ),
        }
    )
    gateway = _FilteringGateway('SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\'')
    response = _service(release, gateway).query(
        QueryRequest(project_id="sales", question="东区净收入", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.COMPLETED, response
    assert response.resolved_by_llm == ()
    assert response.interpretation.filters == ("区域 = 华东",)


def test_dimension_members_are_settled_the_same_way_as_metrics(sales_release):
    """两个维度共享尾缀「分层」：客户分层 / 渠道分层。LLM 按其中一个分组即裁决成功。"""

    release = sales_release.model_copy(
        update={
            "spec_hash": "dimension-suffix",
            "dimensions": tuple(
                d.model_copy(update={"name": "渠道分层", "aliases": ()}) if d.id == "channel" else d
                for d in sales_release.dimensions
            ),
        }
    )
    gateway = _FilteringGateway(
        'SELECT "客户分层", SUM("净收入") FROM "销售经营" GROUP BY "客户分层"'
    )
    response = _service(release, gateway).query(
        QueryRequest(project_id="sales", question="各分层净收入", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.COMPLETED, response
    (resolved,) = response.resolved_by_llm
    assert resolved.detected_text == "分层"
    assert (resolved.chosen.element_type, resolved.chosen.element_id) == (
        "dimension",
        "customer_segment",
    )
    assert [(o.element_type, o.element_id) for o in resolved.alternatives] == [
        ("dimension", "channel")
    ]


def test_a_member_the_release_cannot_name_fails_closed_before_the_model(crash_release):
    """索引比发布新（组里有发布不认识的 ID）时，LLM 也无法用名字表达选择：
    同名门禁必须把它当成不可区分，而不是放行。"""

    from knowflow_analytics.query.ambiguity import same_name_ambiguity
    from knowflow_analytics.query.contracts import MapMode
    from knowflow_analytics.query.mapper import SemanticMapper as _Mapper

    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(crash_release)
    mapping = _Mapper().map(
        question="事故人数是多少", dataset_id="sales_dataset", index=index, mode=MapMode.MODERATE
    )
    assert mapping.ambiguous_groups == (("net_revenue", "refund_amount"),)
    stale = crash_release.model_copy(
        update={"metrics": tuple(m for m in crash_release.metrics if m.id != "refund_amount")}
    )

    assert same_name_ambiguity(mapping, crash_release) is None
    unresolved = same_name_ambiguity(mapping, stale)
    assert unresolved is not None
    assert unresolved.detected_text == "人数"
    assert tuple(member.element_id for member in unresolved.members) == (
        "net_revenue",
        "refund_amount",
    )


def test_typed_ambiguity_groups_never_backfill_a_same_id_from_another_phrase():
    """IDs are only family-local and cannot identify a phrase-group member.

    ``shared`` is a metric in the “人数” group and a dimension in the “日期”
    group.  Selecting the metric settles only the former; the latter must stay
    unresolved instead of borrowing the metric solely because its bare ID is
    equal.
    """

    def match(element_type, element_id: str, detected_text: str) -> SchemaMatch:
        return SchemaMatch(
            entry_id=f"{element_type.value}:{element_id}:{detected_text}",
            dataset_id="scope",
            element_type=element_type,
            element_id=element_id,
            phrase=detected_text,
            detected_text=detected_text,
            method=MatchMethod.KEYWORD,
            score=0.8,
            priority=300,
        )

    metric_shared = SemanticAmbiguityMember(
        element_type=SemanticElementType.METRIC,
        element_id="shared",
    )
    dimension_shared = SemanticAmbiguityMember(
        element_type=SemanticElementType.DIMENSION,
        element_id="shared",
    )
    metric_other = SemanticAmbiguityMember(
        element_type=SemanticElementType.METRIC,
        element_id="other_metric",
    )
    dimension_other = SemanticAmbiguityMember(
        element_type=SemanticElementType.DIMENSION,
        element_id="other_dimension",
    )
    mapping = MappingResult(
        dataset_id="scope",
        mode=MapMode.MODERATE,
        normalized_question="人数和日期",
        matches=(
            match(SemanticElementType.METRIC, "shared", "人数"),
            match(SemanticElementType.METRIC, "other_metric", "人数"),
            match(SemanticElementType.DIMENSION, "shared", "日期"),
            match(SemanticElementType.DIMENSION, "other_dimension", "日期"),
        ),
        ambiguous_groups=(("shared", "other_metric"), ("shared", "other_dimension")),
        semantic_ambiguity_groups=(
            SemanticAmbiguityGroup(
                detected_text="人数",
                members=(metric_shared, metric_other),
            ),
            SemanticAmbiguityGroup(
                detected_text="日期",
                members=(dimension_shared, dimension_other),
            ),
        ),
        config_version="test",
    )

    def options_for(group: SemanticAmbiguityGroup):
        return tuple(
            ClarificationOption(
                candidate_id=f"element:{member.element_type.value}:{member.element_id}",
                label=f"{member.element_type.value}:{member.element_id}",
                description="",
                dataset_id="scope",
            )
            for member in group.members
        )

    settlement = settle_after_parse(
        mapping,
        SemanticQuery(dataset_id="scope", metric_ids=("shared",)),
        options_for,
    )

    assert len(settlement.resolved) == 1
    assert settlement.resolved[0].detected_text == "人数"
    # 指标 "shared" 不得回填同名的维度组：若回填，「日期」组会被判成已裁决并
    # 记入 resolved。2026-08-28 起「一个成员都没用」本身不再是澄清（没做选择
    # 就没有选错），所以这里断言的是它没有被当作某个成员的裁决结果。
    assert settlement.unresolved is None
    assert all(item.detected_text != "日期" for item in settlement.resolved)


def test_an_unused_ambiguity_group_is_not_a_clarification(sales_release):
    """LLM 一个成员都没用 = 没做选择 = 没有选错的风险，不该打断用户。

    歧义组存在的目的是防止模型在同名元素间**静默挑一个**；它两个都没用时，
    这个组与本次查询无关。实测两处同形症状：城市/图书馆 q06「各所属城市的
    图书馆面积」——「图书馆」同时命中 图书馆名称/图书馆地址，模型要的是
    城市名称，两个都没用；双11 q03/q04——碎片词「活动」命中 活动日/参加活动
    商家数量，模型同样两个都没用。旧规则把这两种都判成未裁决并澄清。

    「模型什么都表达不出来」是另一回事（退化查询），由它自己的检查负责，
    不该借歧义组的壳来兜。
    """

    metric_a = SemanticAmbiguityMember(
        element_type=SemanticElementType.DIMENSION,
        element_id="region",
    )
    metric_b = SemanticAmbiguityMember(
        element_type=SemanticElementType.DIMENSION,
        element_id="channel",
    )

    def _match(element_id: str) -> SchemaMatch:
        return SchemaMatch(
            entry_id=f"dimension:{element_id}:地区",
            dataset_id="sales_dataset",
            element_type=SemanticElementType.DIMENSION,
            element_id=element_id,
            phrase="地区",
            detected_text="地区",
            method=MatchMethod.KEYWORD,
            score=0.8,
            priority=300,
        )

    mapping = MappingResult(
        dataset_id="sales_dataset",
        mode=MapMode.MODERATE,
        normalized_question="净收入",
        matches=(_match("region"), _match("channel")),
        ambiguous_groups=(("region", "channel"),),
        semantic_ambiguity_groups=(
            SemanticAmbiguityGroup(detected_text="地区", members=(metric_a, metric_b)),
        ),
        config_version="test",
    )

    def options_for(group: SemanticAmbiguityGroup):
        return tuple(
            ClarificationOption(
                candidate_id=f"element:{member.element_type.value}:{member.element_id}",
                label=member.element_id,
                description="",
                dataset_id="sales_dataset",
            )
            for member in group.members
        )

    settlement = settle_after_parse(
        mapping,
        SemanticQuery(dataset_id="sales_dataset", metric_ids=("net_revenue",)),
        options_for,
    )

    assert settlement.unresolved is None
    assert settlement.resolved == ()


@pytest.mark.parametrize(
    ("metric_ids", "is_resolved"),
    [
        ((), False),
        (("net_revenue",), True),
        (("refund_amount",), False),
        (("net_revenue", "refund_amount"), False),
    ],
)
def test_explicit_ai_or_human_choice_must_be_used_exactly_once(
    metric_ids,
    is_resolved,
):
    selected = SemanticAmbiguityMember(
        element_type=SemanticElementType.METRIC,
        element_id="net_revenue",
    )
    other = SemanticAmbiguityMember(
        element_type=SemanticElementType.METRIC,
        element_id="refund_amount",
    )
    group = SemanticAmbiguityGroup(
        detected_text="销售额",
        members=(selected, other),
    )
    mapping = MappingResult(
        dataset_id="sales_dataset",
        mode=MapMode.MODERATE,
        normalized_question="销售额",
        matches=(),
        config_version="test",
    )

    def options_for(_group: SemanticAmbiguityGroup):
        return (
            ClarificationOption(
                candidate_id="sel-net",
                label="净收入",
                description="订单净收入",
                dataset_id="sales_dataset",
                element_type="metric",
                element_id="net_revenue",
            ),
            ClarificationOption(
                candidate_id="sel-refund",
                label="退款金额",
                description="订单退款金额",
                dataset_id="sales_dataset",
                element_type="metric",
                element_id="refund_amount",
            ),
        )

    options = options_for(group)
    obligation = SemanticDecisionObligation(
        detected_text="销售额",
        source="ai",
        selected=selected,
        candidates=group.members,
        chosen_option=options[0],
        options=options,
    )

    settlement = settle_after_parse(
        mapping,
        SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=metric_ids,
            dimension_ids=("region",),
        ),
        options_for,
        obligations=(obligation,),
    )

    if is_resolved:
        assert settlement.unresolved is None
        assert settlement.decisions[0].source == "ai"
        assert settlement.decisions[0].chosen.label == "净收入"
        assert [item.label for item in settlement.decisions[0].alternatives] == ["退款金额"]
    else:
        assert settlement.unresolved == group
        assert settlement.decisions == ()


@pytest.mark.parametrize(
    ("filters", "metric_ids", "is_resolved"),
    [
        ((QueryFilter(dimension_id="region", operator=FilterOperator.EQ, value="华东"),), (), True),
        ((), (), False),
        (
            (QueryFilter(dimension_id="region", operator=FilterOperator.EQ, value="华南"),),
            (),
            False,
        ),
        (
            (QueryFilter(dimension_id="region", operator=FilterOperator.NE, value="华东"),),
            (),
            False,
        ),
        (
            (QueryFilter(dimension_id="region", operator=FilterOperator.EQ, value="华东"),),
            ("net_revenue",),
            False,
        ),
    ],
)
def test_explicit_dimension_value_choice_must_survive_as_the_grounded_filter(
    filters,
    metric_ids,
    is_resolved,
):
    selected = SemanticAmbiguityMember(
        element_type=SemanticElementType.DIMENSION_VALUE,
        element_id="region-east",
    )
    other = SemanticAmbiguityMember(
        element_type=SemanticElementType.METRIC,
        element_id="net_revenue",
    )
    options = (
        ClarificationOption(
            candidate_id="sel-east",
            kind="dimension_value",
            label="区域 = 华东",
            description="区域值",
            dataset_id="sales_dataset",
            element_type="dimension_value",
            element_id="region-east",
        ),
        ClarificationOption(
            candidate_id="sel-revenue",
            kind="metric",
            label="净收入",
            description="订单净收入",
            dataset_id="sales_dataset",
            element_type="metric",
            element_id="net_revenue",
        ),
    )
    obligation = SemanticDecisionObligation(
        detected_text="华东",
        source="human",
        selected=selected,
        candidates=(selected, other),
        chosen_option=options[0],
        options=options,
        value_bindings=(
            SemanticValueBinding(
                element_id="region-east",
                dimension_id="region",
                raw_value="华东",
            ),
        ),
    )

    settlement = settle_after_parse(
        MappingResult(
            dataset_id="sales_dataset",
            mode=MapMode.MODERATE,
            normalized_question="华东",
            matches=(),
            config_version="test",
        ),
        SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=metric_ids,
            dimension_ids=("region",),
            filters=filters,
        ),
        lambda _group: options,
        obligations=(obligation,),
    )

    assert (settlement.unmet_obligation is None) is is_resolved
