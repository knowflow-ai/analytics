from knowflow_analytics.contracts import FilterOperator, FixedFilter
from knowflow_analytics.modeling.source_query import compile_governed_model_source


def test_table_and_sql_models_share_the_same_fixed_filter_scope(sales_release):
    table_model = next(item for item in sales_release.models if item.id == "orders")
    fixed_filter = FixedFilter(
        field_id="orders.region",
        operator=FilterOperator.EQ,
        value="华东",
    )
    table_model = table_model.model_copy(update={"filters": (fixed_filter,)})
    sql_model = table_model.model_copy(
        update={
            "query_type": "sql_query",
            "table": None,
            "schema_name": None,
            "sql_query": 'SELECT * FROM "analytics_v0"."orders"',
        }
    )

    table_sql, table_parameters = compile_governed_model_source(
        table_model,
        sales_release,
    )
    sql_sql, sql_parameters = compile_governed_model_source(
        sql_model,
        sales_release,
    )

    assert table_sql == ('SELECT * FROM "analytics_v0"."orders" WHERE "region" = :model_filter_0')
    assert "AS governed_sql_model WHERE" in sql_sql
    assert table_parameters == sql_parameters == {"model_filter_0": "华东"}
