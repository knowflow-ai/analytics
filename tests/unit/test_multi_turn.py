from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MapMode, MappingResult, MatchMethod, SchemaMatch
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.multi_turn import MultiTurnContext, MultiTurnRewriter
from knowflow_analytics.query.service import follow_up_rewrite_gate
from knowflow_analytics.semantic.index import SemanticElementType


class _RewriteGateway:
    def __init__(self, rewritten_question: str = "华东地区的净收入呢？") -> None:
        self.rewritten_question = rewritten_question
        self.calls: list[dict[str, object]] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"rewritten_question": self.rewritten_question}


def _mapping(*matches: SchemaMatch) -> MappingResult:
    return MappingResult(
        dataset_id="sales_dataset",
        mode=MapMode.STRICT,
        normalized_question="华东呢",
        matches=matches,
        config_version="test",
    )


def _match(
    *,
    element_type: SemanticElementType,
    element_id: str,
    phrase: str,
) -> SchemaMatch:
    return SchemaMatch(
        entry_id=f"entry:{element_id}",
        dataset_id="sales_dataset",
        element_type=element_type,
        element_id=element_id,
        phrase=phrase,
        detected_text=phrase,
        method=MatchMethod.EXACT,
        score=1.0,
        priority=100,
    )


def test_multi_turn_rewriter_uses_only_last_successful_logical_context() -> None:
    gateway = _RewriteGateway()
    rewriter = MultiTurnRewriter(gateway, enabled=True)
    context = MultiTurnContext(
        current_question="华东呢？",
        current_mapping=_mapping(
            _match(
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="region_east",
                phrase="华东",
            )
        ),
        previous_question="各区域净收入是多少？",
        previous_mapping=_mapping(
            _match(
                element_type=SemanticElementType.METRIC,
                element_id="net_revenue",
                phrase="净收入",
            ),
            _match(
                element_type=SemanticElementType.DIMENSION,
                element_id="region",
                phrase="区域",
            ),
        ),
        previous_corrected_s2sql=('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'),
    )

    assert rewriter.rewrite(context) == "华东地区的净收入呢？"
    assert len(gateway.calls) == 1
    messages = gateway.calls[0]["messages"]
    serialized = "\n".join(item["content"] for item in messages)
    assert "华东呢？" in serialized
    assert "各区域净收入是多少？" in serialized
    assert "净收入" in serialized
    assert "区域" in serialized
    assert context.previous_corrected_s2sql in serialized
    assert "physical_sql" not in serialized


def test_multi_turn_rewriter_is_a_noop_when_disabled() -> None:
    gateway = _RewriteGateway()
    rewriter = MultiTurnRewriter(gateway, enabled=False)
    context = MultiTurnContext(
        current_question="华东呢？",
        current_mapping=_mapping(),
        previous_question="各区域净收入是多少？",
        previous_mapping=_mapping(),
        previous_corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
    )

    assert rewriter.rewrite(context) == "华东呢？"
    assert gateway.calls == []


def test_multi_turn_rewriter_rejects_an_empty_model_response() -> None:
    rewriter = MultiTurnRewriter(_RewriteGateway("  "), enabled=True)
    context = MultiTurnContext(
        current_question="华东呢？",
        current_mapping=_mapping(),
        previous_question="各区域净收入是多少？",
        previous_mapping=_mapping(),
        previous_corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
    )

    with pytest.raises(SemanticParsingError) as raised:
        rewriter.rewrite(context)

    assert raised.value.code == "MULTI_TURN_REWRITE_INVALID"


class TestFollowUpRewriteGate:
    """改写只能补口径，不能加问题（2026-09-05 实机回放后评审）。

    五个有害案例的形态：完整问题被续上上一轮的目标（own_metric 拦），或被续上
    上一轮的分组维度（added_dimension 拦）。合理补全——「华东呢」补上指标和
    过滤值——必须原样放行。
    """

    def test_a_question_naming_its_own_metric_is_never_rewritten(self) -> None:
        # 「各门店上个月销售额是多少」自带指标，改写器却续上了「其中太古里店的占比」。
        current = _mapping(
            _match(
                element_type=SemanticElementType.METRIC, element_id="net_revenue", phrase="净收入"
            ),
            _match(element_type=SemanticElementType.DIMENSION, element_id="region", phrase="区域"),
        )
        assert follow_up_rewrite_gate(current, None) == "own_metric"

    def test_a_rewrite_adding_a_grouping_dimension_is_discarded(self) -> None:
        # 「2024 年开业的门店有多少家」被续上「各城市」：一个数变成一张表。
        current = _mapping(
            _match(
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="v:huadong",
                phrase="华东",
            ),
        )
        rewritten = _mapping(
            _match(
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="v:huadong",
                phrase="华东",
            ),
            _match(element_type=SemanticElementType.DIMENSION, element_id="channel", phrase="渠道"),
            _match(
                element_type=SemanticElementType.METRIC, element_id="net_revenue", phrase="净收入"
            ),
        )
        assert follow_up_rewrite_gate(current, rewritten) == "added_dimension"

    def test_filling_in_the_metric_and_a_value_is_context_not_a_new_question(self) -> None:
        # 「华东呢」→「华东地区的净收入是多少」：补指标、补过滤值，放行。
        current = _mapping(
            _match(
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="v:huadong",
                phrase="华东",
            ),
        )
        rewritten = _mapping(
            _match(
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="v:huadong",
                phrase="华东",
            ),
            _match(
                element_type=SemanticElementType.METRIC, element_id="net_revenue", phrase="净收入"
            ),
        )
        assert follow_up_rewrite_gate(current, rewritten) is None

    def test_a_purely_referential_follow_up_may_inherit_everything(self) -> None:
        # 「那环比呢」什么都没有：上一轮的指标和分组维度都该继承。
        current = _mapping()
        rewritten = _mapping(
            _match(
                element_type=SemanticElementType.METRIC, element_id="net_revenue", phrase="净收入"
            ),
            _match(element_type=SemanticElementType.DIMENSION, element_id="region", phrase="区域"),
        )
        assert follow_up_rewrite_gate(current, rewritten) is None
        assert follow_up_rewrite_gate(current, None) is None

    def test_only_exact_evidence_counts(self) -> None:
        # 向量召回命中的指标不算「自己点名」——那是提示，不是用户说的。
        weak = _match(
            element_type=SemanticElementType.METRIC, element_id="net_revenue", phrase="净收入"
        )
        current = _mapping(weak.model_copy(update={"method": MatchMethod.EMBEDDING, "score": 0.9}))
        assert follow_up_rewrite_gate(current, None) is None
