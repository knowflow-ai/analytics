from __future__ import annotations

import json

import pytest

from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.query.intent_adjudicator import (
    IntentAdjudicationCandidate,
    IntentAdjudicationDecision,
    IntentAdjudicationGroup,
    LlmIntentAdjudicator,
)


class _Gateway:
    def __init__(
        self,
        *,
        decision: str = "MATCH",
        target_label: str = "订单净金额",
        invalid_key: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.decision = decision
        self.target_label = target_label
        self.invalid_key = invalid_key
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        payload = json.loads(kwargs["messages"][1]["content"])
        if "groups" in payload:
            results = []
            for group in payload["groups"]:
                target = "订单退款金额" if "退款" in group["detected_text"] else self.target_label
                key = next(
                    item["candidate_key"] for item in group["candidates"] if item["label"] == target
                )
                results.append(
                    {
                        "group_key": group["group_key"],
                        "decision": "MATCH",
                        "candidate_key": key,
                        "reason": "分别根据业务含义判断",
                    }
                )
            return {"results": results}
        key = None
        if self.decision == "MATCH":
            key = next(
                item["candidate_key"]
                for item in payload["candidates"]
                if item["label"] == self.target_label
            )
            if self.invalid_key:
                key = "C999"
        return {
            "decision": self.decision,
            "candidate_key": key,
            "reason": "根据业务含义判断",
        }


def _candidates() -> tuple[IntentAdjudicationCandidate, ...]:
    return (
        IntentAdjudicationCandidate(
            selection_id="sel1.internal.net",
            kind="metric",
            label="订单净金额",
            description="所属实体：订单；业务定义：订单实收金额",
            aliases=("收入", "销售额"),
            business_context=("聚合：SUM", "单位：元"),
        ),
        IntentAdjudicationCandidate(
            selection_id="sel1.internal.date",
            kind="dimension",
            label="下单日期",
            description="所属实体：订单；业务定义：订单创建日期",
        ),
    )


def test_typed_intent_adjudication_exposes_only_business_payload() -> None:
    gateway = _Gateway()
    result = LlmIntentAdjudicator(gateway).adjudicate(
        intent_kind="semantic_element",
        question="各地区的订单收入是多少",
        detected_text="订单",
        candidates=_candidates(),
        exact_context=("地区",),
        query_id="query-1",
        tenant_id="tenant-1",
        release_id="release-1",
        spec_hash="sha256:spec",
    )

    assert result.decision is IntentAdjudicationDecision.MATCH
    assert result.selection_id == "sel1.internal.net"
    assert result.candidate_set_hash.startswith("sha256:")
    call = gateway.calls[0]
    assert call["purpose"] == "analytics.semantic_intent_adjudication"
    payload = json.loads(call["messages"][1]["content"])
    assert payload["intent_kind"] == "semantic_element"
    assert payload["exact_context"] == ["地区"]
    assert [item["label"] for item in payload["candidates"]] == [
        "下单日期",
        "订单净金额",
    ]
    prompt = json.dumps(call["messages"], ensure_ascii=False)
    for forbidden in (
        "sel1.internal",
        "dataset:",
        "query_scope",
        "root_model_id",
        "embedding_score",
    ):
        assert forbidden not in prompt.casefold()
    assert "confidence" not in call["response_schema"]["properties"]


def test_business_object_adjudication_uses_the_same_bounded_contract() -> None:
    gateway = _Gateway(target_label="订单")
    candidates = (
        IntentAdjudicationCandidate(
            selection_id="sel1.internal.orders",
            kind="analysis_object",
            label="订单",
            description="每张订单一条，包含金额、渠道、地区和下单时间",
            business_context=("可分析：订单数量、订单净金额、订单退款金额",),
        ),
        IntentAdjudicationCandidate(
            selection_id="sel1.internal.items",
            kind="analysis_object",
            label="订单明细",
            description="每个订单商品一条，用于分析商品和明细数量",
        ),
    )

    result = LlmIntentAdjudicator(gateway).adjudicate(
        intent_kind="analysis_object",
        question="8月1日以后有哪些渠道和地区有单子",
        detected_text="业务记录粒度",
        candidates=candidates,
        exact_context=("渠道", "地区", "下单日期"),
        query_id="query-2",
        tenant_id="tenant-1",
        release_id="release-1",
        spec_hash="sha256:spec",
    )

    assert result.decision is IntentAdjudicationDecision.MATCH
    assert result.selection_id == "sel1.internal.orders"
    assert gateway.calls[0]["purpose"] == "analytics.analysis_object_adjudication"


def test_multiple_weak_phrases_use_one_bounded_structured_model_request() -> None:
    gateway = _Gateway()
    revenue, date = _candidates()
    refund = revenue.model_copy(
        update={
            "selection_id": "sel1.internal.refund",
            "label": "订单退款金额",
            "description": "所属实体：订单；业务定义：订单退款金额",
            "aliases": ("退款",),
        }
    )

    result = LlmIntentAdjudicator(gateway).adjudicate_many(
        intent_kind="semantic_element",
        question="销售额和退款分别是多少",
        groups=(
            IntentAdjudicationGroup(
                detected_text="销售额",
                candidates=(revenue, date),
            ),
            IntentAdjudicationGroup(
                detected_text="退款",
                candidates=(refund, date),
            ),
        ),
        exact_context=(),
        query_id="query-batch",
        tenant_id="tenant-1",
        release_id="release-1",
        spec_hash="sha256:spec",
    )

    assert len(gateway.calls) == 1
    assert result.failure_code is None
    assert {(item.detected_text, item.result.selection_id) for item in result.items} == {
        ("销售额", "sel1.internal.net"),
        ("退款", "sel1.internal.refund"),
    }
    prompt = json.dumps(gateway.calls[0]["messages"], ensure_ascii=False)
    assert "sel1.internal" not in prompt


def test_candidate_order_and_internal_identifier_changes_do_not_change_the_prompt() -> None:
    first_gateway = _Gateway()
    second_gateway = _Gateway()
    first = LlmIntentAdjudicator(first_gateway).adjudicate(
        intent_kind="semantic_element",
        question="订单收入",
        detected_text="订单",
        candidates=_candidates(),
        exact_context=(),
        query_id="q1",
        tenant_id="tenant",
        release_id="r1",
        spec_hash="s1",
    )
    renamed = tuple(
        item.model_copy(update={"selection_id": f"renamed-{index}"})
        for index, item in enumerate(reversed(_candidates()), start=1)
    )
    second = LlmIntentAdjudicator(second_gateway).adjudicate(
        intent_kind="semantic_element",
        question="订单收入",
        detected_text="订单",
        candidates=renamed,
        exact_context=(),
        query_id="q2",
        tenant_id="tenant",
        release_id="r1",
        spec_hash="s1",
    )

    assert first.candidate_set_hash == second.candidate_set_hash
    assert (
        first_gateway.calls[0]["messages"][1]["content"]
        == second_gateway.calls[0]["messages"][1]["content"]
    )


@pytest.mark.parametrize("decision", ("AMBIGUOUS", "NONE"))
def test_model_abstention_is_preserved(decision: str) -> None:
    result = LlmIntentAdjudicator(_Gateway(decision=decision)).adjudicate(
        intent_kind="semantic_element",
        question="订单",
        detected_text="订单",
        candidates=_candidates(),
        exact_context=(),
        query_id="q",
        tenant_id="tenant",
        release_id="r",
        spec_hash="s",
    )

    assert result.decision is IntentAdjudicationDecision(decision)
    assert result.selection_id is None


@pytest.mark.parametrize(
    "gateway",
    (
        _Gateway(invalid_key=True),
        _Gateway(error=ModelGatewayError("secret upstream endpoint")),
    ),
)
def test_invalid_or_unavailable_output_fails_closed(gateway: _Gateway) -> None:
    result = LlmIntentAdjudicator(gateway).adjudicate(
        intent_kind="semantic_element",
        question="订单收入",
        detected_text="订单",
        candidates=_candidates(),
        exact_context=(),
        query_id="q",
        tenant_id="tenant",
        release_id="r",
        spec_hash="s",
    )

    assert result.decision is IntentAdjudicationDecision.UNAVAILABLE
    assert result.selection_id is None
    assert result.failure_code in {"MODEL_OUTPUT_INVALID", "MODEL_GATEWAY_FAILED"}
    assert "secret upstream endpoint" not in result.model_dump_json()


def test_missing_tenant_and_indistinguishable_business_payload_skip_the_gateway() -> None:
    gateway = _Gateway()
    missing_tenant = LlmIntentAdjudicator(gateway).adjudicate(
        intent_kind="semantic_element",
        question="订单",
        detected_text="订单",
        candidates=_candidates(),
        exact_context=(),
        query_id="q",
        tenant_id="",
        release_id="r",
        spec_hash="s",
    )
    duplicate = _candidates()[0].model_copy(update={"selection_id": "other"})
    indistinguishable = LlmIntentAdjudicator(gateway).adjudicate(
        intent_kind="semantic_element",
        question="订单",
        detected_text="订单",
        candidates=(_candidates()[0], duplicate),
        exact_context=(),
        query_id="q",
        tenant_id="tenant",
        release_id="r",
        spec_hash="s",
    )

    assert missing_tenant.failure_code == "TENANT_CONTEXT_REQUIRED"
    assert indistinguishable.failure_code == "CANDIDATE_SET_NOT_DISTINGUISHABLE"
    assert gateway.calls == []
