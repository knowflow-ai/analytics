from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from knowflow_analytics.contracts import DimensionValueSpec
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryEligibilityStatus,
    DimensionDictionaryPolicy,
    DimensionDictionaryPreview,
    DimensionDictionaryRefreshInterval,
    DimensionDictionaryStatus,
    DimensionValueCandidate,
    DimensionValueListState,
    ModelingRevision,
    SemanticDataProfile,
)
from knowflow_analytics.modeling.dimension_dictionary_eligibility import (
    assess_dimension_dictionary_eligibility,
)
from knowflow_analytics.modeling.rule_modeller import stable_id


class DimensionAliasSuggester(Protocol):
    def suggest(
        self,
        *,
        revision: ModelingRevision,
        candidates: tuple[DimensionValueCandidate, ...],
    ) -> dict[str, dict[str, object]]: ...


class DimensionDictionaryBuilder:
    """Create an auditable value review artifact from datasource evidence.

    Dictionary collection is separate from semantic-model editing: observed
    values become a persisted preview and cannot enter a revision until a
    separate human apply command is accepted.
    """

    def __init__(self, *, alias_suggester: DimensionAliasSuggester | None = None) -> None:
        self._alias_suggester = alias_suggester

    def build(
        self,
        *,
        revision: ModelingRevision,
        profile: SemanticDataProfile,
        dimension_ids: tuple[str, ...],
        policies: tuple[DimensionDictionaryPolicy, ...] | None = None,
    ) -> DimensionDictionaryPreview:
        selected_ids = tuple(dict.fromkeys(dimension_ids))
        if not selected_ids:
            raise SemanticValidationError(
                "select at least one categorical dimension",
                code="EMPTY_DICTIONARY_SCOPE",
            )
        if profile.schema_snapshot_hash != revision.schema_snapshot_hash:
            raise SemanticValidationError(
                "dimension dictionary profile is outside the revision schema snapshot",
                code="PROFILE_SCHEMA_DRIFT",
            )
        if policies is None:
            policies = tuple(
                DimensionDictionaryPolicy(dimension_id=dimension_id)
                for dimension_id in selected_ids
            )
        if tuple(item.dimension_id for item in policies) != selected_ids:
            raise SemanticValidationError(
                "dictionary policies must match selected dimensions in order",
                code="INVALID_DICTIONARY_POLICY",
            )
        policy_by_dimension = {item.dimension_id: item for item in policies}
        eligibilities = assess_dimension_dictionary_eligibility(
            revision=revision,
            dimension_ids=selected_ids,
            profile=profile,
        )
        ineligible = [
            item.dimension_id
            for item in eligibilities
            if item.status is DimensionDictionaryEligibilityStatus.INELIGIBLE
        ]
        if ineligible:
            raise SemanticValidationError(
                f"dimensions are not eligible for value dictionaries: {ineligible[:5]}",
                code="DICTIONARY_DIMENSION_INELIGIBLE",
            )
        review_dimension_ids = {
            item.dimension_id
            for item in eligibilities
            if item.status is DimensionDictionaryEligibilityStatus.REVIEW
        }

        selected = set(selected_ids)
        current_by_key: dict[tuple[str, str], DimensionValueSpec] = {}
        for current in revision.semantic_spec.dimension_values:
            if current.dimension_id not in selected:
                continue
            key = (current.dimension_id, _value_hash(current.value))
            if key in current_by_key:
                raise SemanticValidationError(
                    f"duplicate governed value for dimension {current.dimension_id}",
                    code="DUPLICATE_DIMENSION_VALUE",
                )
            current_by_key[key] = current

        observed_by_key = {
            (dimension.dimension_id, _value_hash(value.value)): value
            for dimension in profile.dimensions
            for value in dimension.values
        }
        keys = set(current_by_key) | set(observed_by_key)
        candidates: list[DimensionValueCandidate] = []
        dimension_order = {dimension_id: index for index, dimension_id in enumerate(selected_ids)}
        ordered_keys = sorted(
            keys,
            key=lambda key: (
                dimension_order[key[0]],
                -(observed_by_key[key].frequency if key in observed_by_key else 0),
                str(
                    observed_by_key[key].value
                    if key in observed_by_key
                    else current_by_key[key].value
                ),
            ),
        )
        for dimension_id, value_hash in ordered_keys:
            key = (dimension_id, value_hash)
            current = current_by_key.get(key)
            observed = observed_by_key.get(key)
            if observed is None and current is None:
                raise AssertionError("dictionary candidate key lost its source evidence")
            value = observed.value if observed is not None else current.value
            dimension_value_id = (
                current.id
                if current is not None
                else stable_id("dimension_value", dimension_id, value_hash.removeprefix("sha256:"))
            )
            candidates.append(
                DimensionValueCandidate(
                    id=stable_id(
                        "dimension_value_candidate",
                        dimension_id,
                        value_hash.removeprefix("sha256:"),
                    ),
                    dimension_value_id=dimension_value_id,
                    dimension_id=dimension_id,
                    value=value,
                    frequency=observed.frequency if observed is not None else None,
                    observed=observed is not None,
                    current=current is not None,
                    display_name=current.display_name if current is not None else str(value),
                    aliases=current.aliases if current is not None else (),
                    enabled=(
                        current.enabled
                        if current is not None
                        else dimension_id not in review_dimension_ids
                    ),
                    list_state=_list_state(
                        policy_by_dimension[dimension_id],
                        value,
                    ),
                )
            )
        if len(candidates) > 10_000:
            raise SemanticValidationError(
                "dimension dictionary preview exceeds 10000 review candidates",
                code="DICTIONARY_SCOPE_TOO_LARGE",
            )

        candidate_tuple = tuple(candidates)
        if any(item.ai_aliases for item in policies):
            if self._alias_suggester is None:
                raise SemanticValidationError(
                    "AI alias suggestions are not configured",
                    code="AI_ALIAS_SUGGESTER_UNAVAILABLE",
                )
            ai_candidates = tuple(
                item
                for item in candidate_tuple
                if policy_by_dimension[item.dimension_id].ai_aliases
            )
            suggestions = self._alias_suggester.suggest(
                revision=revision,
                candidates=ai_candidates,
            )
            unknown = set(suggestions) - {item.id for item in ai_candidates}
            if unknown:
                raise SemanticValidationError(
                    "AI alias suggestions contain unknown dimension values",
                    code="AI_ALIAS_OUTPUT_INVALID",
                )
            candidate_tuple = tuple(
                _apply_alias_suggestion(item, suggestions.get(item.id)) for item in candidate_tuple
            )

        created_at = datetime.now(UTC)
        scheduled_policies = tuple(
            _schedule_policy(item, refreshed_at=created_at) for item in policies
        )
        digest = content_hash(
            {
                "revision_id": revision.id,
                "revision_etag": revision.etag,
                "schema_snapshot_hash": revision.schema_snapshot_hash,
                "semantic_spec_hash": revision.semantic_spec.spec_hash,
                "selected_dimension_ids": selected_ids,
                "profile_hash": profile.content_hash,
                "policies": [item.model_dump(mode="json") for item in scheduled_policies],
                "eligibilities": [item.model_dump(mode="json") for item in eligibilities],
                "candidates": [item.model_dump(mode="json") for item in candidate_tuple],
                "created_at": created_at.isoformat(),
            }
        )
        return DimensionDictionaryPreview(
            id=f"dimension_dictionary_{digest.removeprefix('sha256:')[:16]}",
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            semantic_spec_hash=revision.semantic_spec.spec_hash,
            selected_dimension_ids=selected_ids,
            policies=scheduled_policies,
            eligibilities=eligibilities,
            profile=profile,
            candidates=candidate_tuple,
            created_at=created_at,
        )


def merge_complete_profiled_dimension_values(
    *,
    current_values: tuple[DimensionValueSpec, ...],
    profile: SemanticDataProfile,
    dimension_ids: tuple[str, ...],
) -> tuple[DimensionValueSpec, ...]:
    """Preset complete database dictionaries without an AI or review boundary.

    The dictionary fetch is a deterministic ``Dimension + COUNT(1) + GROUP BY``
    query. For a newly materialized categorical dimension that evidence can be
    persisted immediately when the result is complete. A
    truncated or partially serializable result is never presented as a complete
    dictionary. Existing governed labels and aliases remain authoritative.

    Automatic application is an explicitly reviewed decision; it changes only the
    review timing, not the fetched value semantics.
    """

    selected = set(dimension_ids)
    existing_keys = {(item.dimension_id, _value_hash(item.value)) for item in current_values}
    additions: list[DimensionValueSpec] = []
    for dimension in profile.dimensions:
        if dimension.dimension_id not in selected:
            continue
        complete = (
            not dimension.truncated
            and not dimension.source_rows_truncated
            and len(dimension.values) == dimension.observed_distinct_values
        )
        if not complete:
            continue
        for observed in dimension.values:
            value_hash = _value_hash(observed.value)
            key = (dimension.dimension_id, value_hash)
            if key in existing_keys:
                continue
            additions.append(
                DimensionValueSpec(
                    id=stable_id(
                        "dimension_value",
                        dimension.dimension_id,
                        value_hash.removeprefix("sha256:"),
                    ),
                    dimension_id=dimension.dimension_id,
                    value=observed.value,
                    display_name=str(observed.value),
                    aliases=(),
                    enabled=True,
                )
            )
            existing_keys.add(key)
    return (*current_values, *additions)


def _value_hash(value: object) -> str:
    return content_hash({"value": value})


def _list_state(
    policy: DimensionDictionaryPolicy,
    value: str | int | float | bool,
) -> DimensionValueListState:
    digest = _value_hash(value)
    if digest in {_value_hash(item) for item in policy.black_list}:
        return DimensionValueListState.BLACK
    if digest in {_value_hash(item) for item in policy.white_list}:
        return DimensionValueListState.WHITE
    return DimensionValueListState.NORMAL


def _apply_alias_suggestion(
    candidate: DimensionValueCandidate,
    suggestion: dict[str, object] | None,
) -> DimensionValueCandidate:
    if suggestion is None:
        return candidate
    display_name = str(suggestion.get("display_name") or candidate.display_name).strip()
    aliases_value = suggestion.get("aliases", candidate.aliases)
    if not isinstance(aliases_value, (tuple, list)):
        raise SemanticValidationError(
            "AI alias suggestions must contain an alias list",
            code="AI_ALIAS_OUTPUT_INVALID",
        )
    aliases = tuple(str(item).strip() for item in aliases_value if str(item).strip())
    return candidate.model_copy(update={"display_name": display_name, "aliases": aliases})


def _schedule_policy(
    policy: DimensionDictionaryPolicy,
    *,
    refreshed_at: datetime,
) -> DimensionDictionaryPolicy:
    next_refresh_at = None
    if policy.refresh_interval is DimensionDictionaryRefreshInterval.DAILY:
        next_refresh_at = refreshed_at + timedelta(days=1)
    elif policy.refresh_interval is DimensionDictionaryRefreshInterval.WEEKLY:
        next_refresh_at = refreshed_at + timedelta(days=7)
    return policy.model_copy(
        update={
            "refreshed_at": refreshed_at,
            "next_refresh_at": next_refresh_at,
        }
    )


def due_dictionary_refresh_groups(
    previews: tuple[DimensionDictionaryPreview, ...],
    *,
    now: datetime,
) -> tuple[tuple[str, tuple[DimensionDictionaryPolicy, ...]], ...]:
    """Return due policies while suppressing duplicate pending previews.

    A scheduled refresh deliberately stops at a new human-review Preview rather
    than touching the runtime dictionary, so an unreviewed refresh suppresses
    later scheduler runs for that dimension.
    """

    latest_applied: dict[tuple[str, str], tuple[datetime, DimensionDictionaryPolicy]] = {}
    latest_pending: dict[tuple[str, str], datetime] = {}
    for preview in previews:
        if preview.status is DimensionDictionaryStatus.APPLIED:
            for policy in preview.policies:
                refreshed_at = policy.refreshed_at or preview.created_at
                key = (preview.revision_id, policy.dimension_id)
                current = latest_applied.get(key)
                if current is None or refreshed_at > current[0]:
                    latest_applied[key] = (refreshed_at, policy)
        elif preview.status is DimensionDictionaryStatus.COMPLETED:
            for dimension_id in preview.selected_dimension_ids:
                key = (preview.revision_id, dimension_id)
                current = latest_pending.get(key)
                if current is None or preview.created_at > current:
                    latest_pending[key] = preview.created_at

    grouped: dict[str, list[DimensionDictionaryPolicy]] = {}
    for key, (refreshed_at, policy) in sorted(latest_applied.items()):
        if policy.next_refresh_at is None or policy.next_refresh_at > now:
            continue
        pending_at = latest_pending.get(key)
        if pending_at is not None and pending_at >= refreshed_at:
            continue
        grouped.setdefault(key[0], []).append(
            policy.model_copy(update={"refreshed_at": None, "next_refresh_at": None})
        )
    return tuple(
        (revision_id, tuple(policies)) for revision_id, policies in sorted(grouped.items())
    )
