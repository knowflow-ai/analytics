import pytest

from knowflow_analytics.contracts import SemanticQuery
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.sql_model import compile_sql_model_source, validate_sql_model
from knowflow_analytics.semantic.translator import SemanticTranslator


def test_sql_model_renders_typed_defaults_and_keeps_one_read_only_query():
    sql = validate_sql_model(
        "SELECT id, amount FROM analytics.orders WHERE region = $region$ AND amount >= $minimum$",
        (
            {"name": "region", "valueType": "STRING", "defaultValues": ["north"]},
            {"name": "minimum", "valueType": "NUMBER", "defaultValues": [10]},
        ),
    )

    assert "'north'" in sql
    assert "10" in sql
    assert "$region$" not in sql
    assert compile_sql_model_source(sql, ()) == f"({sql})"


def test_sql_model_accepts_the_existing_mustache_variable_contract():
    rendered = validate_sql_model(
        "SELECT id FROM sales.orders WHERE region = {{region}}",
        (
            {
                "name": "region",
                "valueType": "STRING",
                "defaultValues": ["华东"],
            },
        ),
    )

    assert rendered == "SELECT id FROM sales.orders WHERE region = '华东'"


@pytest.mark.parametrize(
    "sql, variables",
    [
        ("SELECT 1; DELETE FROM orders", ()),
        ("UPDATE orders SET amount = 0 RETURNING *", ()),
        (
            "SELECT * FROM orders_$suffix$",
            (
                {
                    "name": "suffix",
                    "valueType": "EXPR",
                    "defaultValues": ["daily; DROP TABLE orders"],
                },
            ),
        ),
        ("SELECT * FROM orders WHERE id = $missing$", ()),
    ],
)
def test_sql_model_rejects_write_multi_statement_and_unsafe_variables(sql, variables):
    with pytest.raises(SemanticValidationError):
        validate_sql_model(sql, variables)


def test_translator_queries_sql_model_through_governed_subquery(sales_release):
    release = sales_release.model_copy(
        update={
            "models": tuple(
                item.model_copy(
                    update={
                        "query_type": "sql_query",
                        "table": None,
                        "schema_name": None,
                        "sql_query": "SELECT * FROM analytics_v0.orders",
                    }
                )
                if item.id == "orders"
                else item
                for item in sales_release.models
            )
        }
    )

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(dataset_id="sales_dataset", metric_ids=("net_revenue",)),
    )

    assert "FROM (SELECT * FROM analytics_v0.orders) AS" in physical.sql
