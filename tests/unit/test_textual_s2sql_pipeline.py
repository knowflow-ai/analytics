from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicRouteSpec,
    FilterOperator,
    FixedFilter,
    MetricSpec,
    SemanticQueryType,
)
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.execution.guard import PhysicalSqlGuard
from knowflow_analytics.query.contracts import MapMode, MappingResult
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.s2sql_ast import textual_query_type
from knowflow_analytics.query.symbols import SemanticSymbolTable
from knowflow_analytics.semantic.s2sql_translator import (
    S2SqlSemanticTranslator,
    _semantic_pairs,
)


class _Gateway:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    def generate_json(self, **_kwargs):
        return {"thought": "按业务语义生成 S2SQL", "sql": self._sql}


def _mapping() -> MappingResult:
    return MappingResult(
        dataset_id="sales_dataset",
        mode=MapMode.STRICT,
        normalized_question="",
        matches=(),
        config_version="test",
    )


def _physical_result_signature(sql: str) -> tuple[str, str, bool]:
    """Return value/order semantics while deliberately ignoring output labels."""

    tree = parse_one(sql, read="postgres")
    projection = tree.expressions[0]
    value_expression = projection.this if isinstance(projection, exp.Alias) else projection
    order = tree.args["order"].expressions[0]
    order_expression = order.this if isinstance(order, exp.Ordered) else order
    if (
        isinstance(order_expression, exp.Column)
        and projection.alias
        and order_expression.name == projection.alias
    ):
        order_expression = value_expression
    return (
        value_expression.sql(dialect="postgres"),
        order_expression.sql(dialect="postgres"),
        bool(order.args.get("desc")),
    )


def test_llm_candidate_keeps_textual_s2sql_as_the_authoritative_contract(
    sales_release,
) -> None:
    """Parity: LLMSqlParser stores parsed/corrected S2SQL, not QueryStructReq."""

    sql = (
        'SELECT "区域", SUM("净收入") FROM "销售经营" '
        'GROUP BY "区域" ORDER BY SUM("净收入") DESC LIMIT 3'
    )
    candidate = LlmS2SqlParser(_Gateway(sql)).parse(
        question="各区域净收入前三名",
        release=sales_release,
        mapping=_mapping(),
        query_id="textual-authority",
    )

    assert candidate.parsed_s2sql == sql
    assert candidate.corrected_s2sql == sql
    assert candidate.query_type is SemanticQueryType.AGGREGATE
    assert not hasattr(candidate, "semantic_query")


def test_textual_translator_registry_order_is_fixed() -> None:
    """Parity: spring.factories QueryParser order is an executable contract."""

    assert S2SqlSemanticTranslator.parser_registry == (
        "SqlVariableParser",
        "StructQueryParser",
        "SqlQueryParser",
        "DefaultDimValueParser",
        "DimExpressionParser",
        "MetricExpressionParser",
        "MetricRatioParser",
        "OntologyQueryParser",
    )


def test_textual_translator_preserves_calculation_expression_without_structured_collapse(
    sales_release,
) -> None:
    """A legal S2SQL calculation must survive through the textual translator path."""

    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders",),
            "dimension_ids": ("region", "channel", "order_date"),
        }
    )
    release = sales_release.model_copy(
        update={
            "datasets": (dataset,),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id="sales_dataset",
                    root_model_id="orders",
                    default_count_metric_id="order_count",
                ),
            ),
        }
    )
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT SUM("净收入") / NULLIF(COUNT(*), 0) AS "平均净收入" FROM "销售经营"'
        ),
    )

    assert translated.query_type is SemanticQueryType.AGGREGATE
    assert translated.metric_ids == ("net_revenue", "order_count")
    assert translated.dimension_ids == ()
    assert "/" in translated.physical_query.sql
    assert "SUM" in translated.physical_query.sql.upper()
    assert "COUNT(DISTINCT" in translated.physical_query.sql.upper()
    assert translated.physical_query.columns[0].name == "平均净收入"


def test_textual_translator_accepts_projection_alias_and_fetch_syntax(
    sales_release,
) -> None:
    """Parity: JSQLParser accepts aliases and FETCH FIRST on ordinary S2SQL."""

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", SUM("净收入") AS "收入" FROM "销售经营" '
            'GROUP BY "区域" ORDER BY "收入" DESC FETCH FIRST 1 ROW ONLY'
        ),
    )

    assert translated.metric_ids == ("net_revenue",)
    assert translated.dimension_ids == ("region",)
    assert "ORDER BY" in translated.physical_query.sql.upper()
    assert translated.physical_query.result_limit == 1


def test_metric_named_projection_alias_does_not_shadow_its_projection_expression(
    sales_release,
) -> None:
    """A projection alias is authoritative only at an alias-reference AST site.

    In particular, ``SUM("净收入") AS "净收入"`` contains both a governed metric
    reference (inside the projection expression) and an identically named output
    alias.  Renaming that output alias, or spelling its ORDER BY as the aggregate
    expression, must not change the governed metric, join route, or physical result
    semantics.
    """

    translations = []
    for projection_alias in ("净收入", "收入"):
        for order_expression in (f'"{projection_alias}"', 'SUM("净收入")'):
            translations.append(
                S2SqlSemanticTranslator().translate(
                    release=sales_release,
                    dataset_id="sales_dataset",
                    corrected_s2sql=(
                        f'SELECT SUM("净收入") AS "{projection_alias}", "客户分层" '
                        'FROM "销售经营" GROUP BY "客户分层" '
                        f"ORDER BY {order_expression} DESC"
                    ),
                )
            )

    assert {item.metric_ids for item in translations} == {("net_revenue",)}
    assert {item.dimension_ids for item in translations} == {("customer_segment",)}
    assert {item.physical_query.relation_ids for item in translations} == {("orders_customer",)}
    result_signatures = {
        _physical_result_signature(item.physical_query.sql) for item in translations
    }
    assert len(result_signatures) == 1
    value_expression, order_expression, descending = result_signatures.pop()
    assert value_expression == order_expression
    assert value_expression.startswith("SUM(")
    assert descending is True
    assert all('"net_amount"' in item.physical_query.sql for item in translations)


def test_projection_alias_from_another_select_cannot_authorize_outer_order_by(
    sales_release,
) -> None:
    """Projection aliases are scoped to their SELECT, not the whole statement."""

    with pytest.raises(SemanticParsingError):
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'WITH helper AS (SELECT "客户分层" AS "_辅助字段_" FROM "销售经营") '
                'SELECT "区域" FROM "销售经营" ORDER BY "_辅助字段_" LIMIT 1'
            ),
        )


def test_projection_alias_must_come_from_the_actual_derived_input(
    sales_release,
) -> None:
    with pytest.raises(SemanticParsingError):
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'WITH one AS (SELECT "区域" AS "_区域_" FROM "销售经营"), '
                'helper AS (SELECT "客户分层" AS "_辅助字段_" FROM "销售经营") '
                'SELECT "_区域_" FROM one ORDER BY "_辅助字段_" LIMIT 1'
            ),
        )


def test_semantic_pair_deduplication_keeps_same_id_across_element_types(
    sales_release,
) -> None:
    colliding_dimension = next(
        item for item in sales_release.dimensions if item.id == "channel"
    ).model_copy(update={"id": "net_revenue"})
    dataset = sales_release.datasets[0].model_copy(
        update={
            "dimension_ids": tuple(
                "net_revenue" if item == "channel" else item
                for item in sales_release.datasets[0].dimension_ids
            )
        }
    )
    release = sales_release.model_copy(
        update={
            "dimensions": tuple(
                colliding_dimension if item.id == "channel" else item
                for item in sales_release.dimensions
            ),
            "datasets": (dataset,),
        }
    )
    symbols = SemanticSymbolTable(release=release, dataset=dataset)
    projection = parse_one(
        'SELECT CONCAT("渠道", "净收入") FROM "销售经营"',
        read="postgres",
    ).expressions[0]

    pairs = _semantic_pairs(projection, symbols, {})

    assert {(item.kind, item.id) for item in pairs} == {
        ("dimension", "net_revenue"),
        ("metric", "net_revenue"),
    }

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id=dataset.id,
        corrected_s2sql=(
            'SELECT CONCAT("渠道", SUM("净收入")) AS "_混合口径_" FROM "销售经营" GROUP BY "渠道"'
        ),
    )
    assert translated.audit_query.metric_ids == ("net_revenue",)
    assert translated.audit_query.dimension_ids == ("net_revenue",)


def test_order_by_prefers_the_current_projection_alias_over_another_metric_name(
    sales_release,
) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT SUM("净收入") AS "退款金额", '
            'SUM("退款金额") AS "退款合计", "区域" '
            'FROM "销售经营" GROUP BY "区域" ORDER BY "退款金额" DESC'
        ),
    )

    assert set(translated.metric_ids) == {"net_revenue", "refund_amount"}
    assert translated.audit_query.order_by[0].element_id == "net_revenue"
    physical = parse_one(translated.physical_query.sql, read="postgres")
    assert [item.alias for item in physical.expressions[:2]] == ["退款金额", "退款合计"]
    ordered = physical.args["order"].expressions[0]
    order_expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
    assert isinstance(order_expression, exp.Column)
    assert order_expression.name == "退款金额"


def test_count_star_without_a_resolvable_grain_still_fails(
    sales_release,
) -> None:
    """A multi-model dataset with no confirmed count metric and no fact root has
    no single grain COUNT(*) could mean, so translation must not guess one."""

    with pytest.raises(SemanticParsingError) as exc_info:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql='SELECT COUNT(*) FROM "销售经营"',
        )
    assert exc_info.value.code == "S2SQL_DEFAULT_COUNT_METRIC_REQUIRED"


def test_count_star_over_derived_rows_is_not_rebound_to_fact_count_metric(
    sales_release,
) -> None:
    dataset = sales_release.datasets[0].model_copy(update={"model_ids": ("orders",)})
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == "orders.id" else item
        for item in sales_release.fields
    )
    release = sales_release.model_copy(
        update={
            "fields": fields,
            "datasets": (dataset,),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id="sales_dataset",
                    root_model_id="orders",
                    default_count_metric_id="order_count",
                ),
            ),
        }
    )

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "_target_" AS ('
            'SELECT SUM("净收入") AS "_amount_" FROM "销售经营" '
            "WHERE \"区域\" = '华东'"
            ") "
            'SELECT COUNT(*) + 1 AS "_rank_" FROM ('
            'SELECT SUM("净收入") AS "_amount_" FROM "销售经营" GROUP BY "渠道" '
            'HAVING SUM("净收入") > (SELECT "_amount_" FROM "_target_")'
            ') AS "_larger_"'
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert "COUNT(*)" in sql
    assert 'COUNT("__KF_FIELD' not in sql
    PhysicalSqlGuard().validate(query=translated.physical_query, release=release)


def test_cte_alias_predicates_do_not_override_textual_s2sql_authority(
    sales_release,
) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "_group_amounts_" AS ('
            'SELECT SUM("净收入") AS "_amount_", "区域" '
            'FROM "销售经营" GROUP BY "区域"'
            ") "
            'SELECT 1 + COUNT(*) AS "_rank_" FROM "_group_amounts_" '
            'WHERE "_amount_" > ('
            'SELECT "_amount_" FROM "_group_amounts_" WHERE "区域" = \'华南\''
            ")"
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert "COUNT(*)" in sql
    assert translated.audit_query.filters[0].dimension_id == "region"
    assert translated.audit_query.filters[0].value == "华南"
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_textual_translator_preserves_cte_and_subquery_shapes(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "明细" AS ('
            'SELECT "区域", "净收入" FROM "销售经营" WHERE "净收入" > 0'
            ") "
            'SELECT "区域", SUM("净收入") FROM "明细" GROUP BY "区域"'
        ),
    )

    sql = translated.physical_query.sql
    assert "WITH" in sql.upper()
    assert "明细" in sql
    assert "SUM" in sql.upper()
    assert translated.metric_ids == ("net_revenue",)
    assert translated.dimension_ids == ("region",)


def test_default_dimension_values_follow_the_parser_stage(sales_release) -> None:
    release = sales_release.model_copy(
        update={
            "dimensions": tuple(
                item.model_copy(update={"default_values": ("华东",)})
                if item.id == "region"
                else item
                for item in sales_release.dimensions
            )
        }
    )

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
    )

    assert "__kf_field" in translated.physical_query.sql
    assert " IN " in translated.physical_query.sql.upper()
    assert "华东" in translated.physical_query.parameters.values()


def test_nested_aggregation_can_reference_a_cte_projection_alias(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "_分组_" AS ('
            'SELECT "区域", SUM("净收入") AS "_收入_" FROM "销售经营" '
            'GROUP BY "区域"'
            ") "
            'SELECT MAX("_收入_") AS "_最高收入_" FROM "_分组_"'
        ),
    )

    assert "_分组_" in translated.physical_query.sql
    assert 'MAX("_收入_")' in translated.physical_query.sql
    assert translated.metric_ids == ("net_revenue",)


def test_query_type_uses_select_functions_not_where_functions() -> None:
    assert (
        textual_query_type('SELECT "区域" FROM "销售经营" WHERE LOWER("区域") = \'华东\'')
        is SemanticQueryType.DETAIL
    )


def _release_with_region_default(sales_release):
    return sales_release.model_copy(
        update={
            "dimensions": tuple(
                item.model_copy(update={"default_values": ("华东",)})
                if item.id == "region"
                else item
                for item in sales_release.dimensions
            )
        }
    )


def test_an_unrelated_where_clause_no_longer_drops_the_dimension_default(
    sales_release,
) -> None:
    """WHERE 打在指标上不构成对 region 的约束,默认值必须保留。

    上游 ``DefaultDimValueParser`` 是 ``if (!isEmpty(whereFields)) return``,
    WHERE 里出现任何列就丢掉全部默认值。对用户而言这是静默错答:问「北京
    地区销售额」会让「只算有效订单」这类口径一并消失。此处刻意与上游分歧。
    """

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_region_default(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "_明细_" AS ('
            'SELECT "净收入" FROM "销售经营" WHERE "净收入" > 0'
            ') SELECT SUM("净收入") FROM "_明细_"'
        ),
    )

    assert "华东" in translated.physical_query.parameters.values()


def test_filtering_that_dimension_releases_its_own_default(sales_release) -> None:
    """用户自己约束了 region,就不再叠加治理默认值。"""

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_region_default(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华南\'',
    )

    values = translated.physical_query.parameters.values()
    assert "华南" in values
    assert "华东" not in values


def test_projecting_that_dimension_releases_its_own_default(sales_release) -> None:
    """「各地区销售额」不能被默认值锁死在单个地区。"""

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_region_default(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
    )

    assert "华东" not in translated.physical_query.parameters.values()


@pytest.mark.parametrize("operator", ("UNION", "UNION ALL", "INTERSECT", "EXCEPT"))
def test_textual_translator_preserves_governed_set_operations(
    sales_release,
    operator: str,
) -> None:
    """Parity: JSQLParser accepts SetOperationList and translates every SELECT branch."""

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域" FROM "销售经营" WHERE "渠道" = \'直营\' '
            f"{operator} "
            'SELECT "区域" FROM "销售经营" WHERE "渠道" = \'电商\''
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert operator in sql
    assert translated.dimension_ids == ("region",)
    assert set(translated.physical_query.parameters.values()) == {"直营", "电商"}
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_set_operation_requires_compatible_governed_projection_shapes(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域" FROM "销售经营" UNION SELECT "区域", "渠道" FROM "销售经营"'
            ),
        )

    assert raised.value.code == "S2SQL_SET_OPERATION_SHAPE_MISMATCH"


def test_set_operation_rejects_a_physical_table_in_any_branch(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域" FROM "销售经营" UNION SELECT region FROM analytics_v0.orders'
            ),
        )

    assert raised.value.code == "LLM_S2SQL_AST_INVALID"


def test_set_operation_translates_each_branch_with_its_own_join_route(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=('SELECT "区域" FROM "销售经营" UNION SELECT "客户分层" FROM "销售经营"'),
    )

    sql = translated.physical_query.sql
    assert '"__kf_dataset_0"' in sql
    assert '"__kf_dataset_1"' in sql
    assert '"analytics_v0"."orders"' in sql
    assert '"analytics_v0"."customers"' in sql
    assert translated.physical_query.relation_ids == ()


@pytest.mark.parametrize(
    ("operator", "expected_interval"),
    (("RATIO_ROLL", "1 MONTH"), ("RATIO_OVER", "1 YEAR")),
)
def test_metric_ratio_parser_generates_one_snapshot_period_comparison(
    sales_release,
    operator: str,
    expected_interval: str,
) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
            f'{operator}("净收入") AS "变化率" '
            'FROM "销售经营" GROUP BY DATE_TRUNC(\'month\', "下单日期")'
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert "__KF_RATIO_BASE" in sql
    assert "LEFT JOIN" in sql
    assert expected_interval in sql
    assert "NULLIF" in sql
    assert "COALESCE" in sql
    assert "DOUBLE PRECISION" in sql
    assert translated.metric_ids == ("net_revenue",)
    assert translated.dimension_ids == ("order_date",)
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_ratio_to_total_uses_a_window_over_the_same_grouped_result(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", RATIO_TO_TOTAL("净收入") AS "收入占比" FROM "销售经营" GROUP BY "区域"'
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert "OVER ()" in sql
    assert "NULLIF" in sql
    assert "DOUBLE PRECISION" in sql
    assert translated.metric_ids == ("net_revenue",)
    assert translated.dimension_ids == ("region",)
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_ratio_to_total_without_group_or_explicit_subset_is_rejected(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql='SELECT RATIO_TO_TOTAL("净收入") FROM "销售经营"',
        )

    assert raised.value.code == "S2SQL_RATIO_GROUP_REQUIRED"


def test_ratio_to_total_supports_one_exact_dimension_value_against_unfiltered_total(
    sales_release,
) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT RATIO_TO_TOTAL("净收入", "区域", \'华东\') AS "华东占比" FROM "销售经营"'
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert "FILTER(WHERE" in sql.replace(" ", "")
    assert ")) FILTER" not in sql
    assert "DOUBLE PRECISION" in sql
    assert "OVER ()" not in sql
    assert "华东" in translated.physical_query.parameters.values()
    assert translated.audit_query.filters[0].dimension_id == "region"
    assert translated.audit_query.filters[0].value == "华东"
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_ratio_metric_argument_rejects_a_pre_aggregated_expression(sales_release) -> None:
    """A pre-aggregated ratio argument must fail here, not at the database.

    ``RATIO_TO_TOTAL`` wraps its metric argument in the governed aggregation, so
    an argument that already aggregates becomes ``SUM(SUM(...))``.  PostgreSQL
    rejects that with SQLSTATE 42803 at execution time - after every governed
    stage passed, and too late for the parser retry that can still produce a
    correct candidate.
    """

    for s2sql in (
        'SELECT RATIO_TO_TOTAL(SUM("净收入"), "区域", \'华东\') AS "华东占比" FROM "销售经营"',
        'SELECT "区域", RATIO_TO_TOTAL(SUM("净收入")) AS "占比" FROM "销售经营" GROUP BY "区域"',
    ):
        with pytest.raises(SemanticParsingError) as raised:
            S2SqlSemanticTranslator().translate(
                release=sales_release,
                dataset_id="sales_dataset",
                corrected_s2sql=s2sql,
            )

        assert raised.value.code == "S2SQL_RATIO_METRIC_PRE_AGGREGATED"


def test_period_ratio_requires_one_governed_time_group(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=('SELECT "区域", RATIO_OVER("净收入") FROM "销售经营" GROUP BY "区域"'),
        )

    assert raised.value.code == "S2SQL_RATIO_TIME_REQUIRED"


def test_period_ratio_rejects_mixed_over_and_roll(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
                'RATIO_OVER("净收入"), RATIO_ROLL("退款金额") '
                'FROM "销售经营" GROUP BY DATE_TRUNC(\'month\', "下单日期")'
            ),
        )

    assert raised.value.code == "S2SQL_RATIO_MIXED_MODES"


def test_week_over_week_month_baseline_is_normalized_back_to_week_start(
    sales_release,
) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT DATE_TRUNC(\'week\', "下单日期") AS "周", '
            'RATIO_OVER("净收入") AS "月环比" FROM "销售经营" '
            "GROUP BY DATE_TRUNC('week', \"下单日期\")"
        ),
    )

    sql = translated.physical_query.sql.upper()
    assert sql.count("DATE_TRUNC('WEEK'") >= 3
    assert "1 MONTH" in sql


@pytest.mark.parametrize("limit", ("0", "-1"))
def test_invalid_limit_is_rejected_instead_of_defaulted(sales_release, limit) -> None:
    with pytest.raises(TranslationError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=f'SELECT SUM("净收入") FROM "销售经营" LIMIT {limit}',
        )

    assert getattr(raised.value, "code", None) == "QUERY_LIMIT_EXCEEDED"


def test_multiple_aggregations_of_one_metric_have_unique_result_columns(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT MAX("净收入") AS "_最高收入_", AVG("净收入") AS "_平均收入_" FROM "销售经营"'
        ),
    )

    columns = translated.physical_query.columns
    assert tuple(item.element_id for item in columns) == ("_最高收入_", "_平均收入_")
    assert len({item.element_id for item in columns}) == 2


def test_or_filter_is_reported_as_an_incomplete_audit_projection(sales_release) -> None:
    """The audit projection cannot represent OR predicates, so it must say so
    instead of presenting a filter-free interpretation of a filtered query."""

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\' OR "区域" = \'华南\''
        ),
    )

    assert translated.audit_query.filters == ()
    assert translated.audit_complete is False
    assert "华东" in translated.physical_query.sql or bool(translated.physical_query.parameters)


def test_plain_and_filter_keeps_a_complete_audit_projection(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=('SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\''),
    )

    assert translated.audit_query.filters[0].dimension_id == "region"
    assert translated.audit_complete is True


def test_count_star_without_a_default_count_metric_fails_closed(
    sales_release,
) -> None:
    """Reviewed QueryScope contract (2026-08-27): a no-PK fact scope may expose
    governed business metrics, but it has no authoritative entity-count grain.
    COUNT(*) must not fall back to a unique COUNT candidate or physical row count."""

    dataset = sales_release.datasets[0].model_copy(update={"model_ids": ("orders",)})
    release = sales_release.model_copy(update={"datasets": (dataset,)})

    with pytest.raises(SemanticParsingError) as exc_info:
        S2SqlSemanticTranslator().translate(
            release=release,
            dataset_id="sales_dataset",
            corrected_s2sql='SELECT COUNT(*) FROM "销售经营"',
        )

    assert exc_info.value.code == "S2SQL_DEFAULT_COUNT_METRIC_REQUIRED"


def _release_with_plain_count_metric(sales_release):
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


def test_sum_over_a_count_metric_normalizes_to_the_governed_aggregation(
    sales_release,
) -> None:
    """LLM 手滑把计数指标写成 SUM(件数):SUM(每行记 1 的计数)在单层聚合里就是
    COUNT;照单全收会把指标展开成 SUM(物理 id),把「多少件」算成 id 之和
    (2026-08-26 真实问数实测 2 → 3 静默错答,评测抓到)。治理聚合是权威。"""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("订单明细数量") FROM "销售经营" WHERE "区域" = \'华东\'',
    )
    overrides = {o.metric_id: o.aggregation for o in translated.audit_query.aggregation_overrides}
    assert overrides.get("item_count") is Aggregation.COUNT
    tree = parse_one(translated.physical_query.sql, read="postgres")
    counts = list(tree.find_all(exp.Count))
    assert len(counts) == 1
    assert not isinstance(counts[0].this, exp.Distinct)
    assert list(tree.find_all(exp.Sum)) == []


def test_sum_over_a_text_count_distinct_metric_rewrites_the_physical_aggregate(
    sales_release,
) -> None:
    """Reviewed COUNT contract (2026-08-27), at the textual translator boundary:
    SqlQueryParser resolves SUM(metric), MetricExpressionParser must emit the
    metric's governed COUNT_DISTINCT physical AST, and OntologyQueryParser must
    preserve it in SQL. The identifier field is text, so this also protects
    COUNT_DISTINCT from numeric-only repair assumptions."""

    assert next(item for item in sales_release.fields if item.id == "orders.id").data_type == "text"
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("订单数") FROM "销售经营"',
    )

    overrides = {o.metric_id: o.aggregation for o in translated.audit_query.aggregation_overrides}
    assert overrides.get("order_count") is Aggregation.COUNT_DISTINCT
    tree = parse_one(translated.physical_query.sql, read="postgres")
    counts = list(tree.find_all(exp.Count))
    assert len(counts) == 1
    assert isinstance(counts[0].this, exp.Distinct)
    assert list(tree.find_all(exp.Sum)) == []


@pytest.mark.parametrize(
    "expression",
    [
        'SUM(DISTINCT "订单数")',
        'SUM("订单数" + 0)',
        'SUM(ABS("订单数"))',
        'SUM(CAST("订单数" AS BIGINT))',
        'SUM(SUM("订单数"))',
    ],
)
def test_count_metric_rejects_non_direct_sum_wrappers(sales_release, expression) -> None:
    """Audit aggregation may never claim COUNT while physical SQL still sums IDs."""

    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=f'SELECT {expression} FROM "销售经营"',
        )

    assert raised.value.code == "S2SQL_COUNT_METRIC_WRAPPER_INVALID"


def test_count_over_a_count_metric_stays_count(sales_release) -> None:
    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT COUNT("订单明细数量") FROM "销售经营"',
    )
    overrides = {o.metric_id: o.aggregation for o in translated.audit_query.aggregation_overrides}
    assert overrides.get("item_count") is Aggregation.COUNT


def test_bare_count_metric_is_repaired_not_rejected(sales_release) -> None:
    """裸计数指标列曾经退化成物理 id 列(「销量最高的商品」翻成 ORDER BY id
    取行,2026-08-26 实测),一度改为 fail-closed 拒绝。对齐上游后它被展开成
    治理聚合并回补 GROUP BY,直接答对,不再需要拒绝用户。"""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", "订单明细数量" FROM "销售经营" ORDER BY "订单明细数量" DESC LIMIT 1'
        ),
    )
    assert translated.query_type is SemanticQueryType.AGGREGATE
    sql = translated.physical_query.sql.upper()
    assert "COUNT(" in sql and " GROUP BY " in sql


def test_bare_count_metric_with_existing_group_by_becomes_aggregate(
    sales_release,
) -> None:
    """Reviewed COUNT contract (2026-08-27): an existing GROUP BY must not leave
    QueryTypeParser's pre-expansion DETAIL classification stale after the governed
    count metric expands to COUNT(field)."""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", "订单明细数量" FROM "销售经营" '
            'GROUP BY "区域" ORDER BY "订单明细数量" DESC LIMIT 1'
        ),
    )

    assert translated.query_type is SemanticQueryType.AGGREGATE
    tree = parse_one(translated.physical_query.sql, read="postgres")
    # SELECT and ORDER BY each retain their governed metric expression.
    assert len(list(tree.find_all(exp.Count))) == 2
    assert list(tree.find_all(exp.Sum)) == []


def test_bare_count_metric_restores_group_by_in_every_union_branch(sales_release) -> None:
    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", "订单明细数量" FROM "销售经营" '
            'UNION ALL SELECT "区域", "订单明细数量" FROM "销售经营"'
        ),
    )

    tree = parse_one(translated.physical_query.sql, read="postgres")
    branches = [
        branch
        for branch in tree.find_all(exp.Select)
        if any(isinstance(item, exp.Count) for item in branch.expressions)
        and any(isinstance(item, exp.Column) for item in branch.expressions)
    ]
    assert len(branches) == 2
    assert all(branch.args.get("group") is not None for branch in branches)
    assert all(len(list(branch.find_all(exp.Count))) == 1 for branch in branches)


def test_bare_count_metric_restores_group_by_inside_a_cte(sales_release) -> None:
    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'WITH "分组" AS (SELECT "区域", "订单明细数量" FROM "销售经营") '
            'SELECT COUNT(*) FROM "分组"'
        ),
    )

    tree = parse_one(translated.physical_query.sql, read="postgres")
    cte_select = next(item.this for item in tree.find_all(exp.CTE) if item.alias_or_name == "分组")
    assert isinstance(cte_select, exp.Select)
    assert cte_select.args.get("group") is not None
    assert len(list(cte_select.find_all(exp.Count))) == 1


def test_bare_count_metric_in_where_fails_closed_before_physical_sql(sales_release) -> None:
    release = _release_with_plain_count_metric(sales_release)

    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=release,
            dataset_id="sales_dataset",
            corrected_s2sql=('SELECT "区域" FROM "销售经营" WHERE "订单明细数量" > 1'),
        )

    assert raised.value.code == "S2SQL_COUNT_METRIC_WHERE_INVALID"


def test_aggregated_count_metric_stays_translatable(sales_release) -> None:
    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", COUNT("订单明细数量") FROM "销售经营" '
            'GROUP BY "区域" ORDER BY COUNT("订单明细数量") DESC LIMIT 1'
        ),
    )
    assert translated.query_type is SemanticQueryType.AGGREGATE


def test_bare_metric_expands_to_governed_aggregate_and_regains_group_by(
    sales_release,
) -> None:
    """对齐上游 MetricExpressionParser + SqlReplaceHelper:指标展开为 agg(expr)
    而不是裸物理列,展开后若含聚合函数且缺 GROUP BY 则按剩余裸列回补。
    上游 TranslatorTest.testSql_3 钉死了这一行为(CTE 内投影指标无聚合无
    GROUP BY,断言物理 SQL 含 count(1) 且可执行)。"""

    release = _release_with_plain_count_metric(sales_release)
    bare = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", "订单明细数量" FROM "销售经营" ORDER BY "订单明细数量" DESC LIMIT 1'
        ),
    )
    explicit = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", COUNT("订单明细数量") FROM "销售经营" '
            'GROUP BY "区域" ORDER BY COUNT("订单明细数量") DESC LIMIT 1'
        ),
    )
    # LLM 漏写聚合与写全聚合必须收敛到同一条物理 SQL
    assert bare.physical_query.sql == explicit.physical_query.sql
    assert bare.query_type is SemanticQueryType.AGGREGATE


def test_metric_already_wrapped_is_not_double_aggregated(sales_release) -> None:
    """外层已有聚合时指标保持裸字段,否则会翻成 COUNT(COUNT(id)) 嵌套聚合。"""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT COUNT("订单明细数量") FROM "销售经营"',
    )
    assert "COUNT(COUNT(" not in translated.physical_query.sql.upper().replace(" ", "")
    tree = parse_one(translated.physical_query.sql, read="postgres")
    assert len(list(tree.find_all(exp.Count))) == 1


def test_detail_query_without_metrics_keeps_no_group_by(sales_release) -> None:
    """纯维度明细查询不含聚合,不得被回补 GROUP BY。"""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域" FROM "销售经营"',
    )
    assert " GROUP BY " not in translated.physical_query.sql.upper()
    assert translated.query_type is SemanticQueryType.DETAIL


def test_positional_group_by_and_order_by_stay_literal(sales_release) -> None:
    """位置式序数不能参数化。

    PG 对 ORDER BY $1 既不报错也不排序(PG16 实测 VALUES (3),(1),(2) 原样
    返回),配上 LIMIT 就是静默错的 Top-N;GROUP BY $1 则直接报
    "column must appear in the GROUP BY clause"。序数是语法位置,不是值。
    """

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "区域", COUNT("订单明细数量") FROM "销售经营" '
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 1"
        ),
    )
    sql = translated.physical_query.sql
    assert "GROUP BY 1" in sql
    assert "ORDER BY 2 DESC" in sql
    # 序数不得出现在绑定参数里
    assert 1 not in translated.physical_query.parameters.values()
    assert 2 not in translated.physical_query.parameters.values()


def test_ordinary_literals_are_still_parameterized(sales_release) -> None:
    """只放行序数位置,普通字面量必须继续走绑定参数。"""

    release = _release_with_plain_count_metric(sales_release)
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域" FROM "销售经营" WHERE "区域" = \'华东\'',
    )
    assert "华东" not in translated.physical_query.sql
    assert "华东" in translated.physical_query.parameters.values()


def test_empty_projection_raises_a_governed_error(sales_release) -> None:
    """没有任何字段可投影时必须抛治理错误,不能产出 SELECT  FROM。

    上游 SqlBuilder.buildOntologySql:56-59 在 dataModels 为空时抛
    "data model not found"。我们此前 inner_select 为空串直接拼进 SQL,
    产出非法的 `SELECT  FROM ...`,最终在构造审计 DTO 时抛 pydantic
    ValidationError —— SQL 非法,错误类型还泄漏。
    """

    # 多模型数据集里 COUNT(*) 无法绑定唯一计数指标时,字段令牌集合为空
    release = _release_with_plain_count_metric(sales_release)
    with pytest.raises(TranslationError) as exc_info:
        S2SqlSemanticTranslator().translate(
            release=release,
            dataset_id="sales_dataset",
            corrected_s2sql='SELECT 1 FROM "销售经营"',
        )
    assert exc_info.value.code == "EMPTY_ONTOLOGY_PROJECTION"


def _release_with_two_filter_scopes(sales_release):
    """净收入带地区口径、退款额不带——「A 在 B 中的占比」的必要形态。"""

    return sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(
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
                if item.id == "net_revenue"
                else item
                for item in sales_release.metrics
            )
        }
    )


def test_textual_path_combines_metrics_with_different_filter_scopes(sales_release) -> None:
    """自然语言路径必须和结构化路径一样支持多口径同框。

    这条链是客户实际走的路径(``query``),结构化路径只有建模者在 Playground
    里用。只修结构化等于把修复装在没人走的门上——与半可加守卫同一教训。
    共享 WHERE 无法表达多口径:一个指标的过滤会连带砍掉另一个,实测占比恒
    等于 1.0。
    """

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_two_filter_scopes(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入"), SUM("退款金额") FROM "销售经营"',
    )

    sql = translated.physical_query.sql
    assert "CASE WHEN" in sql
    # 口径没有泄漏进内层共享 WHERE,否则会连带过滤另一个指标。
    inner = sql[: sql.index("SELECT", sql.index("__kf_dataset"))]
    assert " WHERE " not in inner
    # 值走参数化,不内联进 SQL。
    assert "华东" in translated.physical_query.parameters.values()
    assert "华东" not in sql


def test_textual_path_keeps_the_shared_where_for_one_filter_scope(sales_release) -> None:
    """口径一致时仍走内层共享 WHERE:语义等价且更省,现有行为不变。"""

    scope = (FixedFilter(field_id="orders.region", operator=FilterOperator.EQ, value="华东"),)
    release = sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"filters": scope})
                if item.id in {"net_revenue", "refund_amount"}
                else item
                for item in sales_release.metrics
            )
        }
    )

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入"), SUM("退款金额") FROM "销售经营"',
    )

    assert "CASE WHEN" not in translated.physical_query.sql


def test_detail_dimension_projection_is_deduplicated(sales_release) -> None:
    """明细查询只投影维度时强制去重,与结构化路径同一条规则。

    结构化路径一直是 ``dimensions and not metrics -> SELECT DISTINCT``,文本路径
    此前照搬 LLM 写的 SELECT。实测同一个语义查询,LLM 写 DISTINCT 给 2 行、不写
    给 3 行(明细表 join 后重复),而 SemanticQuery 没有 distinct 字段,两条 SQL
    投影成同一个语义查询——评测报告里所有语义字段一致却行数不同,看不出差在哪。
    """

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域" FROM "销售经营"',
    )

    assert "DISTINCT" in translated.physical_query.sql


def test_a_detail_projection_with_a_metric_is_not_deduplicated(sales_release) -> None:
    """带指标就不是「有哪些」类问题,去重会改变含义。"""

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域", "净收入" FROM "销售经营"',
    )

    assert "DISTINCT" not in translated.physical_query.sql


def test_an_aggregate_query_is_not_deduplicated(sales_release) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
    )

    assert "DISTINCT" not in translated.physical_query.sql


def _with_ratio_formula(sales_release):
    """把派生指标改成比率:逐行算和先聚合再算,数字不同,能暴露展开语义。"""

    return sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"formula": "{refund_amount} / {net_revenue}"})
                if item.id == "gross_after_refund"
                else item
                for item in sales_release.metrics
            )
        }
    )


def test_a_derived_metric_expands_aggregate_first_even_inside_sum(sales_release) -> None:
    """派生公式作用在「聚合后的依赖」上,与结构化路径同一契约。

    LLM 习惯写 SUM("指标")。原子指标那层 SUM 就是治理聚合;派生指标的依赖不是
    树里的 token,bare_metric_ids 判不到它们——此前退化成裸列逐行算再求和,
    退款率被算成「逐行 退款/收入 之和」,实测 0.1 对 0.0333(先聚合的正确口径)。
    """

    translated = S2SqlSemanticTranslator().translate(
        release=_with_ratio_formula(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("扣减退款后收入") FROM "销售经营"',
    )

    sql = translated.physical_query.sql
    # 依赖各自聚合;包住派生 token 的外层 SUM 被剥掉,不产生嵌套聚合。
    assert sql.count("SUM(") == 2
    assert "SUM((SUM" not in sql and "SUM(SUM" not in sql
    # 除法防零与结构化路径同义。
    assert "NULLIF" in sql


def test_a_bare_derived_metric_matches_the_wrapped_form(sales_release) -> None:
    release = _with_ratio_formula(sales_release)
    wrapped = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("扣减退款后收入") FROM "销售经营"',
    )
    bare = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT "扣减退款后收入" FROM "销售经营"',
    )

    # 只允许 LIMIT 不同:裸形态被文本分类判成明细,行数上限走明细档(预存行为)。
    strip = lambda sql: sql.rsplit(" LIMIT ", 1)[0]  # noqa: E731
    assert strip(wrapped.physical_query.sql) == strip(bare.physical_query.sql)


def test_a_case_when_derived_formula_translates_on_the_textual_path(sales_release) -> None:
    release = sales_release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(
                    update={
                        "formula": (
                            "CASE WHEN {net_revenue} > 0"
                            " THEN {refund_amount} / {net_revenue} ELSE 0 END"
                        )
                    }
                )
                if item.id == "gross_after_refund"
                else item
                for item in sales_release.metrics
            )
        }
    )

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("扣减退款后收入") FROM "销售经营"',
    )

    assert "CASE" in translated.physical_query.sql
    assert "NULLIF" in translated.physical_query.sql


def _measure_type_metric(formula, sources):
    from knowflow_analytics.contracts import MetricExpressionSource, MetricKind, MetricSpec

    return MetricSpec(
        id="net_revenue",
        name="净收入",
        model_id="orders",
        kind=MetricKind.DERIVED,
        define_type="MEASURE",
        formula=formula,
        expression_sources=tuple(MetricExpressionSource(**kw) for kw in sources),
    )


def _release_with_measure_metric(sales_release, metric):
    return sales_release.model_copy(
        update={
            "metrics": tuple(
                metric if item.id == metric.id else item for item in sales_release.metrics
            )
        }
    )


def test_a_bare_measure_formula_replaces_the_root_column(sales_release) -> None:
    """公式是单个裸度量名时,根节点也必须被替换。

    sqlglot 的 column.replace 对根节点静默失败:整棵树就是那个 Column,裸度量名
    直接漏进物理 SQL(实测 UndefinedColumn)。退化形态编译成 ATOMIC 不走这里,
    生产一直没炸;表达式度量的单引用形态会真实命中。
    """

    metric = _measure_type_metric(
        "net_a",
        [{"name": "net_a", "field_id": "orders.net_amount", "aggregation": Aggregation.SUM}],
    )
    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_measure_metric(sales_release, metric),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
    )

    sql = translated.physical_query.sql
    # 裸名泄漏形态是不带引号的 ` net_a `;"net_a" 是 net_amount 的子串,不能裸查。
    assert " net_a " not in sql and '"net_a"' not in sql
    assert "SUM(" in sql


def test_an_expression_measure_keeps_its_expression_on_the_textual_path(sales_release) -> None:
    """表达式度量的表达式不能被丢:此前只认 field_id,「x2」翻成裸 SUM,数字对半错。"""

    metric = _measure_type_metric(
        "net_a",
        [
            {
                "name": "net_a",
                "field_id": "orders.net_amount",
                "aggregation": Aggregation.SUM,
                "expression": "net_amount * 2",
                "expression_field_ids": ("orders.net_amount",),
            }
        ],
    )
    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_measure_metric(sales_release, metric),
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
    )

    # 字面量 2 被既有的 _parameterize_literals 统一参数化,断言乘法结构+参数值。
    assert " * " in translated.physical_query.sql
    assert 2 in translated.physical_query.parameters.values()


def test_period_ratio_allows_an_aggregate_beside_the_ratio(sales_release) -> None:
    """「按月看净收入和它的同比」：聚合列与比率列并列。

    此前只允许分组维度与比率列并列，模型自然写出的 SUM + RATIO_OVER 被
    S2SQL_RATIO_SHAPE_INVALID 拒答——同一个问题时成时败。聚合列只从当前期
    取值，不能进自连接对齐键，否则只会匹配到两期金额恰好相等的行。
    """

    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT DATE_TRUNC(\'month\', "下单日期") AS "_月份_", '
            'SUM("净收入") AS "_净收入_", '
            'RATIO_OVER("净收入") AS "_同比_" '
            'FROM "销售经营" GROUP BY DATE_TRUNC(\'month\', "下单日期")'
        ),
    )

    sql = translated.physical_query.sql
    assert "__kf_ratio_base" in sql.lower()
    # 聚合列从当前期投影，不作为对齐键。
    assert '"__kf_previous"."_净收入_"' not in sql
    assert '"__kf_current"."_净收入_"' in sql


def test_period_ratio_still_rejects_ungrouped_non_aggregate_projection(
    sales_release,
) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "_月份_", "渠道", '
                'RATIO_OVER("净收入") AS "_同比_" '
                'FROM "销售经营" GROUP BY DATE_TRUNC(\'month\', "下单日期")'
            ),
        )

    assert raised.value.code == "S2SQL_RATIO_SHAPE_INVALID"


class TestModelGivingUpIsNotAnEmptyResult:
    """模型写出永远不成立的条件时，不能当成"查询成功但没有数据"。

    实机（2026-09-03，demo_cafe）：问「哪些门店售卖卡布奇洛」，作用域落到门店分析，
    模型自己在 rationale 里写明"没有产品或销售相关的维度……为了不忽略该条件，使用一个
    永不成立的过滤条件（1=0）来表示该条件无法被满足"，产出
    ``SELECT "门店名称" FROM "门店" WHERE 1=0``。执行成功、0 行，界面显示"查询成功，
    但没有返回数据"——用户读到的是"没有门店卖这个"，一句关于他自己业务的假话。

    空结果和"模型放弃了"必须分开：前者是数据事实，后者是系统没能表达问题。这里在
    翻译阶段确定性识别后者并拒绝，让既有重试链有机会重新生成候选。
    """

    def test_impossible_predicate_is_refused_instead_of_returning_zero_rows(
        self, sales_release
    ) -> None:
        for s2sql in (
            'SELECT "区域" FROM "销售经营" WHERE 1=0',
            'SELECT "区域" FROM "销售经营" WHERE 1 = 0',
            'SELECT "区域" FROM "销售经营" WHERE FALSE',
            'SELECT "区域" FROM "销售经营" WHERE "区域" = \'华东\' AND 1=0',
        ):
            with pytest.raises(SemanticParsingError) as raised:
                S2SqlSemanticTranslator().translate(
                    release=sales_release,
                    dataset_id="sales_dataset",
                    corrected_s2sql=s2sql,
                )

            assert raised.value.code == "S2SQL_CONTRADICTORY_FILTER", s2sql

    def test_a_contradiction_inside_a_subquery_is_also_giving_up(
        self, sales_release
    ) -> None:
        """子查询里的矛盾同样是放弃，只是藏得深一点。"""

        with pytest.raises(SemanticParsingError) as raised:
            S2SqlSemanticTranslator().translate(
                release=sales_release,
                dataset_id="sales_dataset",
                corrected_s2sql=(
                    'SELECT "区域" FROM "销售经营" '
                    'WHERE "区域" IN (SELECT "区域" FROM "销售经营" WHERE 1=0)'
                ),
            )

        assert raised.value.code == "S2SQL_CONTRADICTORY_FILTER"

    def test_no_op_true_predicates_are_left_alone(self, sales_release) -> None:
        """``1=1`` 是无害的占位，不是放弃——拒了它会误伤正常查询。"""

        translated = S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域", SUM("净收入") FROM "销售经营" '
                "WHERE 1=1 AND \"区域\" = '华东' GROUP BY \"区域\""
            ),
        )

        assert "华东" in translated.physical_query.parameters.values()

    def test_ordinary_filters_including_or_branches_still_translate(
        self, sales_release
    ) -> None:
        """OR 分支投影不出结构化过滤项，但它不是矛盾，必须照常执行。"""

        translated = S2SqlSemanticTranslator().translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域" FROM "销售经营" '
                "WHERE \"区域\" = '华东' OR \"区域\" = '华南'"
            ),
        )

        assert translated.audit_complete is False
        assert "华东" in translated.physical_query.parameters.values()


class TestNamesDefinedInsideTheQuery:
    """CTE / 子查询自己起的名字，逐层都要认得出来。

    根因只有一个：一个 SELECT 的输出名不只是显式 ``AS``，还包括裸列透传和 ``*`` 带出来
    的名字。少认一种，外层再引用时就会被当成受治理成员送去符号表，后果有两种——
    查不到是"不认识这个名字"的假拒绝（实测「每个门店卖得最好的商品」这类**每组取前 N**
    必然两层、中间层通常写 ``*``）；查得到更糟：它会被重写成物理列并丢掉 ``p.`` 限定符，
    执行期才以 AmbiguousColumn 报错，那时已越过能救回这次查询的 ALL 重试。
    """

    @staticmethod
    def _translate(release, sql: str):
        return S2SqlSemanticTranslator().translate(
            release=release, dataset_id="sales_dataset", corrected_s2sql=sql
        )

    def test_a_star_in_the_middle_layer_still_carries_the_alias(self, sales_release) -> None:
        translated = self._translate(
            sales_release,
            'WITH agg AS (SELECT "区域", SUM("净收入") AS x FROM "销售经营" GROUP BY "区域"),'
            ' ranked AS (SELECT * FROM agg)'
            ' SELECT "区域", x FROM ranked',
        )

        assert translated.physical_query.sql

    def test_a_bare_pass_through_still_carries_the_alias(self, sales_release) -> None:
        translated = self._translate(
            sales_release,
            'WITH agg AS (SELECT "区域", SUM("净收入") AS x FROM "销售经营" GROUP BY "区域"),'
            ' ranked AS (SELECT "区域", x FROM agg)'
            ' SELECT "区域", x FROM ranked',
        )

        assert translated.physical_query.sql

    def test_top_n_per_group_translates(self, sales_release) -> None:
        """实机原样的形态：先聚合、再排名、取第一。"""

        translated = self._translate(
            sales_release,
            'WITH agg AS ('
            ' SELECT "区域", "渠道", SUM("净收入") AS _总额_ FROM "销售经营"'
            ' GROUP BY "区域", "渠道"'
            '), ranked AS ('
            ' SELECT *, RANK() OVER (PARTITION BY "区域" ORDER BY _总额_ DESC) AS rn FROM agg'
            ') SELECT "区域", "渠道", _总额_ FROM ranked WHERE rn = 1',
        )

        assert translated.physical_query.sql

    def test_an_inner_select_keeps_the_name_it_exports(self, sales_release) -> None:
        """内层裸投影一个受治理成员时，改名不能改掉它对外导出的名字。

        CTE 里的 ``SELECT "区域"`` 一旦编译成 ``SELECT "__kf_field_0"``，外层写
        ``p."区域"`` 就是 UndefinedColumn——翻译一路放行，执行期才炸。
        """

        translated = self._translate(
            sales_release,
            'WITH agg AS (SELECT "区域", SUM("净收入") AS x FROM "销售经营" GROUP BY "区域")'
            ' SELECT p."区域", p.x, q.x FROM agg p JOIN agg q ON p."区域" = q."区域"',
        )

        # CTE 必须真的把「区域」这个名字导出去，外层的 p."区域" 才有东西可指。
        physical = parse_one(translated.physical_query.sql, read="postgres")
        agg = next(
            item for item in physical.find_all(exp.CTE) if item.alias_or_name == "agg"
        )
        exported = {
            projection.alias_or_name for projection in agg.this.expressions
        }
        assert "区域" in exported, translated.physical_query.sql

    def test_a_pass_through_still_counts_as_the_governed_member(self, sales_release) -> None:
        """透传出去的名字承载的仍是受治理维度的语义，审计投影不能漏掉它。

        漏了的话回答卡少一个维度 chip，而查询确实是按它分的组。
        """

        translated = self._translate(
            sales_release,
            'WITH 明细 AS (SELECT "区域", "净收入" FROM "销售经营")'
            ' SELECT "区域", SUM("净收入") FROM 明细 GROUP BY "区域"',
        )

        assert translated.dimension_ids == ("region",)

    def test_a_local_name_does_not_borrow_a_governed_member(self, sales_release) -> None:
        """CTE 把别的东西起名叫某个受治理成员名时，审计说的是真正算的那个。"""

        translated = self._translate(
            sales_release,
            'WITH agg AS ('
            ' SELECT "区域", SUM("净收入") AS "订单数" FROM "销售经营" GROUP BY "区域"'
            ') SELECT "区域", "订单数" FROM agg',
        )

        assert translated.metric_ids == ("net_revenue",), translated.metric_ids

    @pytest.mark.parametrize(
        "sql",
        [
            pytest.param(
                'WITH agg AS (SELECT "查无此列" FROM "销售经营") SELECT "查无此列" FROM agg',
                id="最内层就写错了名字",
            ),
            pytest.param(
                'WITH agg AS (SELECT "区域", SUM("净收入") AS x FROM "销售经营" GROUP BY "区域")'
                ' SELECT "区域", xx FROM agg',
                id="外层把别名拼错",
            ),
            pytest.param(
                'WITH agg AS (SELECT "区域", SUM("净收入") AS x FROM "销售经营" GROUP BY "区域")'
                ' SELECT "区域", "查无此列" FROM agg',
                id="外层引用CTE没导出的名字",
            ),
        ],
    )
    def test_a_wrong_name_is_still_refused(self, sales_release, sql: str) -> None:
        """认得出本地名，不等于放宽校验。真写错的名字必须照样拒。"""

        with pytest.raises(SemanticParsingError):
            self._translate(sales_release, sql)

    def test_the_dataset_may_not_be_joined_to_itself(self, sales_release) -> None:
        """同一个 SELECT 里引用两次受治理表。

        两边都编译成同一个 __kf_dataset，而成员列改写成物理列时带不走 ``a.`` / ``b.``，
        PostgreSQL 到执行期才以 AmbiguousColumn 报错。在翻译期拒掉，重试才有机会。
        """

        with pytest.raises(SemanticParsingError) as excinfo:
            self._translate(
                sales_release,
                'SELECT a."区域" FROM "销售经营" a JOIN "销售经营" b ON a."区域" = b."区域"',
            )

        assert excinfo.value.code == "S2SQL_DATASET_SELF_JOIN_UNSUPPORTED"

    def test_crossing_a_cte_boundary_twice_is_fine(self, sales_release) -> None:
        """跨 CTE 边界各引用一次是两个独立的 SELECT，实测可执行，不该被上面那条误伤。"""

        translated = self._translate(
            sales_release,
            'WITH agg AS (SELECT "区域" FROM "销售经营" GROUP BY "区域")'
            ' SELECT s."渠道" FROM "销售经营" s JOIN agg ON s."区域" = agg."区域"',
        )

        assert translated.physical_query.sql
