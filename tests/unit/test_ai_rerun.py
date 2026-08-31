from __future__ import annotations

from knowflow_analytics.modeling.contracts import (
    SuggestionPatch,
    SuggestionSource,
    SuggestionState,
)
from knowflow_analytics.modeling.revision import unprocessed_suggestions


def _patch(suggestion_id: str, state: SuggestionState) -> SuggestionPatch:
    return SuggestionPatch(
        id=suggestion_id,
        target_kind="model",
        target_id="mdl_1",
        changes={"biz_name": "订单"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.9,
        state=state,
    )


def test_rerun_only_offers_suggestions_the_user_has_not_handled() -> None:
    """建议 ID 由 revision+model+输出内容决定，重跑必然重复。

    此前重跑直接抛 `suggestion run was already applied`，用户看到一句无上下文
    的英文报错。ID 稳定本身是对的（可审计、防重复应用），因此改为在重跑时
    过滤掉已处理的建议，只呈现新的。
    """

    already = (
        _patch("sug_accepted", SuggestionState.ACCEPTED),
        _patch("sug_rejected", SuggestionState.REJECTED),
    )
    fresh = (
        _patch("sug_accepted", SuggestionState.PENDING),
        _patch("sug_new", SuggestionState.PENDING),
    )
    assert [item.id for item in unprocessed_suggestions(fresh, already)] == ["sug_new"]


def test_an_already_staged_pending_suggestion_is_not_offered_again() -> None:
    """下游 apply_suggestion_run 按全部 id 判重，不看 state。

    放行一条已在 revision.suggestions 里的 PENDING 建议，整批仍会以
    `suggestion run was already applied` 失败 —— 正是本特性要修的故障。
    只要 id 已存在就不该再次提供。
    """

    already = (_patch("sug_pending", SuggestionState.PENDING),)
    fresh = (
        _patch("sug_pending", SuggestionState.PENDING),
        _patch("sug_new", SuggestionState.PENDING),
    )
    assert [item.id for item in unprocessed_suggestions(fresh, already)] == ["sug_new"]


def test_everything_new_survives_when_nothing_was_handled_before() -> None:
    fresh = (_patch("a", SuggestionState.PENDING), _patch("b", SuggestionState.PENDING))
    assert unprocessed_suggestions(fresh, ()) == fresh


def test_conflict_state_counts_as_handled() -> None:
    already = (_patch("sug_conflict", SuggestionState.CONFLICT),)
    fresh = (_patch("sug_conflict", SuggestionState.PENDING),)
    assert unprocessed_suggestions(fresh, already) == ()
