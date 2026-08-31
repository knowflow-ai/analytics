from __future__ import annotations

from dataclasses import dataclass

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionSpec,
    DimensionValueSpec,
    MetricSpec,
)
from knowflow_analytics.query.contracts import MatchMethod, SchemaMatch
from knowflow_analytics.query.scope_resolver import (
    MetricPhraseAmbiguity,
    QueryScopeResolutionStatus,
    QueryScopeResolver,
)
from knowflow_analytics.semantic.index import SemanticElementType


@dataclass(frozen=True)
class _Evidence:
    """Scope-neutral mapper evidence used to prove resolver decoupling."""

    element_type: str
    element_id: str
    detected_text: str
    method: str = "exact"
    score: float = 1.0
    channel: str = "dictionary"
    origin_term_entry_id: str | None = None


@dataclass(frozen=True)
class _EligibleEvidence(_Evidence):
    eligible_dataset_ids: tuple[str, ...] = ()


def _metric(metric_id: str, *, owner: str) -> MetricSpec:
    return MetricSpec(
        id=metric_id,
        name=metric_id,
        model_id=owner,
        field_id=f"{owner}.{metric_id}",
        aggregation=Aggregation.SUM,
    )


def _dimension(dimension_id: str, *, model_id: str) -> DimensionSpec:
    return DimensionSpec(
        id=dimension_id,
        name=dimension_id,
        model_id=model_id,
        field_id=f"{model_id}.{dimension_id}",
    )


@pytest.fixture
def resolver() -> QueryScopeResolver:
    metrics = (
        _metric("gross_revenue", owner="orders"),
        _metric("order_count", owner="orders"),
        _metric("orders_revenue", owner="orders"),
        _metric("platform_revenue", owner="platforms"),
        _metric("active_platforms", owner="platforms"),
        _metric("stale_orders_metric", owner="orders"),
    )
    dimensions = (
        _dimension("order_channel", model_id="orders"),
        _dimension("customer_region", model_id="customers"),
        _dimension("platform_region", model_id="platforms"),
    )
    values = (
        DimensionValueSpec(
            id="region_us",
            dimension_id="customer_region",
            value="US",
            display_name="United States",
        ),
        DimensionValueSpec(
            id="platform_region_us",
            dimension_id="platform_region",
            value="US",
            display_name="United States",
        ),
        DimensionValueSpec(
            id="disabled_region",
            dimension_id="customer_region",
            value="DISABLED",
            display_name="Disabled region",
            enabled=False,
        ),
    )
    datasets = (
        DatasetSpec(
            id="orders_global",
            name="Global orders",
            model_ids=("orders", "customers"),
            metric_ids=(
                "gross_revenue",
                "order_count",
                "orders_revenue",
            ),
            dimension_ids=("order_channel", "customer_region"),
        ),
        # It exposes the remote dimension in member metadata, but has no frozen
        # route to it. Membership alone must not make the dimension reachable.
        DatasetSpec(
            id="orders_local",
            name="Local orders",
            model_ids=("orders", "customers"),
            metric_ids=(
                "gross_revenue",
                "order_count",
                "orders_revenue",
            ),
            dimension_ids=("order_channel", "customer_region"),
        ),
        DatasetSpec(
            id="platforms_global",
            name="Global platforms",
            model_ids=("platforms",),
            metric_ids=("platform_revenue", "active_platforms"),
            dimension_ids=("platform_region",),
        ),
    )
    routes = (
        AnalysisTopicRouteSpec(
            dataset_id="orders_global",
            root_model_id="orders",
            paths=(
                AnalysisTopicPathSpec(
                    target_model_id="customers",
                    relation_ids=("orders_customer",),
                ),
            ),
        ),
        AnalysisTopicRouteSpec(
            dataset_id="orders_local",
            root_model_id="orders",
        ),
        AnalysisTopicRouteSpec(
            dataset_id="platforms_global",
            root_model_id="platforms",
        ),
    )
    return QueryScopeResolver(
        datasets=datasets,
        routes=routes,
        metrics=metrics,
        dimensions=dimensions,
        values=values,
    )


def test_same_detected_text_across_fact_roots_is_scope_ambiguity(
    resolver: QueryScopeResolver,
) -> None:
    evidence = (
        _Evidence("metric", "orders_revenue", " Revenue ", score=0.99),
        _Evidence("metric", "platform_revenue", "revenue", score=0.01),
    )

    resolution = resolver.resolve(evidence)

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.owner_model_ids == ("orders", "platforms")
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )
    assert resolution.ambiguous_metric_groups == (
        MetricPhraseAmbiguity(
            detected_text="revenue",
            metric_ids=("orders_revenue", "platform_revenue"),
        ),
    )
    # A score gap is intentionally irrelevant to the ambiguity result.
    assert resolution.selected_dataset_id is None


def test_same_detected_text_within_one_root_remains_semantic_ambiguity(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "revenue"),
            _Evidence("metric", "orders_revenue", "revenue"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.owner_model_ids == ("orders",)
    assert resolution.candidate_dataset_ids == ("orders_global", "orders_local")


def test_same_scope_metric_alias_ambiguity_is_left_for_final_llm_settlement(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "revenue"),
            _Evidence("metric", "orders_revenue", "revenue"),
        ),
        allowed_dataset_ids=("orders_global",),
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.exact_metric_ids == ("gross_revenue", "orders_revenue")


@pytest.mark.parametrize(
    ("evidence", "forbidden_id", "expected_metric_ids", "expected_dimension_ids"),
    (
        (
            (
                _Evidence("metric", "orders_revenue", "revenue"),
                _Evidence("metric", "platform_revenue", "revenue"),
            ),
            "platform_revenue",
            ("orders_revenue",),
            (),
        ),
        (
            (
                _Evidence("dimension", "customer_region", "region"),
                _Evidence("dimension", "platform_region", "region"),
            ),
            "platform_region",
            (),
            ("customer_region",),
        ),
        (
            (
                _Evidence("dimension_value", "region_us", "United States"),
                _Evidence(
                    "dimension_value",
                    "platform_region_us",
                    "United States",
                ),
            ),
            "platform_region",
            (),
            ("customer_region",),
        ),
    ),
)
def test_allowed_scope_filters_evidence_before_grouping_without_an_element_oracle(
    resolver: QueryScopeResolver,
    evidence: tuple[_Evidence, ...],
    forbidden_id: str,
    expected_metric_ids: tuple[str, ...],
    expected_dimension_ids: tuple[str, ...],
) -> None:
    resolution = resolver.resolve(
        evidence,
        allowed_dataset_ids=("orders_global",),
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.candidate_dataset_ids == ("orders_global",)
    assert resolution.exact_metric_ids == expected_metric_ids
    assert resolution.exact_dimension_ids == expected_dimension_ids
    assert resolution.ambiguous_metric_groups == ()
    assert forbidden_id not in str(resolution.to_trace_detail())


def test_selected_scope_filters_out_other_allowed_scope_evidence_before_grouping(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
        ),
        selected_dataset_id="orders_global",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.exact_metric_ids == ("orders_revenue",)
    assert resolution.ambiguous_metric_groups == ()
    assert "platform_revenue" not in str(resolution.to_trace_detail())


@pytest.mark.parametrize(
    ("element_type", "element_id"),
    (
        ("metric", "platform_revenue"),
        ("dimension", "platform_region"),
        ("dimension_value", "platform_region_us"),
    ),
)
def test_selected_scope_rejects_a_singleton_group_it_cannot_satisfy(
    resolver: QueryScopeResolver,
    element_type: str,
    element_id: str,
) -> None:
    resolution = resolver.resolve(
        (_Evidence(element_type, element_id, "outside selected scope"),),
        selected_dataset_id="orders_global",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "SELECTED_QUERY_SCOPE_INVALID"
    assert resolution.exact_metric_ids == ()
    assert resolution.exact_dimension_ids == ()
    assert element_id not in str(resolution.to_trace_detail())


@pytest.mark.parametrize(
    ("element_type", "element_id"),
    (
        ("metric", "unknown_metric"),
        ("dimension", "unknown_dimension"),
        ("dimension_value", "unknown_value"),
        ("dimension_value", "disabled_region"),
    ),
)
def test_unknown_or_disabled_exact_evidence_fails_closed_without_ids(
    resolver: QueryScopeResolver,
    element_type: str,
    element_id: str,
) -> None:
    resolution = resolver.resolve(
        (_Evidence(element_type, element_id, "stale evidence"),),
        allowed_dataset_ids=("orders_global",),
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "QUERY_SCOPE_COMPILATION_STALE"
    assert resolution.exact_metric_ids == ()
    assert resolution.exact_dimension_ids == ()
    assert element_id not in str(resolution.to_trace_detail())


@pytest.mark.parametrize(
    ("evidence", "selected_element_id", "expected_metric_ids", "expected_dimension_ids"),
    (
        (
            (
                _Evidence("metric", "orders_revenue", "revenue"),
                _Evidence("metric", "platform_revenue", "revenue"),
            ),
            "platform_revenue",
            ("platform_revenue",),
            (),
        ),
        (
            (
                _Evidence("dimension", "customer_region", "region"),
                _Evidence("dimension", "platform_region", "region"),
            ),
            "platform_region",
            (),
            ("platform_region",),
        ),
        (
            (
                _Evidence("dimension_value", "region_us", "United States"),
                _Evidence(
                    "dimension_value",
                    "platform_region_us",
                    "United States",
                ),
            ),
            "platform_region_us",
            (),
            ("platform_region",),
        ),
    ),
)
def test_selected_element_collapses_only_its_exact_phrase_group(
    resolver: QueryScopeResolver,
    evidence: tuple[_Evidence, ...],
    selected_element_id: str,
    expected_metric_ids: tuple[str, ...],
    expected_dimension_ids: tuple[str, ...],
) -> None:
    resolution = resolver.resolve(
        evidence,
        selected_element_id=selected_element_id,
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.exact_metric_ids == expected_metric_ids
    assert resolution.exact_dimension_ids == expected_dimension_ids
    assert resolution.ambiguous_metric_groups == ()


def test_selected_element_outside_allowed_scope_is_a_scope_violation(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
        ),
        allowed_dataset_ids=("orders_global",),
        selected_element_id="platform_revenue",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "SELECTED_ELEMENT_SCOPE_VIOLATION"
    assert resolution.exact_metric_ids == ()
    assert "platform_revenue" not in str(resolution.to_trace_detail())


def test_selected_element_must_exist_in_exact_evidence(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (_Evidence("metric", "orders_revenue", "revenue"),),
        selected_element_id="order_count",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "SELECTED_ELEMENT_INVALID"
    assert resolution.exact_metric_ids == ()


@pytest.mark.parametrize(
    ("method", "channel", "origin_term_entry_id"),
    (
        ("keyword", "dictionary", None),
        ("embedding", "embedding", None),
        ("exact", "term_dictionary", "term:business-word"),
    ),
)
def test_selected_metric_accepts_every_valid_non_manifest_evidence_as_confirmed_owner(
    resolver: QueryScopeResolver,
    method: str,
    channel: str,
    origin_term_entry_id: str | None,
) -> None:
    """A typed HITL choice is valid for every Mapper channel users can see.

    Keyword, embedding and Term-description evidence remain non-exact, but an
    explicit user choice is an independent owner signal for Scope routing.
    """

    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "business wording",
                method=method,
                channel=channel,
                origin_term_entry_id=origin_term_entry_id,
            ),
        ),
        allowed_dataset_ids=("platforms_global",),
        selected_element_id="platform_revenue",
        selected_element_type="metric",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.owner_model_ids == ("platforms",)
    assert resolution.exact_metric_ids == ()
    assert resolution.confirmed_metric_ids == ("platform_revenue",)


def test_human_confirmed_weak_metric_becomes_owner_without_becoming_exact(
    resolver: QueryScopeResolver,
) -> None:
    """Reviewed CANDIDATE_DISCOVERY contract.

    A user choice is a separate governed routing signal.  It fixes the metric
    owner but must never rewrite Mapper provenance into MatchMethod.EXACT.
    """

    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="keyword",
                score=0.31,
            ),
        ),
        selected_element_id="platform_revenue",
        selected_element_type="metric",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.owner_model_ids == ("platforms",)
    assert resolution.exact_metric_ids == ()
    assert resolution.confirmed_metric_ids == ("platform_revenue",)


def test_ai_adjudicated_weak_metric_has_distinct_owner_provenance(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="embedding",
                score=0.99,
            ),
        ),
        ai_adjudicated_metric_id="platform_revenue",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.owner_model_ids == ("platforms",)
    assert resolution.exact_metric_ids == ()
    assert resolution.confirmed_metric_ids == ()
    assert resolution.ai_adjudicated_metric_ids == ("platform_revenue",)


def test_release_bound_memory_metric_has_distinct_owner_provenance(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="embedding",
                score=0.99,
            ),
        ),
        memory_confirmed_metric_id="platform_revenue",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.exact_metric_ids == ()
    assert resolution.confirmed_metric_ids == ()
    assert resolution.ai_adjudicated_metric_ids == ()
    assert resolution.memory_confirmed_metric_ids == ("platform_revenue",)


def test_multiple_ai_resolved_weak_phrases_route_when_metrics_share_one_owner(
    resolver: QueryScopeResolver,
) -> None:
    evidence = (
        _Evidence("metric", "gross_revenue", "sales", method="keyword", score=0.4),
        _Evidence("metric", "order_count", "volume", method="embedding", score=0.95),
    )

    resolution = resolver.resolve(
        evidence,
        ai_adjudicated_metric_ids=("gross_revenue", "order_count"),
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.candidate_dataset_ids == ("orders_global", "orders_local")
    assert resolution.owner_model_ids == ("orders",)
    assert resolution.ai_adjudicated_metric_ids == ("gross_revenue", "order_count")


def test_multiple_ai_resolved_weak_phrases_never_bridge_fact_roots(
    resolver: QueryScopeResolver,
) -> None:
    evidence = (
        _Evidence("metric", "gross_revenue", "sales", method="keyword", score=0.4),
        _Evidence(
            "metric",
            "platform_revenue",
            "platform sales",
            method="embedding",
            score=0.95,
        ),
    )

    resolution = resolver.resolve(
        evidence,
        ai_adjudicated_metric_ids=("gross_revenue", "platform_revenue"),
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "CROSS_FACT_METRICS_UNSUPPORTED"


def test_ai_and_human_metric_selection_cannot_be_combined(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (_Evidence("metric", "platform_revenue", "sales wording", method="embedding"),),
        selected_element_id="platform_revenue",
        selected_element_type="metric",
        ai_adjudicated_metric_id="platform_revenue",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "MULTIPLE_METRIC_SELECTION_SOURCES"


def test_ai_adjudicated_metric_keeps_exact_dimension_as_a_veto(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="embedding",
            ),
            _Evidence("dimension_value", "region_us", "United States"),
        ),
        ai_adjudicated_metric_id="platform_revenue",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "DIMENSION_NOT_REACHABLE"
    assert resolution.exact_metric_ids == ()
    assert resolution.confirmed_metric_ids == ()
    assert resolution.ai_adjudicated_metric_ids == ("platform_revenue",)
    assert resolution.exact_dimension_ids == ("customer_region",)


def test_confirmed_metric_keeps_exact_dimension_as_a_veto(
    resolver: QueryScopeResolver,
) -> None:
    """Metric anchoring cannot discard an incompatible exact dimension/value."""

    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="embedding",
                score=0.99,
            ),
            _Evidence("dimension_value", "region_us", "United States"),
        ),
        selected_element_id="platform_revenue",
        selected_element_type="metric",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "DIMENSION_NOT_REACHABLE"
    assert resolution.owner_model_ids == ("platforms",)
    assert resolution.exact_metric_ids == ()
    assert resolution.exact_dimension_ids == ("customer_region",)
    assert resolution.confirmed_metric_ids == ("platform_revenue",)


def test_confirmed_metric_owner_is_invariant_to_weak_score_and_evidence_order(
    resolver: QueryScopeResolver,
) -> None:
    def resolve(score: float, *, reversed_order: bool):
        evidence = (
            _Evidence("dimension_value", "platform_region_us", "United States"),
            _Evidence(
                "metric",
                "platform_revenue",
                "sales wording",
                method="embedding",
                score=score,
            ),
        )
        return resolver.resolve(
            tuple(reversed(evidence)) if reversed_order else evidence,
            selected_element_id="platform_revenue",
            selected_element_type="metric",
        )

    assert resolve(0.01, reversed_order=False) == resolve(0.99, reversed_order=True)


def test_selected_element_rejects_manifest_only_membership(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "platform_revenue",
                "platform_revenue",
                method="all_field",
                channel="manifest",
            ),
        ),
        allowed_dataset_ids=("platforms_global",),
        selected_element_id="platform_revenue",
        selected_element_type="metric",
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "SELECTED_ELEMENT_INVALID"
    assert resolution.exact_metric_ids == ()


@pytest.mark.parametrize(
    "element_types",
    (
        ("metric", "dimension"),
        ("dimension", "dimension_value"),
        ("metric", "dimension_value"),
    ),
)
def test_legacy_selected_element_id_refuses_cross_type_collisions(
    element_types: tuple[str, str],
) -> None:
    shared_id = "shared_element"
    metric = _metric(shared_id, owner="orders")
    shared_dimension = _dimension(shared_id, model_id="orders")
    value_parent = (
        shared_dimension
        if "dimension" in element_types
        else _dimension("value_parent", model_id="orders")
    )
    value = DimensionValueSpec(
        id=shared_id,
        dimension_id=value_parent.id,
        value="shared",
        display_name="Shared",
    )
    metrics = (metric,) if "metric" in element_types else ()
    dimensions = (value_parent,)
    values = (value,) if "dimension_value" in element_types else ()
    dataset = DatasetSpec(
        id="collision_scope",
        name="Collision scope",
        model_ids=("orders",),
        metric_ids=tuple(item.id for item in metrics),
        dimension_ids=tuple(item.id for item in dimensions),
    )
    collision_resolver = QueryScopeResolver(
        datasets=(dataset,),
        routes=(
            AnalysisTopicRouteSpec(
                dataset_id=dataset.id,
                root_model_id="orders",
            ),
        ),
        metrics=metrics,
        dimensions=dimensions,
        values=values,
    )

    resolution = collision_resolver.resolve(
        tuple(_Evidence(item_type, shared_id, "shared") for item_type in element_types),
        selected_element_id=shared_id,
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "SELECTED_ELEMENT_TYPE_REQUIRED"
    assert resolution.exact_metric_ids == ()
    assert resolution.exact_dimension_ids == ()


def test_typeful_selected_element_resolves_a_cross_type_collision() -> None:
    shared_id = "shared_element"
    metric = _metric(shared_id, owner="orders")
    dimension = _dimension(shared_id, model_id="orders")
    dataset = DatasetSpec(
        id="collision_scope",
        name="Collision scope",
        model_ids=("orders",),
        metric_ids=(shared_id,),
        dimension_ids=(shared_id,),
    )
    collision_resolver = QueryScopeResolver(
        datasets=(dataset,),
        routes=(
            AnalysisTopicRouteSpec(
                dataset_id=dataset.id,
                root_model_id="orders",
            ),
        ),
        metrics=(metric,),
        dimensions=(dimension,),
        values=(),
    )

    resolution = collision_resolver.resolve(
        (
            _Evidence("metric", shared_id, "shared"),
            _Evidence("dimension", shared_id, "shared"),
        ),
        selected_element_id=shared_id,
        selected_element_type="metric",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "collision_scope"
    assert resolution.exact_metric_ids == (shared_id,)
    assert resolution.exact_dimension_ids == ()


def test_legacy_element_type_collision_is_evaluated_only_inside_allowed_scopes() -> None:
    shared_id = "shared_element"
    metric = _metric(shared_id, owner="platforms")
    dimension = _dimension(shared_id, model_id="orders")
    orders_scope = DatasetSpec(
        id="orders_scope",
        name="Orders scope",
        model_ids=("orders",),
        dimension_ids=(shared_id,),
    )
    platform_scope = DatasetSpec(
        id="platform_scope",
        name="Platform scope",
        model_ids=("platforms",),
        metric_ids=(shared_id,),
    )
    collision_resolver = QueryScopeResolver(
        datasets=(orders_scope, platform_scope),
        routes=(
            AnalysisTopicRouteSpec(
                dataset_id=orders_scope.id,
                root_model_id="orders",
            ),
            AnalysisTopicRouteSpec(
                dataset_id=platform_scope.id,
                root_model_id="platforms",
            ),
        ),
        metrics=(metric,),
        dimensions=(dimension,),
        values=(),
    )

    resolution = collision_resolver.resolve(
        (
            _Evidence("metric", shared_id, "shared"),
            _Evidence("dimension", shared_id, "shared"),
        ),
        allowed_dataset_ids=(orders_scope.id,),
        selected_element_id=shared_id,
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == orders_scope.id
    assert resolution.exact_metric_ids == ()
    assert resolution.exact_dimension_ids == (shared_id,)


@pytest.mark.parametrize(
    "extra_evidence",
    (
        (
            _Evidence("metric", "order_count", "orders"),
            _Evidence("metric", "active_platforms", "active platforms"),
        ),
        (
            _Evidence("dimension", "customer_region", "customer region"),
            _Evidence("dimension", "platform_region", "platform region"),
        ),
    ),
)
def test_metric_ambiguity_with_no_compatible_scope_is_a_refusal(
    resolver: QueryScopeResolver,
    extra_evidence: tuple[_Evidence, ...],
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
            *extra_evidence,
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "NO_COMPATIBLE_QUERY_SCOPE"
    assert resolution.candidate_dataset_ids == ()


def test_evidence_eligibility_is_intersected_with_published_membership(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _EligibleEvidence(
                "metric",
                "gross_revenue",
                "revenue",
                eligible_dataset_ids=("orders_global", "not_a_published_scope"),
            ),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.candidate_dataset_ids == ("orders_global",)


def test_distinct_metric_phrases_across_owners_are_an_explicit_multi_fact_refusal(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "revenue"),
            _Evidence("metric", "active_platforms", "active platforms"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "CROSS_FACT_METRICS_UNSUPPORTED"
    assert resolution.owner_model_ids == ("orders", "platforms")
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )
    assert resolution.ambiguous_metric_groups == ()


def test_ambiguous_scope_candidates_satisfy_unambiguous_and_grouped_metrics(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "order_count", "orders"),
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
    )
    assert resolution.exact_metric_ids == (
        "order_count",
        "orders_revenue",
        "platform_revenue",
    )


@pytest.mark.parametrize(
    "evidence",
    (
        (
            _Evidence("dimension", "customer_region", "region"),
            _Evidence("dimension", "platform_region", " region "),
        ),
        (
            _Evidence("dimension_value", "region_us", "United States"),
            _Evidence(
                "dimension_value",
                "platform_region_us",
                "united states",
            ),
        ),
    ),
)
def test_same_text_dimension_alternatives_need_only_one_reachable_member_per_scope(
    resolver: QueryScopeResolver,
    evidence: tuple[_Evidence, ...],
) -> None:
    resolution = resolver.resolve(evidence)

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "platforms_global",
    )


def test_distinct_dimension_phrases_must_each_be_reachable_in_one_scope(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("dimension", "customer_region", "customer region"),
            _Evidence("dimension", "platform_region", "platform region"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "DIMENSION_NOT_REACHABLE"
    assert resolution.candidate_dataset_ids == ()


def test_one_owner_and_exact_value_reachability_select_one_scope(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "revenue"),
            _Evidence("metric", "order_count", "orders"),
            _Evidence("dimension_value", "region_us", "United States"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.code == "QUERY_SCOPE_SELECTED"
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.candidate_dataset_ids == ("orders_global",)
    assert resolution.owner_model_ids == ("orders",)
    assert resolution.exact_metric_ids == ("gross_revenue", "order_count")
    assert resolution.exact_dimension_ids == ("customer_region",)


def test_dimension_membership_without_a_frozen_route_is_not_reachable(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "revenue"),
            _Evidence("dimension", "customer_region", "region"),
        ),
        allowed_dataset_ids=("orders_local",),
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "DIMENSION_NOT_REACHABLE"
    assert resolution.candidate_dataset_ids == ()


def test_allowed_member_without_a_compiled_route_is_stale() -> None:
    metric = _metric("stale_orders_metric", owner="orders")
    stale_resolver = QueryScopeResolver(
        datasets=(
            DatasetSpec(
                id="stale_scope",
                name="Stale scope",
                model_ids=("orders",),
                metric_ids=(metric.id,),
            ),
        ),
        routes=(),
        metrics=(metric,),
        dimensions=(),
        values=(),
    )

    resolution = stale_resolver.resolve(
        (_Evidence("metric", "stale_orders_metric", "stale metric"),)
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "QUERY_SCOPE_COMPILATION_STALE"
    assert resolution.owner_model_ids == ("orders",)


def test_without_exact_metric_one_reachable_scope_can_be_selected(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve((_Evidence("dimension_value", "region_us", "United States"),))

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.candidate_dataset_ids == ("orders_global",)


def test_without_exact_metric_multiple_reachable_scopes_require_clarification(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(())

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )


def test_selected_scope_resolves_a_clarification_only_when_still_feasible(
    resolver: QueryScopeResolver,
) -> None:
    selected = resolver.resolve((), selected_dataset_id="orders_local")
    stale_selection = resolver.resolve(
        (_Evidence("dimension_value", "region_us", "United States"),),
        selected_dataset_id="orders_local",
    )

    assert selected.status is QueryScopeResolutionStatus.SELECTED
    assert selected.selected_dataset_id == "orders_local"
    assert stale_selection.status is QueryScopeResolutionStatus.REFUSED
    assert stale_selection.code == "SELECTED_QUERY_SCOPE_INVALID"
    assert stale_selection.candidate_dataset_ids == ("orders_global",)


def test_allowed_scopes_are_applied_before_scope_cardinality(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (),
        allowed_dataset_ids=("platforms_global",),
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.candidate_dataset_ids == ("platforms_global",)


def test_schema_matches_are_accepted_but_score_and_input_order_are_not_decisions(
    resolver: QueryScopeResolver,
) -> None:
    def match(metric_id: str, detected_text: str, score: float) -> SchemaMatch:
        return SchemaMatch(
            entry_id=f"entry:{metric_id}",
            dataset_id="legacy_mapper_scope_must_be_ignored",
            element_type=SemanticElementType.METRIC,
            element_id=metric_id,
            phrase=detected_text,
            detected_text=detected_text,
            method=MatchMethod.EXACT,
            score=score,
            priority=300,
        )

    first = resolver.resolve(
        (
            match("order_count", "orders", 0.1),
            match("gross_revenue", "revenue", 1.0),
        )
    )
    reordered = resolver.resolve(
        (
            match("gross_revenue", "revenue", 0.2),
            match("order_count", "orders", 0.99),
        )
    )

    assert first == reordered
    assert first.status is QueryScopeResolutionStatus.CLARIFICATION
    assert first.candidate_dataset_ids == ("orders_global", "orders_local")


def test_non_exact_metric_evidence_does_not_create_a_fact_owner(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "active_platforms",
                "active platforms",
                method="embedding",
                score=0.999,
            ),
            _Evidence("dimension_value", "region_us", "United States"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.owner_model_ids == ()


def test_exact_term_description_evidence_does_not_create_a_fact_owner(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence(
                "metric",
                "active_platforms",
                "term definition",
                method="exact",
                channel="term_dictionary",
                origin_term_entry_id="term:active-businesses",
            ),
            _Evidence("dimension_value", "region_us", "United States"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.owner_model_ids == ()
    assert resolution.exact_metric_ids == ()


def test_resolution_is_trace_safe_and_deterministically_sorted(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (),
        allowed_dataset_ids=(
            "platforms_global",
            "orders_local",
            "orders_global",
            "orders_local",
        ),
    )

    assert resolution.to_trace_detail() == {
        "status": "clarification",
        "code": "AMBIGUOUS_QUERY_SCOPE",
        "message": "多个受治理查询作用域满足当前精确语义证据。",
        "selected_dataset_id": None,
        "candidate_dataset_ids": [
            "orders_global",
            "orders_local",
            "platforms_global",
        ],
        "owner_model_ids": [],
        "exact_metric_ids": [],
        "confirmed_metric_ids": [],
        "ai_adjudicated_metric_ids": [],
        "memory_confirmed_metric_ids": [],
        "exact_dimension_ids": [],
        "ambiguous_metric_groups": [],
        "anchor_dataset_ids": [],
    }


def test_exact_dataset_evidence_anchors_the_named_scope_without_semantic_evidence(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve((_Evidence("dataset", "orders_global", "global orders"),))

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.anchor_dataset_ids == ("orders_global",)


def test_two_exact_dataset_mentions_clarify_between_exactly_those_scopes(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("dataset", "orders_global", "global orders"),
            _Evidence("dataset", "platforms_global", "global platforms"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.code == "AMBIGUOUS_QUERY_SCOPE"
    assert resolution.candidate_dataset_ids == ("orders_global", "platforms_global")
    assert resolution.anchor_dataset_ids == ("orders_global", "platforms_global")


def test_dataset_anchor_never_overrides_a_unique_metric_owner(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "platform_revenue", "platform revenue"),
            _Evidence("dataset", "orders_global", "global orders"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    # 与指标 owner 候选无交集的锚点被丢弃，不参与决策。
    assert resolution.anchor_dataset_ids == ()


def test_dataset_anchor_narrows_same_root_scope_clarification(
    resolver: QueryScopeResolver,
) -> None:
    without_anchor = resolver.resolve((_Evidence("metric", "gross_revenue", "gross revenue"),))
    assert without_anchor.status is QueryScopeResolutionStatus.CLARIFICATION

    resolution = resolver.resolve(
        (
            _Evidence("metric", "gross_revenue", "gross revenue"),
            _Evidence("dataset", "orders_local", "local orders"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_local"
    assert resolution.anchor_dataset_ids == ("orders_local",)


def test_dataset_anchor_resolves_cross_root_metric_phrase_ambiguity(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
            _Evidence("dataset", "orders_global", "global orders"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"
    assert resolution.anchor_dataset_ids == ("orders_global",)


def test_partial_dataset_match_is_not_an_anchor(resolver: QueryScopeResolver) -> None:
    resolution = resolver.resolve(
        (_Evidence("dataset", "orders_global", "orders", method="keyword", score=0.7),)
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )
    assert resolution.anchor_dataset_ids == ()


def test_unknown_dataset_evidence_is_ignored_not_stale(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve((_Evidence("dataset", "retired_scope", "retired"),))

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )
    assert resolution.anchor_dataset_ids == ()


def test_dataset_anchor_outside_allowed_scopes_is_ignored(
    resolver: QueryScopeResolver,
) -> None:
    resolution = resolver.resolve(
        (_Evidence("dataset", "platforms_global", "global platforms"),),
        allowed_dataset_ids=("orders_global", "orders_local"),
    )

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.candidate_dataset_ids == ("orders_global", "orders_local")
    assert resolution.anchor_dataset_ids == ()


def test_explicit_scope_selection_ignores_anchor(resolver: QueryScopeResolver) -> None:
    resolution = resolver.resolve(
        (_Evidence("dataset", "orders_global", "global orders"),),
        selected_dataset_id="platforms_global",
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "platforms_global"
    assert resolution.anchor_dataset_ids == ()


def test_bare_model_name_alias_is_not_a_scope_anchor(resolver: QueryScopeResolver) -> None:
    """编译器自动注册的裸模型名别名不是锚点。

    「图书馆」这类通用名词几乎出现在每个相关问题里；把它当专指标识会在零语义
    证据时钉死错误作用域（2026-08-28 城市/图书馆 q09 回归）。点名作用域必须
    点完整规范名（如「商家交易额分析」）。
    """

    resolution = resolver.resolve((_Evidence("dataset", "orders_global", "orders"),))

    assert resolution.status is QueryScopeResolutionStatus.CLARIFICATION
    assert resolution.candidate_dataset_ids == (
        "orders_global",
        "orders_local",
        "platforms_global",
    )
    assert resolution.anchor_dataset_ids == ()


def test_cross_root_ambiguity_with_a_single_feasible_scope_selects_it(
    resolver: QueryScopeResolver,
) -> None:
    """跨根短语歧义收敛到唯一可行 Scope 时直选，不再弹单选项澄清。

    唯一选项的「选择」不是决策；选定该 Scope 后短语在其成员内自然消解，
    与同根歧义分支、allowed 收窄分支的既有语义一致。
    """

    resolution = resolver.resolve(
        (
            _Evidence("metric", "orders_revenue", "revenue"),
            _Evidence("metric", "platform_revenue", "revenue"),
            # 维度证据只有 orders 侧可达，platforms_global 被排除。
            _Evidence("dimension", "customer_region", "region"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.SELECTED
    assert resolution.selected_dataset_id == "orders_global"


def test_unreachable_dimension_is_not_reported_as_compilation_drift(
    resolver: QueryScopeResolver,
) -> None:
    """作用域存在但到不了该维度时，报可达性而不是"版本过期"。

    音乐六表实测（2026-08-28）：翻唱歌曲 能经三条路径到达 歌手，路径不唯一使
    歌手 被整体排除出该 Scope。问「各歌手的翻唱评分」时用户看到的却是"语义模型
    的查询范围需要重新发布，请联系建模管理员"——重新发布多少次都不会变，真相是
    这个作用域到不了那个维度。STALE 必须留给真正的编译漂移（事实根根本没有含该
    指标的作用域）。
    """

    resolution = resolver.resolve(
        (
            _Evidence("metric", "platform_revenue", "platform revenue"),
            _Evidence("dimension", "customer_region", "customer region"),
        )
    )

    assert resolution.status is QueryScopeResolutionStatus.REFUSED
    assert resolution.code == "DIMENSION_NOT_REACHABLE"
    assert resolution.owner_model_ids == ("platforms",)
