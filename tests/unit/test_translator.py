from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    Cardinality,
    DatasetSpec,
    DimensionSpec,
    FieldSpec,
    FilterOperator,
    FixedFilter,
    JoinType,
    MetricSpec,
    ModelSpec,
    QueryAggregationOverride,
    QueryFilter,
    QueryMeasureFilter,
    QueryMetricFilter,
    QueryOrder,
    RelationCondition,
    RelationSpec,
    SemanticQuery,
    SemanticQueryType,
    SemanticRelease,
    SortDirection,
)
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.semantic import SemanticTranslator


def _release_with_order_identifier(sales_release):
    fields = tuple(
        field.model_copy(update={"identifier_type": "primary"})
        if field.id == "orders.id"
        else field
        for field in sales_release.fields
    )
    order_id = DimensionSpec(
        id="order_id",
        name="订单ID",
        model_id="orders",
        field_id="orders.id",
        semantic_type="identifier",
    )
    dataset = sales_release.datasets[0].model_copy(
        update={
            "dimension_ids": (*sales_release.datasets[0].dimension_ids, order_id.id),
        }
    )
    return sales_release.model_copy(
        update={
            "fields": fields,
            "dimensions": (*sales_release.dimensions, order_id),
            "datasets": (dataset,),
        }
    )


def test_detail_query_projects_raw_measure_and_uses_parameterized_where(sales_release):
    release = _release_with_order_identifier(sales_release)

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            query_type=SemanticQueryType.DETAIL,
            metric_ids=("refund_amount",),
            dimension_ids=("order_id",),
            measure_filters=(
                QueryMeasureFilter(
                    metric_id="refund_amount",
                    operator=FilterOperator.GT,
                    value=0,
                ),
            ),
        ),
    )

    assert '"m0"."id" AS "order_id"' in physical.sql
    assert '"m0"."refund_amount" AS "refund_amount"' in physical.sql
    assert 'SUM("m0"."refund_amount")' not in physical.sql
    assert 'WHERE ("m0"."refund_amount" > :p0)' in physical.sql
    assert " GROUP BY " not in physical.sql
    assert physical.parameters == {"p0": 0}


def test_aggregate_metric_filter_translates_to_parameterized_having(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("refund_amount",),
            dimension_ids=("region",),
            metric_filters=(
                QueryMetricFilter(
                    metric_id="refund_amount",
                    operator=FilterOperator.GT,
                    value=10,
                ),
            ),
        ),
    )

    assert 'GROUP BY "m0"."region"' in physical.sql
    assert 'HAVING (SUM("m0"."refund_amount") > :p0)' in physical.sql
    assert physical.parameters == {"p0": 10}


def test_measure_filter_value_is_never_interpolated_into_sql(sales_release):
    release = _release_with_order_identifier(sales_release)
    hostile_value = "0); DROP TABLE orders; --"

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            query_type=SemanticQueryType.DETAIL,
            metric_ids=("refund_amount",),
            dimension_ids=("order_id",),
            measure_filters=(
                QueryMeasureFilter(
                    metric_id="refund_amount",
                    operator=FilterOperator.EQ,
                    value=hostile_value,
                ),
            ),
        ),
    )

    assert hostile_value not in physical.sql
    assert physical.parameters == {"p0": hostile_value}


def test_detail_filter_is_independent_of_semantic_ids_names_and_schema_shape():
    release = SemanticRelease(
        id="release_finance_holdout",
        project_id="finance_holdout",
        spec_hash="holdout-v1",
        models=(
            ModelSpec(
                id="ledger_model",
                name="财务流水",
                schema_name="finance_holdout",
                table="ledger_entries",
            ),
        ),
        fields=(
            FieldSpec(
                id="ledger.entry_key",
                model_id="ledger_model",
                name="流水主键",
                column="entry_key",
                kind="identifier",
                identifier_type="primary",
            ),
            FieldSpec(
                id="ledger.adjustment_value",
                model_id="ledger_model",
                name="调整值",
                column="adjustment_value",
                data_type="numeric",
                kind="measure",
            ),
        ),
        dimensions=(
            DimensionSpec(
                id="entry_identity",
                name="流水编号",
                model_id="ledger_model",
                field_id="ledger.entry_key",
                semantic_type="identifier",
            ),
        ),
        metrics=(
            MetricSpec(
                id="adjustment_measure",
                name="调整金额",
                model_id="ledger_model",
                field_id="ledger.adjustment_value",
                aggregation="sum",
            ),
        ),
        datasets=(
            DatasetSpec(
                id="finance_holdout_dataset",
                name="财务调整",
                model_ids=("ledger_model",),
                metric_ids=("adjustment_measure",),
                dimension_ids=("entry_identity",),
            ),
        ),
    )

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="finance_holdout_dataset",
            query_type=SemanticQueryType.DETAIL,
            metric_ids=("adjustment_measure",),
            dimension_ids=("entry_identity",),
            measure_filters=(
                QueryMeasureFilter(
                    metric_id="adjustment_measure",
                    operator=FilterOperator.LTE,
                    value=-2.5,
                ),
            ),
        ),
    )

    assert 'FROM "finance_holdout"."ledger_entries" AS "m0"' in physical.sql
    assert '"m0"."entry_key" AS "entry_identity"' in physical.sql
    assert 'WHERE ("m0"."adjustment_value" <= :p0)' in physical.sql
    assert physical.parameters == {"p0": -2.5}


def test_translates_single_model_topn_with_bound_filters(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
            filters=(
                QueryFilter(
                    dimension_id="channel",
                    operator=FilterOperator.IN,
                    value=["直营", "电商"],
                ),
            ),
            order_by=(QueryOrder(element_id="net_revenue", direction=SortDirection.DESC),),
            limit=10,
        ),
    )

    assert 'SUM("m0"."net_amount") AS "net_revenue"' in physical.sql
    assert 'GROUP BY "m0"."region"' in physical.sql
    assert 'ORDER BY "net_revenue" DESC LIMIT 11' in physical.sql
    assert physical.result_limit == 10
    assert physical.parameters == {"p0": "直营", "p1": "电商"}
    assert physical.relation_ids == ()


def test_query_aggregation_override_changes_only_this_physical_query(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            aggregation_overrides=(
                QueryAggregationOverride(
                    metric_id="net_revenue",
                    aggregation=Aggregation.AVG,
                ),
            ),
        ),
    )

    assert 'AVG("m0"."net_amount") AS "net_revenue"' in physical.sql
    assert next(item for item in sales_release.metrics if item.id == "net_revenue").aggregation is (
        Aggregation.SUM
    )


def test_query_aggregation_override_cannot_change_count_distinct_identity(sales_release):
    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=sales_release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("order_count",),
                aggregation_overrides=(
                    QueryAggregationOverride(
                        metric_id="order_count",
                        aggregation=Aggregation.SUM,
                    ),
                ),
            ),
        )

    assert exc_info.value.code == "COUNT_DISTINCT_AGGREGATION_OVERRIDE_FORBIDDEN"


def test_query_can_apply_count_distinct_to_an_atomic_metric(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            aggregation_overrides=(
                QueryAggregationOverride(
                    metric_id="net_revenue",
                    aggregation=Aggregation.COUNT_DISTINCT,
                ),
            ),
        ),
    )

    assert 'COUNT(DISTINCT "m0"."net_amount") AS "net_revenue"' in physical.sql


def test_translates_many_to_one_dimension_without_fanout(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("customer_segment",),
        ),
    )

    assert 'LEFT JOIN "analytics_v0"."customers" AS "m1"' in physical.sql
    assert physical.relation_ids == ("orders_customer",)
    assert physical.applied_defaults == ("limit",)


def test_rejects_one_to_many_fanout(sales_release):
    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=sales_release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("net_revenue",),
                dimension_ids=("product",),
            ),
        )

    assert exc_info.value.code == "FANOUT_RISK"


def test_join_planner_cannot_traverse_a_model_outside_the_dataset(sales_release):
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": ("region", "customer_segment"),
        }
    )
    relations = (
        next(item for item in sales_release.relations if item.id == "orders_items"),
        RelationSpec(
            id="items_customer",
            left_model_id="order_items",
            right_model_id="customers",
            cardinality=Cardinality.ONE_TO_ONE,
            conditions=(
                RelationCondition(
                    left_field_id="order_items.order_id",
                    right_field_id="customers.id",
                ),
            ),
        ),
    )
    release = sales_release.model_copy(update={"datasets": (dataset,), "relations": relations})

    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("net_revenue",),
                dimension_ids=("customer_segment",),
            ),
        )

    assert exc_info.value.code == "MISSING_JOIN_PATH"


def test_dimension_only_query_is_distinct_and_deterministic(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            dimension_ids=("region",),
        ),
    )

    assert physical.sql.startswith('SELECT DISTINCT "m0"."region" AS "region"')
    assert 'ORDER BY "region" ASC LIMIT 101' in physical.sql


def test_metrics_with_different_fixed_filter_scopes_share_one_query(sales_release):
    """口径不同的指标同框:过滤下推到聚合内部,而不是拒答。

    这类问题是「A 在 B 中的占比」的必要形态。共享 WHERE 无法表达它——把
    net_revenue 的地区过滤放进 WHERE 会连带砍掉 refund_amount,实测占比恒等于
    1.0:SQL 合法、数字看着正常、结论是错的。条件聚合在真实库上与逐指标独立
    查询的结果完全一致(SUM/COUNT/COUNT_DISTINCT/AVG/MIN/MAX 六种聚合均已实测)。
    """

    metrics = tuple(
        metric.model_copy(
            update={
                "filters": (
                    FixedFilter(
                        field_id="orders.region",
                        operator=FilterOperator.EQ,
                        value="华东",
                    ),
                )
            }
        )
        if metric.id == "net_revenue"
        else metric
        for metric in sales_release.metrics
    )
    release = sales_release.model_copy(update={"metrics": metrics})

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue", "refund_amount"),
        ),
    )

    # 带口径的指标包进 CASE WHEN,不带口径的保持裸聚合。
    assert "CASE WHEN" in physical.sql
    assert "华东" in physical.parameters.values()
    # 口径没有泄漏进共享 WHERE,否则会连带过滤另一个指标。
    assert " WHERE " not in physical.sql


def test_metrics_sharing_one_filter_scope_keep_the_shared_where(sales_release):
    """口径一致时仍走共享 WHERE:语义等价且更省,现有行为不变。"""

    scope = (
        FixedFilter(
            field_id="orders.region",
            operator=FilterOperator.EQ,
            value="华东",
        ),
    )
    metrics = tuple(
        metric.model_copy(update={"filters": scope})
        if metric.id in {"net_revenue", "refund_amount"}
        else metric
        for metric in sales_release.metrics
    )
    release = sales_release.model_copy(update={"metrics": metrics})

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue", "refund_amount"),
        ),
    )

    assert " WHERE " in physical.sql
    assert "CASE WHEN" not in physical.sql


def test_model_fixed_filter_is_applied_inside_each_model_source(sales_release):
    models = tuple(
        model.model_copy(
            update={
                "filter_sql": "id > 0",
                "filters": (
                    FixedFilter(
                        field_id="customers.id",
                        operator=FilterOperator.GT,
                        value=0,
                    ),
                ),
            }
        )
        if model.id == "customers"
        else model
        for model in sales_release.models
    )
    release = sales_release.model_copy(update={"models": models})

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("customer_segment",),
        ),
    )

    assert 'LEFT JOIN (SELECT * FROM "analytics_v0"."customers"' in physical.sql
    assert 'WHERE ("id" > :p0)) AS "m1"' in physical.sql
    assert physical.parameters == {"p0": 0}


def test_null_equality_uses_is_null_and_null_in_set_is_rejected(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            dimension_ids=("region",),
            filters=(
                QueryFilter(
                    dimension_id="region",
                    operator=FilterOperator.EQ,
                    value=None,
                ),
            ),
        ),
    )

    assert '"m0"."region" IS NULL' in physical.sql
    assert physical.parameters == {}

    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=sales_release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                dimension_ids=("region",),
                filters=(
                    QueryFilter(
                        dimension_id="region",
                        operator=FilterOperator.IN,
                        value=["华东", None],
                    ),
                ),
            ),
        )

    assert exc_info.value.code == "INVALID_NULL_FILTER"


def test_timestamptz_bound_uses_business_midnight_in_utc(sales_release):
    fields = tuple(
        field.model_copy(update={"data_type": "timestamp with time zone"})
        if field.id == "orders.order_date"
        else field
        for field in sales_release.fields
    )
    dataset = sales_release.datasets[0].model_copy(update={"timezone": "Asia/Shanghai"})
    release = sales_release.model_copy(update={"fields": fields, "datasets": (dataset,)})

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            filters=(
                QueryFilter(
                    dimension_id="order_date",
                    operator=FilterOperator.GTE,
                    value=date(2026, 4, 1),
                ),
            ),
        ),
    )

    assert physical.parameters["p0"] == datetime(2026, 3, 31, 16, tzinfo=UTC)


def test_expands_derived_metric_and_guards_division(sales_release):
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("gross_after_refund",),
            dimension_ids=("region",),
        ),
    )

    assert 'SUM("m0"."net_amount")' in physical.sql
    assert 'SUM("m0"."refund_amount")' in physical.sql


def test_rejects_order_by_unprojected_element(sales_release):
    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=sales_release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("net_revenue",),
                order_by=(QueryOrder(element_id="region"),),
            ),
        )

    assert exc_info.value.code == "INVALID_ORDER_ELEMENT"


@pytest.mark.parametrize(
    ("join_type", "expected_keyword"),
    [
        (JoinType.INNER, "INNER JOIN"),
        (JoinType.LEFT, "LEFT JOIN"),
        (JoinType.RIGHT, "RIGHT JOIN"),
    ],
)
def test_join_sql_preserves_declared_direction_when_traversed_left_to_right(
    sales_release,
    join_type,
    expected_keyword,
):
    relation = sales_release.relations[0].model_copy(update={"join_type": join_type})
    release = sales_release.model_copy(update={"relations": (relation,)})

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("customer_segment",),
        ),
    )

    assert expected_keyword in physical.sql


def test_reverse_traversal_inverts_left_join_instead_of_changing_semantics(sales_release):
    relation = sales_release.relations[0].model_copy(update={"join_type": JoinType.LEFT})
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
    release = sales_release.model_copy(
        update={
            "relations": (relation,),
            "metrics": (*sales_release.metrics, customer_count),
            "datasets": (dataset,),
        }
    )

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("customer_count",),
            dimension_ids=("region",),
        ),
    )

    assert "RIGHT JOIN" in physical.sql


def _release_with_count_metric(sales_release):
    item_count = MetricSpec(
        id="item_count",
        name="订单明细数量",
        model_id="orders",
        field_id="orders.id",
        aggregation=Aggregation.COUNT,
    )
    dataset = sales_release.datasets[0].model_copy(
        update={"metric_ids": (*sales_release.datasets[0].metric_ids, item_count.id)}
    )
    return sales_release.model_copy(
        update={"metrics": (*sales_release.metrics, item_count), "datasets": (dataset,)}
    )


def test_sum_override_on_count_metric_translates_to_count(sales_release) -> None:
    """SUM 覆盖直译会把计数指标展开成 SUM(物理 id)——把「多少件」算成 id 之和
    (2026-08-26 实测 2 → 3 静默错答)。规范化为治理聚合。"""

    release = _release_with_count_metric(sales_release)
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("item_count",),
            aggregation_overrides=(
                QueryAggregationOverride(metric_id="item_count", aggregation=Aggregation.SUM),
            ),
        ),
    )
    assert 'COUNT("m0"."id")' in physical.sql
    assert "SUM" not in physical.sql.upper().replace("COUNT", "")


def test_other_overrides_on_count_metric_fail_closed(sales_release) -> None:
    release = _release_with_count_metric(sales_release)
    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                metric_ids=("item_count",),
                aggregation_overrides=(
                    QueryAggregationOverride(metric_id="item_count", aggregation=Aggregation.AVG),
                ),
            ),
        )
    assert exc_info.value.code == "COUNT_AGGREGATION_OVERRIDE_FORBIDDEN"


def test_count_metric_in_detail_query_fails_closed(sales_release) -> None:
    """计数指标的「原始列」是标识(id),行级取值没有业务意义。明细查询里投影它
    会把「销量最高的商品」翻成 ORDER BY id 取行(2026-08-26 实测:界面上标着
    「订单明细数量」的 3 其实是明细表主键值)。fail-closed,与派生指标一致。"""

    release = _release_with_count_metric(sales_release)
    with pytest.raises(TranslationError) as exc_info:
        SemanticTranslator().translate(
            release=release,
            query=SemanticQuery(
                dataset_id="sales_dataset",
                query_type=SemanticQueryType.DETAIL,
                metric_ids=("item_count",),
                dimension_ids=("region",),
            ),
        )
    assert exc_info.value.code == "COUNT_METRIC_IN_DETAIL_QUERY"


def test_sum_metric_detail_projection_stays_allowed(sales_release) -> None:
    """SUM 指标的原始列(金额)行级取值有意义,既有明细行为保持不变。"""

    release = _release_with_count_metric(sales_release)
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            query_type=SemanticQueryType.DETAIL,
            metric_ids=("refund_amount",),
            dimension_ids=("region",),
        ),
    )
    assert '"m0"."refund_amount"' in physical.sql


def _with_derived_formula(sales_release, formula: str):
    return sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"formula": formula})
                if item.id == "gross_after_refund"
                else item
                for item in sales_release.metrics
            )
        }
    )


def test_derived_formula_accepts_the_modeling_scalar_set(sales_release) -> None:
    """派生公式的翻译接受集对齐建模校验器,不再只认四则。

    此前这里是 Python ast 白名单:建模接受 CASE WHEN 等富标量,客户走的文本路径
    也原样翻译,只有 Playground 报 INVALID_METRIC_FORMULA——三方里唯一的异类,
    建模者据此误以为公式存不了。
    """

    release = _with_derived_formula(
        sales_release,
        "CASE WHEN {net_revenue} > 0 THEN {refund_amount} / {net_revenue} ELSE 0 END",
    )
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(dataset_id="sales_dataset", metric_ids=("gross_after_refund",)),
    )

    assert "CASE" in physical.sql
    # 除法防零两条路径同义:0 分母出 NULL,不是数据库报错。
    assert "NULLIF" in physical.sql


def test_cyclic_derived_formula_is_refused(sales_release) -> None:
    release = _with_derived_formula(sales_release, "{gross_after_refund} + 1")

    with pytest.raises(TranslationError) as excinfo:
        SemanticTranslator().translate(
            release=release,
            query=SemanticQuery(dataset_id="sales_dataset", metric_ids=("gross_after_refund",)),
        )
    assert excinfo.value.code == "INVALID_METRIC_FORMULA"
