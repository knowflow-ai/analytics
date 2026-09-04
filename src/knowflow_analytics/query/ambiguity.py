"""Metric/dimension ambiguity: who decides, and how we know it was decided.

The mapper only recalls. When one detected phrase ("人数") hits several governed
elements ("生还人数", "遇难人数") it records an ambiguous group and nothing in the
pipeline is responsible for choosing. Handing every member to the final LLM and
computing whichever one it writes back is a silent wrong answer, so the group is
split by whether the LLM *can* express a choice at all:

* members that share a display name cannot be told apart in textual S2SQL —
  ``SUM("净收入")`` maps back to two IDs — so the question goes to the user
  before any model call (``same_name_ambiguity``);
* members with distinct names are given to the LLM together with their
  provenance, and the corrected query is checked afterwards: exactly one member
  used means the model decided and the answer must say so; zero or several
  means it did not, and the user is asked (``unresolved_after_parse``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from knowflow_analytics.contracts import FilterOperator, FrozenModel, SemanticQuery, SemanticRelease
from knowflow_analytics.query.contracts import (
    ClarificationOption,
    MappingResult,
    ResolvedAmbiguity,
    SemanticAmbiguityGroup,
    SemanticAmbiguityMember,
    SemanticDecision,
    SemanticDecisionSource,
)
from knowflow_analytics.semantic.index import SemanticElementType


class SemanticValueBinding(FrozenModel):
    element_id: str
    dimension_id: str
    raw_value: Any


class SemanticDecisionObligation(FrozenModel):
    """One explicit human/AI/memory choice the final S2SQL must actually use."""

    detected_text: str
    source: SemanticDecisionSource
    selected: SemanticAmbiguityMember
    candidates: tuple[SemanticAmbiguityMember, ...]
    chosen_option: ClarificationOption
    options: tuple[ClarificationOption, ...] = ()
    value_bindings: tuple[SemanticValueBinding, ...] = ()


class Settlement(FrozenModel):
    """Outcome of checking the corrected query against every ambiguous group."""

    resolved: tuple[ResolvedAmbiguity, ...] = ()
    # First typed group the LLM did not settle. None when all settled.
    unresolved: SemanticAmbiguityGroup | None = None
    decisions: tuple[SemanticDecision, ...] = ()
    unmet_obligation: SemanticDecisionObligation | None = None


def semantic_ambiguity_groups(
    mapping: MappingResult,
) -> tuple[tuple[tuple[str, ...], str], ...]:
    """Groups of members, each with the phrase that caused it.

    维度值也算数。它确实不会出现在 ``SemanticQuery`` 的 ids 里，但
    ``_query_uses_dimension_value`` 能直接看这条查询有没有按它过滤——这正是义务路径
    早就在用的判断。把值成员整个剔掉会让混合组塌成一个成员、随即被丢弃，于是
    「按门店名称分组 + 按渠道=门店过滤」这种同一个词消费两次的对冲无人接管（实测
    demo_cafe「哪些门店售卖卡布奇诺」5 次里 3 次这样答）。纯值碰撞仍归 grounding
    validator。少于两个成员的组不再是歧义。
    """

    return tuple(
        (
            tuple(member.element_id for member in group.members),
            group.detected_text,
        )
        for group in _typed_semantic_ambiguity_groups(mapping)
    )


def _typed_semantic_ambiguity_groups(
    mapping: MappingResult,
    *,
    include_values: bool = True,
) -> tuple[SemanticAmbiguityGroup, ...]:
    """Return Mapper-authored groups without reconstructing them from bare IDs.

    ``include_values`` 分开两个门：同名门问的是"模型能不能用名字表达这个选择"，
    维度值写成过滤值本来就表达得出来，且 Release 的名字表里根本没有它——放进去会被
    当成"叫不出名字的成员"，在模型还没跑之前就弹卡（实测「门店渠道的销售金额」）。
    结算门问的是"模型有没有对冲"，那里维度值必须算数。
    """

    groups: list[SemanticAmbiguityGroup] = []
    for group in mapping.semantic_ambiguity_groups:
        typed = tuple(
            member
            for member in group.members
            if member.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
        )
        # 值成员只在**混合**组里算数。纯值碰撞是另一回事：同一维度的多个取值共用一个
        # 说法（「大区」→ 华东/华南/…）本来就该合成 IN 过滤，不是对冲，归 grounding
        # validator 管。把它们也算进来会把那条既有行为变成澄清。
        members = (
            (*typed, *(m for m in group.members
                       if m.element_type is SemanticElementType.DIMENSION_VALUE))
            if typed and include_values
            else typed
        )
        if len(members) < 2:
            continue
        groups.append(
            SemanticAmbiguityGroup(
                detected_text=group.detected_text,
                members=members,
            )
        )
    return tuple(groups)


def same_name_ambiguity(
    mapping: MappingResult,
    release: SemanticRelease,
) -> SemanticAmbiguityGroup | None:
    """The first group whose members cannot be distinguished by display name."""

    return next(iter(same_name_ambiguities(mapping, release)), None)


def same_name_ambiguities(
    mapping: MappingResult,
    release: SemanticRelease,
) -> tuple[SemanticAmbiguityGroup, ...]:
    """Every group whose members cannot be distinguished by display name.

    A member the release cannot name at all (index ahead of release) is treated
    as indistinguishable: this gate is what keeps the LLM from being handed a
    choice it cannot express, so it fails closed.
    """

    names = {
        **{
            (SemanticElementType.METRIC, item.id): item.name.strip().casefold()
            for item in release.metrics
        },
        **{
            (SemanticElementType.DIMENSION, item.id): item.name.strip().casefold()
            for item in release.dimensions
        },
    }
    groups = []
    for group in _typed_semantic_ambiguity_groups(mapping, include_values=False):
        labels = [names.get((member.element_type, member.element_id)) for member in group.members]
        if None in labels or len(labels) != len(set(labels)):
            groups.append(group)
    return tuple(groups)


def used_element_ids(query: SemanticQuery) -> frozenset[str]:
    """Every governed element the corrected query actually consumes."""

    return frozenset(
        (
            *query.metric_ids,
            *query.dimension_ids,
            *(item.dimension_id for item in query.filters),
            *(item.metric_id for item in query.measure_filters),
            *(item.metric_id for item in query.metric_filters),
            *(item.element_id for item in query.order_by),
        )
    )


def _typed_used_element_ids(
    query: SemanticQuery,
) -> frozenset[tuple[SemanticElementType, str]]:
    metric_ids = {
        *query.metric_ids,
        *(item.metric_id for item in query.measure_filters),
        *(item.metric_id for item in query.metric_filters),
    }
    dimension_ids = {
        *query.dimension_ids,
        *(item.dimension_id for item in query.filters),
    }
    # QueryOrder carries only a family-local bare ID. It is already represented
    # when the same element is projected/filtered above; an order-only ID cannot
    # safely distinguish a metric from a dimension and therefore must not settle
    # a typed ambiguity by itself.
    return frozenset(
        (
            *((SemanticElementType.METRIC, item) for item in metric_ids),
            *((SemanticElementType.DIMENSION, item) for item in dimension_ids),
        )
    )


def settle_after_parse(
    mapping: MappingResult,
    query: SemanticQuery,
    options_for: Callable[[SemanticAmbiguityGroup], tuple[ClarificationOption, ...]],
    *,
    obligations: tuple[SemanticDecisionObligation, ...] = (),
) -> Settlement:
    """Decide, per group, whether the LLM settled it on exactly one member.

    Ambiguity groups exist to stop the model from silently picking one of two
    same-wording elements, so they are about a *choice*, not about presence.
    Using no member is therefore settled by construction: no choice was made,
    so none can be wrong, and the group was simply irrelevant to this question
    (「各所属城市的图书馆面积」 collides 图书馆名称/图书馆地址 while the model
    correctly groups by 城市名称). Two or more members means it hedged, which is
    a real undisclosed choice. A settled member the release cannot present as an
    option is also unsettled: the choice would otherwise be applied without ever
    being disclosed, which is the upstream behaviour this module exists to
    remove.

    A model that could not express anything is a different failure (a degenerate
    query) and must be caught by its own check, not by borrowing this gate.
    """

    typed_used = _typed_used_element_ids(query)
    resolved = []
    decisions: list[SemanticDecision] = []
    obligated_groups: set[frozenset[tuple[SemanticElementType, str]]] = set()
    for obligation in obligations:
        group = (
            SemanticAmbiguityGroup(
                detected_text=obligation.detected_text,
                members=obligation.candidates,
            )
            if len(obligation.candidates) >= 2
            else None
        )
        typed_group = frozenset(
            (member.element_type, member.element_id) for member in obligation.candidates
        )
        selected_key = (
            obligation.selected.element_type,
            obligation.selected.element_id,
        )
        obligated_groups.add(typed_group)
        value_bindings = {item.element_id: item for item in obligation.value_bindings}
        used_candidates = {
            (member.element_type, member.element_id)
            for member in obligation.candidates
            if (
                _query_uses_dimension_value(query, value_bindings[member.element_id])
                if member.element_type is SemanticElementType.DIMENSION_VALUE
                and member.element_id in value_bindings
                else (member.element_type, member.element_id) in typed_used
            )
        }
        if used_candidates != {selected_key}:
            return Settlement(unresolved=group, unmet_obligation=obligation)
        options = obligation.options or (options_for(group) if group is not None else ())
        chosen = obligation.chosen_option
        if (
            chosen.element_type != obligation.selected.element_type.value
            or chosen.element_id != obligation.selected.element_id
            or all(item.candidate_id != chosen.candidate_id for item in options)
        ):
            chosen = None
        if chosen is None:
            return Settlement(unresolved=group, unmet_obligation=obligation)
        decisions.append(
            SemanticDecision(
                source=obligation.source,
                detected_text=obligation.detected_text or chosen.label,
                chosen=chosen,
                alternatives=tuple(
                    item for item in options if item.candidate_id != chosen.candidate_id
                ),
            )
        )
    mapping_bindings = _value_bindings(mapping)
    for group in _typed_semantic_ambiguity_groups(mapping):
        typed_group = {(member.element_type, member.element_id) for member in group.members}
        if frozenset(typed_group) in obligated_groups:
            continue
        # 值成员不在 typed_used 里（SemanticQuery 只有 ids），按过滤条件判断它有没有
        # 被真正用上——与义务路径同一个判断。
        chosen_members = {
            (member.element_type, member.element_id)
            for member in group.members
            if (
                _query_uses_dimension_value(query, mapping_bindings[member.element_id])
                if member.element_type is SemanticElementType.DIMENSION_VALUE
                and member.element_id in mapping_bindings
                else (member.element_type, member.element_id) in typed_used
            )
        }
        if not chosen_members:
            continue
        if len(chosen_members) > 1:
            return Settlement(
                resolved=tuple(resolved),
                unresolved=group,
                decisions=tuple(decisions),
            )
        chosen_type, chosen_id = next(iter(chosen_members))
        options = options_for(group)
        chosen = next(
            (
                item
                for item in options
                if (item.element_type == chosen_type.value and item.element_id == chosen_id)
                or (
                    item.element_type is None
                    and item.element_id is None
                    and item.candidate_id == f"element:{chosen_type.value}:{chosen_id}"
                )
            ),
            None,
        )
        if chosen is None:
            return Settlement(
                resolved=tuple(resolved),
                unresolved=group,
                decisions=tuple(decisions),
            )
        resolved.append(
            ResolvedAmbiguity(
                detected_text=group.detected_text or chosen.label,
                chosen=chosen,
                alternatives=tuple(o for o in options if o.candidate_id != chosen.candidate_id),
            )
        )
    return Settlement(resolved=tuple(resolved), decisions=tuple(decisions))



def _value_bindings(mapping: MappingResult) -> dict[str, SemanticValueBinding]:
    """从 Mapper 证据里取出每个维度值成员的 (维度, 原始值)。

    结算要判断"这条查询有没有按这个值过滤"，光有 element_id 不够。
    """

    bindings: dict[str, SemanticValueBinding] = {}
    for item in mapping.matches:
        if item.element_type is not SemanticElementType.DIMENSION_VALUE:
            continue
        if item.dimension_id is None or item.element_id in bindings:
            continue
        bindings[item.element_id] = SemanticValueBinding(
            element_id=item.element_id,
            dimension_id=item.dimension_id,
            raw_value=item.raw_value,
        )
    return bindings


def _query_uses_dimension_value(
    query: SemanticQuery,
    binding: SemanticValueBinding,
) -> bool:
    expected = (type(binding.raw_value).__name__, str(binding.raw_value))
    for item in query.filters:
        if item.dimension_id != binding.dimension_id or item.operator not in {
            FilterOperator.EQ,
            FilterOperator.IN,
        }:
            continue
        values = (
            item.value if isinstance(item.value, (list, tuple, set, frozenset)) else (item.value,)
        )
        if any((type(value).__name__, str(value)) == expected for value in values):
            return True
    return False
