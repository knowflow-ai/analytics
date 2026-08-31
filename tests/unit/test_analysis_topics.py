from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    Cardinality,
    DimensionSpec,
    MetricSpec,
    RelationCondition,
    RelationSpec,
    SemanticQuery,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.analysis_topics import (
    AnalysisTopicProposer,
    scope_canonical_names,
    validate_analysis_topic_route,
)
from knowflow_analytics.semantic import SemanticTranslator


def _with_confirmed_order_root(sales_release):
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == "orders.id" else item
        for item in sales_release.fields
    )
    return sales_release.model_copy(update={"fields": fields})


def test_default_topic_uses_only_safe_root_relative_paths(sales_release):
    release = _with_confirmed_order_root(sales_release)

    proposals = AnalysisTopicProposer().propose(release)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.route.root_model_id == "orders"
    assert proposal.dataset.model_ids == ("orders", "customers")
    assert proposal.route.paths == (
        AnalysisTopicPathSpec(
            target_model_id="customers",
            relation_ids=("orders_customer",),
        ),
    )
    assert "customer_segment" in proposal.dataset.dimension_ids
    assert "product" not in proposal.dataset.dimension_ids
    assert release.datasets[0].id == "sales_dataset"  # proposal generation is non-mutating


def test_default_topic_membership_is_invariant_to_business_name_changes(sales_release):
    release = _with_confirmed_order_root(sales_release)
    renamed = release.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"name": f"renamed model {index}"})
                for index, item in enumerate(release.models)
            ),
            "metrics": tuple(
                item.model_copy(update={"name": f"renamed metric {index}"})
                for index, item in enumerate(release.metrics)
            ),
            "dimensions": tuple(
                item.model_copy(update={"name": f"renamed dimension {index}"})
                for index, item in enumerate(release.dimensions)
            ),
        }
    )

    original_proposal = AnalysisTopicProposer().propose(release)[0]
    renamed_proposal = AnalysisTopicProposer().propose(renamed)[0]

    assert renamed_proposal.dataset.model_ids == original_proposal.dataset.model_ids
    assert renamed_proposal.dataset.metric_ids == original_proposal.dataset.metric_ids
    assert renamed_proposal.dataset.dimension_ids == original_proposal.dataset.dimension_ids
    assert renamed_proposal.route == original_proposal.route


def test_explicit_topic_route_is_consumed_by_translation(sales_release):
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": ("region", "channel", "order_date", "customer_segment"),
        }
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
        ),
    )
    release = sales_release.model_copy(
        update={"datasets": (dataset,), "analysis_topic_routes": (route,)}
    )

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            dimension_ids=("customer_segment",),
        ),
    )

    assert physical.relation_ids == ("orders_customer",)


def test_topic_route_accepts_a_reviewed_count_metric_from_the_fact_root(sales_release):
    release = _with_confirmed_order_root(sales_release)
    dataset = release.datasets[0].model_copy(update={"model_ids": ("orders", "customers")})
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        default_count_metric_id="order_count",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
        ),
    )
    release = release.model_copy(update={"datasets": (dataset,), "analysis_topic_routes": (route,)})

    validate_analysis_topic_route(release, route)


def test_topic_route_rejects_a_non_count_default_metric(sales_release):
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        default_count_metric_id="net_revenue",
    )
    release = sales_release.model_copy(update={"analysis_topic_routes": (route,)})

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_DEFAULT_COUNT_METRIC_INVALID"


def test_topic_route_rejects_a_count_metric_outside_the_fact_root(sales_release):
    customer_count = MetricSpec(
        id="customer_count",
        name="客户数",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    dataset = sales_release.datasets[0].model_copy(
        update={"metric_ids": (*sales_release.datasets[0].metric_ids, customer_count.id)}
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        default_count_metric_id=customer_count.id,
    )
    release = sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, customer_count),
            "datasets": (dataset,),
            "analysis_topic_routes": (route,),
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_DEFAULT_COUNT_METRIC_OUTSIDE_ROOT"


def test_topic_route_rejects_discontinuous_paths(sales_release):
    dataset = sales_release.datasets[0].model_copy(update={"metric_ids": ()})
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="customers",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="order_items",
                relation_ids=("orders_items",),
            ),
            AnalysisTopicPathSpec(
                target_model_id="orders",
                relation_ids=("orders_customer",),
            ),
        ),
    )
    release = sales_release.model_copy(
        update={"datasets": (dataset,), "analysis_topic_routes": (route,)}
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_PATH_DISCONTINUOUS"


def test_topic_route_rejects_fanout_from_fact_root(sales_release):
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
            AnalysisTopicPathSpec(
                target_model_id="order_items",
                relation_ids=("orders_items",),
            ),
        ),
    )
    release = sales_release.model_copy(update={"analysis_topic_routes": (route,)})

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_FANOUT_PATH"


def test_topic_route_rejects_intermediate_model_outside_dataset_scope(sales_release):
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("order_items", "customers"),
            "metric_ids": (),
            "dimension_ids": (),
        }
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="order_items",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_items", "orders_customer"),
            ),
        ),
    )
    release = sales_release.model_copy(
        update={"datasets": (dataset,), "analysis_topic_routes": (route,)}
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_PATH_MODEL_OUT_OF_SCOPE"


def test_topic_route_rejects_conflicting_prefixes_to_the_same_model(sales_release):
    orders_customer = next(item for item in sales_release.relations if item.id == "orders_customer")
    alternate = orders_customer.model_copy(update={"id": "orders_customer_alt"})
    regions = next(item for item in sales_release.models if item.id == "customers").model_copy(
        update={"id": "regions", "name": "区域", "biz_name": "regions", "table": "regions"}
    )
    customers_regions = orders_customer.model_copy(
        update={
            "id": "customers_regions",
            "left_model_id": "customers",
            "right_model_id": "regions",
        }
    )
    dataset = sales_release.datasets[0].model_copy(
        update={"model_ids": ("orders", "customers", "regions")}
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
            AnalysisTopicPathSpec(
                target_model_id="regions",
                relation_ids=("orders_customer_alt", "customers_regions"),
            ),
        ),
    )
    release = sales_release.model_copy(
        update={
            "models": (*sales_release.models, regions),
            "relations": (*sales_release.relations, alternate, customers_regions),
            "datasets": (dataset,),
            "analysis_topic_routes": (route,),
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_PATH_CONFLICT"


def test_topic_route_rejects_metrics_outside_fact_root(sales_release):
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="customers",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="orders",
                relation_ids=("orders_customer",),
            ),
            AnalysisTopicPathSpec(
                target_model_id="order_items",
                relation_ids=("orders_customer", "orders_items"),
            ),
        ),
    )
    release = sales_release.model_copy(update={"analysis_topic_routes": (route,)})

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_METRIC_OUTSIDE_ROOT"


def test_primary_entities_retain_independent_count_scopes_when_reachable(sales_release):
    """Reviewed QueryScope contract: reachability never removes an entity count root."""

    from knowflow_analytics.contracts import Aggregation, MetricSpec
    from knowflow_analytics.modeling.rule_modeller import stable_id

    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"})
        if item.id in {"orders.id", "customers.id"}
        else item
        for item in sales_release.fields
    )
    customer_count_id = stable_id("metric", "default_count", "customers", "customers.id")
    release = sales_release.model_copy(
        update={
            "fields": fields,
            "metrics": (
                *sales_release.metrics,
                MetricSpec(
                    id=customer_count_id,
                    name="客户数量",
                    model_id="customers",
                    field_id="customers.id",
                    aggregation=Aggregation.COUNT,
                ),
            ),
        }
    )

    proposals = AnalysisTopicProposer().propose(release)

    assert [item.route.root_model_id for item in proposals] == ["customers", "orders"]
    customer_scope = next(item for item in proposals if item.route.root_model_id == "customers")
    assert customer_scope.dataset.metric_ids == (customer_count_id,)
    assert customer_scope.route.default_count_metric_id == customer_count_id


def test_a_count_only_entity_that_no_fact_reaches_still_gets_a_topic(sales_release):
    """An isolated entity is the only way to ask anything about it, so its count
    topic must survive convergence."""

    from knowflow_analytics.contracts import Aggregation, MetricSpec
    from knowflow_analytics.modeling.rule_modeller import stable_id

    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"})
        if item.id in {"orders.id", "customers.id"}
        else item
        for item in sales_release.fields
    )
    customer_count_id = stable_id("metric", "default_count", "customers", "customers.id")
    release = sales_release.model_copy(
        update={
            "fields": fields,
            "relations": (),  # nothing reaches customers any more
            "metrics": (
                *sales_release.metrics,
                MetricSpec(
                    id=customer_count_id,
                    name="客户数量",
                    model_id="customers",
                    field_id="customers.id",
                    aggregation=Aggregation.COUNT,
                ),
            ),
        }
    )

    proposals = AnalysisTopicProposer().propose(release)

    assert "customers" in {item.route.root_model_id for item in proposals}


def test_metric_owner_without_a_primary_identifier_gets_a_scope_without_default_count(
    sales_release,
):
    """Reviewed QueryScope contract: metric ownership, not primary-key presence, creates a root."""

    proposals = AnalysisTopicProposer().propose(sales_release)

    assert [item.route.root_model_id for item in proposals] == ["orders"]
    scope = proposals[0]
    assert scope.route.default_count_metric_id is None
    assert scope.dataset.metric_ids == tuple(sorted(item.id for item in sales_release.metrics))
    assert {
        next(item for item in sales_release.metrics if item.id == metric_id).model_id
        for metric_id in scope.dataset.metric_ids
    } == {scope.route.root_model_id}


def test_scope_roots_and_members_are_invariant_to_names_and_catalog_order(sales_release):
    """Metamorphic guard: the compiler cannot infer roots or members from business wording."""

    extra_metric = MetricSpec(
        id="entity_metric",
        name="Entity metric",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    release = sales_release.model_copy(update={"metrics": (*sales_release.metrics, extra_metric)})
    transformed = release.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"name": f"Model {index}"})
                for index, item in enumerate(reversed(release.models))
            ),
            "metrics": tuple(
                item.model_copy(update={"name": f"Metric {index}"})
                for index, item in enumerate(reversed(release.metrics))
            ),
            "dimensions": tuple(
                item.model_copy(update={"name": f"Dimension {index}"})
                for index, item in enumerate(reversed(release.dimensions))
            ),
            "relations": tuple(reversed(release.relations)),
        }
    )

    def signature(candidate):
        return {
            item.route.root_model_id: (
                item.dataset.model_ids,
                item.dataset.metric_ids,
                item.dataset.dimension_ids,
                item.route.default_count_metric_id,
                item.route.paths,
                item.exclusions,
            )
            for item in AnalysisTopicProposer().propose(candidate)
        }

    assert signature(transformed) == signature(release)
    assert set(signature(release)) == {"orders", "customers"}


def test_scope_excludes_dimensions_backed_by_technical_identifiers(sales_release):
    technical_dimension = DimensionSpec(
        id="customer_key_dimension",
        name="Customer key",
        model_id="customers",
        field_id="customers.id",
        semantic_type="identifier",
    )
    release = sales_release.model_copy(
        update={"dimensions": (*sales_release.dimensions, technical_dimension)}
    )

    scope = AnalysisTopicProposer().propose(release)[0]

    assert technical_dimension.id not in scope.dataset.dimension_ids
    assert any(
        item.element_id == technical_dimension.id and item.reason_code == "technical_identifier"
        for item in scope.exclusions
    )


def test_scope_keeps_the_unique_shortest_path_over_a_longer_alternative(sales_release):
    """2026-08-28 合同修订：歧义的判据是"存在等长的另一条最短路径"。

    旧规则是"存在任何其它路径就丢弃",代价是整个实体退出作用域。音乐六表实测：
    翻唱歌曲 经直接外键(1 跳)、经歌曲(2 跳)、经歌曲→专辑(3 跳)三条路径可达
    歌手,旧规则因此把 歌手 整体排除,「各歌手的翻唱评分」「各原唱歌手的歌曲时长」
    这类问题全部无解;改判后三题的 JOIN 全部走对(分组结果与参考逐名一致)。

    一条严格更长的绕行是另一种派生关系,不是对同一关系的竞争解释;真正的歧义
    (同一对模型间两条外键、或两条等长绕行)仍然 fail-closed。作用域实际选用了
    哪条关系、哪些实体到不了,由 SCOPE_ENTITY_NOT_REACHABLE 诊断对建模者显式说明。
    """

    direct = RelationSpec(
        id="items_customer_direct",
        left_model_id="order_items",
        right_model_id="customers",
        cardinality=Cardinality.MANY_TO_ONE,
        conditions=(
            RelationCondition(
                left_field_id="order_items.order_id",
                right_field_id="customers.id",
            ),
        ),
    )
    metric = MetricSpec(
        id="line_metric",
        name="Line metric",
        model_id="order_items",
        field_id="order_items.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    release = sales_release.model_copy(
        update={
            "metrics": (metric,),
            "relations": (*sales_release.relations, direct),
            "datasets": (),
        }
    )

    scope = AnalysisTopicProposer().propose(release)[0]

    assert scope.route.root_model_id == "order_items"
    # 直接外键是唯一最短路径(1 跳),经 orders 的绕行更长,不构成歧义。
    assert "customers" in scope.dataset.model_ids
    customer_paths = [
        item.relation_ids for item in scope.route.paths if item.target_model_id == "customers"
    ]
    assert customer_paths == [("items_customer_direct",)]
    assert not any(item.reason_code == "ambiguous_safe_path" for item in scope.exclusions)


def test_scope_recompilation_preserves_reviewed_context(sales_release):
    reviewed = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        ai_context="reviewed scope context",
    )
    release = sales_release.model_copy(update={"analysis_topic_routes": (reviewed,)})

    scope = AnalysisTopicProposer().propose(release)[0]

    assert scope.route.dataset_id == reviewed.dataset_id
    assert scope.route.ai_context == reviewed.ai_context


def test_scope_recompilation_rejects_multiple_existing_scopes_for_one_root(sales_release):
    alternate_dataset = sales_release.datasets[0].model_copy(update={"id": "alternate_scope"})
    routes = (
        AnalysisTopicRouteSpec(
            dataset_id="sales_dataset",
            root_model_id="orders",
            ai_context="first reviewed context",
        ),
        AnalysisTopicRouteSpec(
            dataset_id=alternate_dataset.id,
            root_model_id="orders",
            ai_context="first reviewed context",
        ),
    )
    release = sales_release.model_copy(
        update={
            "datasets": (*sales_release.datasets, alternate_dataset),
            "analysis_topic_routes": routes,
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        AnalysisTopicProposer().propose(release)

    assert raised.value.code == "ANALYSIS_TOPIC_ROOT_CONFLICT"


def test_topic_route_rejects_an_exposed_technical_identifier(sales_release):
    technical_dimension = DimensionSpec(
        id="root_key_dimension",
        name="Root key",
        model_id="orders",
        field_id="orders.id",
        semantic_type="identifier",
    )
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders",),
            "dimension_ids": (technical_dimension.id,),
        }
    )
    route = AnalysisTopicRouteSpec(dataset_id=dataset.id, root_model_id="orders")
    release = sales_release.model_copy(
        update={
            "dimensions": (*sales_release.dimensions, technical_dimension),
            "datasets": (dataset,),
            "analysis_topic_routes": (route,),
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_TECHNICAL_IDENTIFIER_EXPOSED"


def test_scope_rejects_duplicate_canonical_names_across_metric_and_dimension(
    sales_release,
):
    metrics = tuple(
        item.model_copy(update={"name": "Revenue"}) if item.id == "net_revenue" else item
        for item in sales_release.metrics
    )
    dimensions = tuple(
        item.model_copy(update={"name": "ＲＥＶＥＮＵＥ"})
        if item.id == "customer_segment"
        else item
        for item in sales_release.dimensions
    )
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "metric_ids": ("net_revenue",),
            "dimension_ids": ("region", "customer_segment"),
        }
    )
    route = AnalysisTopicRouteSpec(
        dataset_id=dataset.id,
        root_model_id="orders",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
        ),
    )
    release = sales_release.model_copy(
        update={
            "metrics": metrics,
            "dimensions": dimensions,
            "datasets": (dataset,),
            "analysis_topic_routes": (route,),
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_analysis_topic_route(release, route)

    assert raised.value.code == "ANALYSIS_TOPIC_CANONICAL_NAME_CONFLICT"

    qualified_route = route.model_copy(
        update={"paths": (route.paths[0].model_copy(update={"prefix": "Customer"}),)}
    )
    qualified_release = release.model_copy(update={"analysis_topic_routes": (qualified_route,)})
    validate_analysis_topic_route(qualified_release, qualified_route)
    assert scope_canonical_names(qualified_release, qualified_route) == {
        "customer_segment": "Customer.ＲＥＶＥＮＵＥ",
        "net_revenue": "Revenue",
        "region": "区域",
    }


def test_scope_compilation_qualifies_cross_model_names_independent_of_order(
    sales_release,
):
    metrics = tuple(
        item.model_copy(update={"name": "Metric"}) if item.id == "net_revenue" else item
        for item in sales_release.metrics
    )
    dimensions = tuple(
        item.model_copy(update={"name": "ｍｅｔｒｉｃ"}) if item.id == "customer_segment" else item
        for item in sales_release.dimensions
    )
    release = sales_release.model_copy(update={"metrics": metrics, "dimensions": dimensions})

    routes = []
    for candidate in (
        release,
        release.model_copy(
            update={
                "metrics": tuple(reversed(release.metrics)),
                "dimensions": tuple(reversed(release.dimensions)),
                "relations": tuple(reversed(release.relations)),
            }
        ),
    ):
        route = AnalysisTopicProposer().propose(candidate)[0].route
        routes.append(route)

        customer_path = next(item for item in route.paths if item.target_model_id == "customers")
        assert customer_path.prefix == "客户"

    assert routes[1] == routes[0]


def test_scope_compilation_rejects_same_root_canonical_name_conflicts_independent_of_order(
    sales_release,
):
    metrics = tuple(
        item.model_copy(update={"name": "Revenue"})
        if item.id == "net_revenue"
        else item.model_copy(update={"name": "ＲＥＶＥＮＵＥ"})
        if item.id == "refund_amount"
        else item
        for item in sales_release.metrics
    )
    release = sales_release.model_copy(update={"metrics": metrics})

    for candidate in (
        release,
        release.model_copy(update={"metrics": tuple(reversed(release.metrics))}),
    ):
        with pytest.raises(SemanticValidationError) as raised:
            AnalysisTopicProposer().propose(candidate)

        assert raised.value.code == "ANALYSIS_TOPIC_CANONICAL_NAME_CONFLICT"


def test_scope_still_excludes_a_target_reachable_by_two_equally_short_paths(sales_release):
    """两条等长最短路径仍是真歧义：谁也不比谁更直接，必须 fail-closed。

    现实形态是同一实体被同一张表的两个外键引用（订单.发货地址id / 账单地址id
    都指向 地址）。这类才需要 role-playing 具名角色维度，而翻译器的别名按模型
    索引，同一张表在一次查询里只能有一个别名，因此现阶段保持排除。
    """
    from knowflow_analytics.contracts import RelationCondition, RelationSpec

    second_direct = RelationSpec(
        id="items_customer_alternate",
        left_model_id="order_items",
        right_model_id="customers",
        cardinality=Cardinality.MANY_TO_ONE,
        conditions=(
            RelationCondition(
                left_field_id="order_items.id",
                right_field_id="customers.id",
            ),
        ),
    )
    direct = RelationSpec(
        id="items_customer_direct",
        left_model_id="order_items",
        right_model_id="customers",
        cardinality=Cardinality.MANY_TO_ONE,
        conditions=(
            RelationCondition(
                left_field_id="order_items.order_id",
                right_field_id="customers.id",
            ),
        ),
    )
    metric = MetricSpec(
        id="line_metric",
        name="Line metric",
        model_id="order_items",
        field_id="order_items.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    release = sales_release.model_copy(
        update={
            "metrics": (metric,),
            "relations": (*sales_release.relations, direct, second_direct),
            "datasets": (),
        }
    )

    scope = AnalysisTopicProposer().propose(release)[0]

    assert "customers" not in scope.dataset.model_ids
    assert any(
        item.element_id == "customers" and item.reason_code == "ambiguous_safe_path"
        for item in scope.exclusions
    )
