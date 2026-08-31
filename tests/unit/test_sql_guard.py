from __future__ import annotations

import pytest

from knowflow_analytics.contracts import PhysicalQuery
from knowflow_analytics.errors import QueryGuardError
from knowflow_analytics.execution import PhysicalSqlGuard


def _query(sql: str) -> PhysicalQuery:
    return PhysicalQuery(
        release_id="release_sales_v1",
        dataset_id="sales_dataset",
        sql=sql,
        parameters={},
        columns=(),
    )


def test_accepts_single_select_with_known_table_and_limit(sales_release):
    PhysicalSqlGuard().validate(
        query=_query('SELECT "id" FROM "analytics_v0"."orders" LIMIT 10'),
        release=sales_release,
    )


def test_accepts_governed_physical_tables_referenced_through_a_cte(sales_release):
    PhysicalSqlGuard().validate(
        query=_query(
            'WITH "semantic_rows" AS ('
            'SELECT "id" FROM "analytics_v0"."orders"'
            ') SELECT "id" FROM "semantic_rows" LIMIT 10'
        ),
        release=sales_release,
    )


def test_accepts_nested_cte_aliases_when_leaf_tables_are_governed(sales_release):
    PhysicalSqlGuard().validate(
        query=_query(
            'SELECT "id" FROM ('
            'WITH "nested_rows" AS ('
            'SELECT "id" FROM "analytics_v0"."orders"'
            ') SELECT "id" FROM "nested_rows"'
            ') AS "scoped" LIMIT 10'
        ),
        release=sales_release,
    )


def test_accepts_only_tables_declared_by_a_dataset_sql_model(sales_release):
    sql_model = next(item for item in sales_release.models if item.id == "orders").model_copy(
        update={
            "query_type": "sql_query",
            "table": None,
            "schema_name": None,
            "sql_query": "SELECT * FROM analytics_v0.orders",
        }
    )
    release = sales_release.model_copy(
        update={
            "models": tuple(
                sql_model if item.id == sql_model.id else item for item in sales_release.models
            )
        }
    )

    PhysicalSqlGuard().validate(
        query=_query('SELECT "m0"."id" FROM (SELECT * FROM analytics_v0.orders) AS "m0" LIMIT 10'),
        release=release,
    )

    with pytest.raises(QueryGuardError):
        PhysicalSqlGuard().validate(
            query=_query(
                'SELECT "m0"."id" FROM (SELECT * FROM analytics_v0.payments) AS "m0" LIMIT 10'
            ),
            release=release,
        )


@pytest.mark.parametrize(
    "sql",
    [
        'DELETE FROM "analytics_v0"."orders"',
        'SELECT "id" FROM "analytics_v0"."orders"',
        'SELECT "id" FROM "public"."unknown_table" LIMIT 10',
        'SELECT "id" FROM "analytics_v0"."orders" LIMIT 10; SELECT 1',
        'SELECT "id" FROM "analytics_v0"."orders" -- hidden\n LIMIT 10',
    ],
)
def test_rejects_unsafe_physical_sql(sales_release, sql):
    with pytest.raises(QueryGuardError):
        PhysicalSqlGuard().validate(query=_query(sql), release=sales_release)
