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
    """Groups of METRIC/DIMENSION members, each with the phrase that caused it.

    Dimension values are dropped from every group, not merely tolerated: a value
    can never appear in ``SemanticQuery`` ids, so counting it would make every
    mixed group look unsettled forever. Pure value collisions stay with the
    grounding validator. A group that keeps fewer than two members is no longer
    ambiguous.
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
) -> tuple[SemanticAmbiguityGroup, ...]:
    """Return Mapper-authored groups without reconstructing them from bare IDs."""

    groups: list[SemanticAmbiguityGroup] = []
    for group in mapping.semantic_ambiguity_groups:
        members = tuple(
            member
            for member in group.members
            if member.element_type in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
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
    for group in _typed_semantic_ambiguity_groups(mapping):
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
    for group in _typed_semantic_ambiguity_groups(mapping):
        typed_group = {(member.element_type, member.element_id) for member in group.members}
        if frozenset(typed_group) in obligated_groups:
            continue
        chosen_members = typed_used.intersection(typed_group)
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
