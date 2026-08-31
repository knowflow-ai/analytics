from __future__ import annotations

from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser, RuleS2SqlParser
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


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
