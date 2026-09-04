from __future__ import annotations

from knowflow_analytics.contracts import (
    FilterOperator,
    QueryFilter,
    SemanticQuery,
)
from knowflow_analytics.query.ambiguity import settle_after_parse
from knowflow_analytics.query.contracts import (
    ClarificationOption,
    MapMode,
    MappingResult,
    MatchMethod,
    SchemaMatch,
    SemanticAmbiguityGroup,
    SemanticAmbiguityMember,
)
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser, RuleS2SqlParser
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    SemanticElementType,
    SemanticIndexBuilder,
)


class _ConstantEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="ambiguity-test",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


class _SelectingS2SqlGateway:
    def __init__(self, metric_names: list[str]) -> None:
        self.metric_names = metric_names
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        return {
            "thought": "选择受治理指标",
            "sql": (
                "SELECT "
                + ", ".join(f'SUM("{metric_name}")' for metric_name in self.metric_names)
                + ' FROM "销售经营"'
            ),
        }


def _ambiguous_release(sales_release):
    return sales_release.model_copy(
        update={
            "spec_hash": "ambiguous-release",
            "metrics": tuple(
                metric.model_copy(update={"name": "净收入", "aliases": ()})
                if metric.id in {"net_revenue", "refund_amount"}
                else metric
                for metric in sales_release.metrics
            ),
        }
    )


def test_rule_parser_retains_unresolved_mapper_alternatives(sales_release):
    release = _ambiguous_release(sales_release)
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    mapping = SemanticMapper().map(
        question="净收入是多少",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question="净收入是多少",
        release=release,
        mapping=mapping,
    )

    assert mapping.ambiguous_groups
    assert candidate is not None
    assert candidate.parsed_s2sql.count('SUM("净收入")') == 2


def test_discovery_preserves_same_name_ambiguity_for_scope_first_governance(
    sales_release,
):
    """两个指标都叫「净收入」时，LLM 输出的 SUM("净收入") 无法回指到唯一 ID。

    此前 discover 照常返回候选，final_parse 交给 LLM 静默二选一 —— 用户拿到一个
    看起来正常、可能是退款额的数字。service._semantic_options 早就为同名对象
    补上了"来源模型"区分（它自己的注释写明这是对 MapFilter 的 safer HITL 偏离），
    只是从来没人往里抛。
    """

    release = _ambiguous_release(sales_release)
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    gateway = _SelectingS2SqlGateway(["净收入"])
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=LlmS2SqlParser(gateway),
    )

    candidates = orchestrator.discover(
        question="净收入是多少",
        release=release,
        index=index,
        dataset_ids=("sales_dataset",),
    )

    # Service 先比较 QueryScope，再对选中的 candidate.mapping 做同名澄清；
    # discover 不能提前抛错，否则第一个 scope 会遮住后面的 scope。
    assert candidates.candidates[0].mapping.ambiguous_groups
    assert gateway.calls == 0


def test_a_resolved_selection_goes_through_discovery_and_the_final_llm_parse(sales_release):
    """用户点选其中一个后，mapper 收掉竞争项，同一个问题不再被反复追问。"""

    release = _ambiguous_release(sales_release)
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    gateway = _SelectingS2SqlGateway(["净收入"])
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=LlmS2SqlParser(gateway),
    )

    candidates = orchestrator.discover(
        question="净收入是多少",
        release=release,
        index=index,
        dataset_ids=("sales_dataset",),
        selected_element_id="net_revenue",
    )
    assert gateway.calls == 0
    assert not candidates.candidates[0].mapping.ambiguous_groups

    parsed = orchestrator.final_parse(
        question="净收入是多少",
        query_id="ambiguity-resolved",
        release=release,
        index=index,
        selected=candidates.candidates[0],
    )

    assert parsed.parser == "llm"
    assert gateway.calls == 1


class TestOneWordUsedTwice:
    """同一个说法既落在维度上、又落在另一个维度的取值上，模型两个都用了。

    那不是"选择"，是把一个词消费了两次。混合组此前整个看不见——值成员被剔除后组塌成
    一个成员随即被丢弃——于是没人接管（实测 demo_cafe「哪些门店售卖卡布奇诺」5 次里
    3 次这样答，同一问题给不同的数）。
    """

    @staticmethod
    def _mapping() -> MappingResult:
        return MappingResult(
            dataset_id="sales_dataset",
            mode=MapMode.STRICT,
            normalized_question="哪些门店售卖卡布奇诺",
            config_version="v1",
            matches=(
                SchemaMatch(
                    entry_id="e-store-name",
                    dataset_id="sales_dataset",
                    element_type=SemanticElementType.DIMENSION,
                    element_id="store_name",
                    phrase="门店",
                    detected_text="门店",
                    method=MatchMethod.EXACT,
                    score=1.0,
                    priority=300,
                    detected_spans=((2, 4),),
                ),
                SchemaMatch(
                    entry_id="e-channel-value",
                    dataset_id="sales_dataset",
                    element_type=SemanticElementType.DIMENSION_VALUE,
                    element_id="channel_store",
                    phrase="门店",
                    detected_text="门店",
                    method=MatchMethod.EXACT,
                    score=1.0,
                    priority=300,
                    dimension_id="channel",
                    raw_value="门店",
                    detected_spans=((2, 4),),
                ),
            ),
            semantic_ambiguity_groups=(
                SemanticAmbiguityGroup(
                    detected_text="门店",
                    members=(
                        SemanticAmbiguityMember(
                            element_type=SemanticElementType.DIMENSION,
                            element_id="store_name",
                        ),
                        SemanticAmbiguityMember(
                            element_type=SemanticElementType.DIMENSION_VALUE,
                            element_id="channel_store",
                        ),
                    ),
                ),
            ),
        )

    @staticmethod
    def _options(group):
        return tuple(
            ClarificationOption(
                candidate_id=f"element:{member.element_type.value}:{member.element_id}",
                label=member.element_id,
                description="",
                dataset_id="sales_dataset",
                kind=member.element_type.value,
                element_type=member.element_type.value,
                element_id=member.element_id,
            )
            for member in group.members
        )

    def test_using_both_is_not_settled(self) -> None:
        query = SemanticQuery(
            dataset_id="sales_dataset",
            dimension_ids=("store_name",),
            filters=(
                QueryFilter(dimension_id="channel", operator=FilterOperator.EQ, value="门店"),
            ),
        )

        settlement = settle_after_parse(self._mapping(), query, self._options)

        assert settlement.unresolved is not None
        assert settlement.unresolved.detected_text == "门店"

    def test_using_only_the_dimension_is_settled(self) -> None:
        """按门店分组、不带那条巧合过滤——这才是这个问题想要的。"""

        query = SemanticQuery(dataset_id="sales_dataset", dimension_ids=("store_name",))

        settlement = settle_after_parse(self._mapping(), query, self._options)

        assert settlement.unresolved is None

    def test_using_only_the_value_is_settled(self) -> None:
        """「门店渠道的销售金额」——只按渠道过滤，同样是做了一个选择。"""

        query = SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            filters=(
                QueryFilter(dimension_id="channel", operator=FilterOperator.EQ, value="门店"),
            ),
        )

        settlement = settle_after_parse(self._mapping(), query, self._options)

        assert settlement.unresolved is None
