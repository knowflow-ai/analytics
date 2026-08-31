from __future__ import annotations

from datetime import UTC, datetime, timedelta

from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.contracts import (
    DimensionDataProfile,
    DimensionDictionaryEligibilityStatus,
    DimensionDictionaryPolicy,
    DimensionDictionaryPreview,
    DimensionDictionaryRefreshInterval,
    DimensionDictionaryStatus,
    DimensionValueListState,
    ModelingRevision,
    ProfiledValue,
    SemanticDataProfile,
)
from knowflow_analytics.modeling.dimension_dictionary import (
    DimensionDictionaryBuilder,
    due_dictionary_refresh_groups,
)


def test_dictionary_candidates_are_invariant_to_projection_order_and_semantic_id(sales_release):
    """No value behavior may depend on one business dimension or physical field name."""

    spec = sales_release.model_copy(update={"dimension_values": ()})
    revision = ModelingRevision(
        id="revision-dictionary",
        project_id=spec.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=7,
        semantic_spec=spec,
    )
    builder = DimensionDictionaryBuilder()
    region_profile = _profile(
        dimension_id="region",
        model_id="orders",
        field_id="orders.region",
        values=(("R2", 1), ("R1", 3)),
    )
    channel_profile = _profile(
        dimension_id="channel",
        model_id="orders",
        field_id="orders.channel",
        values=(("C2", 1), ("C1", 3)),
    )

    region = builder.build(
        revision=revision,
        profile=region_profile,
        dimension_ids=("region",),
    )
    channel = builder.build(
        revision=revision,
        profile=channel_profile,
        dimension_ids=("channel",),
    )

    assert [(item.value, item.frequency) for item in region.candidates] == [
        ("R1", 3),
        ("R2", 1),
    ]
    assert [(item.value, item.frequency) for item in channel.candidates] == [
        ("C1", 3),
        ("C2", 1),
    ]
    assert [item.observed for item in region.candidates] == [True, True]
    assert [item.current for item in region.candidates] == [False, False]


def test_truncated_high_cardinality_dimension_requires_manual_review(sales_release):
    revision = ModelingRevision(
        id="revision-high-cardinality",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=2,
        semantic_spec=sales_release.model_copy(update={"dimension_values": ()}),
    )
    profile = _profile(
        dimension_id="region",
        model_id="orders",
        field_id="orders.region",
        values=(("R1", 3), ("R2", 1)),
    )
    profile = profile.model_copy(
        update={
            "dimensions": (
                profile.dimensions[0].model_copy(
                    update={"observed_distinct_values": 51, "truncated": True}
                ),
            )
        }
    )

    preview = DimensionDictionaryBuilder().build(
        revision=revision,
        profile=profile,
        dimension_ids=("region",),
    )

    assert preview.eligibilities[0].status is DimensionDictionaryEligibilityStatus.REVIEW
    assert preview.eligibilities[0].reason_code == "high_cardinality"
    assert all(item.enabled is False for item in preview.candidates)


def test_dictionary_policy_keeps_black_and_white_lists_disjoint(sales_release):
    revision = ModelingRevision(
        id="revision-policy",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=2,
        semantic_spec=sales_release.model_copy(update={"dimension_values": ()}),
    )
    preview = DimensionDictionaryBuilder().build(
        revision=revision,
        profile=_profile(
            dimension_id="region",
            model_id="orders",
            field_id="orders.region",
            values=(("R1", 3), ("R2", 1)),
        ),
        dimension_ids=("region",),
        policies=(
            DimensionDictionaryPolicy(
                dimension_id="region",
                refresh_interval=DimensionDictionaryRefreshInterval.DAILY,
                black_list=("R2",),
                white_list=("R1",),
            ),
        ),
    )

    assert preview.policies[0].refresh_interval is DimensionDictionaryRefreshInterval.DAILY
    assert [item.list_state for item in preview.candidates] == [
        DimensionValueListState.WHITE,
        DimensionValueListState.BLACK,
    ]


def test_ai_alias_suggestions_are_data_driven_and_do_not_change_raw_values(sales_release):
    class StubAliasSuggester:
        def suggest(self, *, revision, candidates):
            return {
                item.id: {
                    "display_name": f"业务-{item.value}",
                    "aliases": (f"简称-{item.value}",),
                }
                for item in reversed(candidates)
            }

    revision = ModelingRevision(
        id="revision-ai-alias",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=2,
        semantic_spec=sales_release.model_copy(update={"dimension_values": ()}),
    )
    preview = DimensionDictionaryBuilder(alias_suggester=StubAliasSuggester()).build(
        revision=revision,
        profile=_profile(
            dimension_id="channel",
            model_id="orders",
            field_id="orders.channel",
            values=(("C2", 1), ("C1", 3)),
        ),
        dimension_ids=("channel",),
        policies=(DimensionDictionaryPolicy(dimension_id="channel", ai_aliases=True),),
    )

    assert [item.value for item in preview.candidates] == ["C1", "C2"]
    assert [item.display_name for item in preview.candidates] == ["业务-C1", "业务-C2"]
    assert [item.aliases for item in preview.candidates] == [
        ("简称-C1",),
        ("简称-C2",),
    ]


def test_due_refresh_does_not_duplicate_pending_human_review(sales_release):
    now = datetime(2026, 8, 17, tzinfo=UTC)
    revision = ModelingRevision(
        id="revision-refresh",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=2,
        semantic_spec=sales_release.model_copy(update={"dimension_values": ()}),
    )
    applied = (
        DimensionDictionaryBuilder()
        .build(
            revision=revision,
            profile=_profile(
                dimension_id="region",
                model_id="orders",
                field_id="orders.region",
                values=(("R1", 1),),
            ),
            dimension_ids=("region",),
            policies=(
                DimensionDictionaryPolicy(
                    dimension_id="region",
                    refresh_interval=DimensionDictionaryRefreshInterval.DAILY,
                ),
            ),
        )
        .model_copy(
            update={
                "status": DimensionDictionaryStatus.APPLIED,
                "reviewed_by": "analyst",
                "reviewed_at": now - timedelta(days=2),
                "decisions": (),
                "resulting_revision_etag": 3,
                "policies": (
                    DimensionDictionaryPolicy(
                        dimension_id="region",
                        refresh_interval=DimensionDictionaryRefreshInterval.DAILY,
                        refreshed_at=now - timedelta(days=2),
                        next_refresh_at=now - timedelta(days=1),
                    ),
                ),
            }
        )
    )
    # The production contract requires decisions for every candidate; use a
    # no-value copy to focus this test on scheduler behavior.
    applied = DimensionDictionaryPreview.model_validate(
        applied.model_copy(update={"candidates": ()}).model_dump(mode="python")
    )
    due = due_dictionary_refresh_groups((applied,), now=now)
    assert len(due) == 1
    assert due[0][0] == revision.id
    assert due[0][1][0].dimension_id == "region"
    assert due[0][1][0].refreshed_at is None
    assert due[0][1][0].next_refresh_at is None

    pending = applied.model_copy(
        update={
            "id": "pending-refresh",
            "status": DimensionDictionaryStatus.COMPLETED,
            "created_at": now,
            "reviewed_by": None,
            "reviewed_at": None,
            "decisions": (),
            "resulting_revision_etag": None,
        }
    )
    pending = DimensionDictionaryPreview.model_validate(pending.model_dump(mode="python"))
    assert due_dictionary_refresh_groups((applied, pending), now=now) == ()


def _profile(
    *,
    dimension_id: str,
    model_id: str,
    field_id: str,
    values: tuple[tuple[str, int], ...],
) -> SemanticDataProfile:
    dimension = DimensionDataProfile(
        dimension_id=dimension_id,
        model_id=model_id,
        field_id=field_id,
        sampled_rows=sum(frequency for _value, frequency in values),
        observed_distinct_values=len(values),
        values=tuple(
            ProfiledValue(value=value, frequency=frequency) for value, frequency in values
        ),
    )
    digest = content_hash(dimension.model_dump(mode="json"))
    return SemanticDataProfile(
        id=f"profile-{dimension_id}",
        schema_snapshot_hash="sha256:snapshot",
        content_hash=digest,
        captured_at=datetime.now(UTC),
        dimensions=(dimension,),
    )
