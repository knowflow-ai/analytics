from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MapMode, MappingResult, MatchMethod, SchemaMatch
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.multi_turn import MultiTurnContext, MultiTurnRewriter
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
