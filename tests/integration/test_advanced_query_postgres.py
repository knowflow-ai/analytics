from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.contracts import QueryRuleMode, QueryRuleSpec, QueryRuleType
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.query.rules import QueryRuleEngine
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator
from tests.support import create_sales_fixture


@pytest.mark.postgres
def test_advanced_s2sql_executes_against_one_postgres_snapshot(sales_release) -> None:
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO analytics_v0.orders VALUES
              (4, 1, '华东', '直营', 200, 0, DATE '2025-08-01')
            """
        )
    engine.dispose()
    executor = SqlExecutor(database_url)
    translator = S2SqlSemanticTranslator()
    try:
        year_over_year = translator.translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
                'RATIO_OVER("净收入") AS "同比" FROM "销售经营" '
                "GROUP BY DATE_TRUNC('month', \"下单日期\")"
            ),
        )
        ratio_result = executor.execute(
            query=year_over_year.physical_query,
            release=sales_release,
        )
        current = next(row for row in ratio_result.rows if str(row[0]).startswith("2026-08"))
        assert Decimal(str(current[1])).quantize(Decimal("0.0001")) == Decimal("0.9000")

        share = translator.translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域", RATIO_TO_TOTAL("净收入") AS "占比" '
                'FROM "销售经营" WHERE "下单日期" >= DATE \'2026-01-01\' '
                'GROUP BY "区域"'
            ),
        )
        share_result = executor.execute(query=share.physical_query, release=sales_release)
        shares = {row[0]: Decimal(str(row[1])) for row in share_result.rows}
        assert shares["华东"].quantize(Decimal("0.0001")) == Decimal("0.7895")
        assert shares["华南"].quantize(Decimal("0.0001")) == Decimal("0.2105")

        subset_share = translator.translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT RATIO_TO_TOTAL("净收入", "区域", \'华东\') AS "华东占比" FROM "销售经营"'
            ),
        )
        subset_result = executor.execute(
            query=subset_share.physical_query,
            release=sales_release,
        )
        assert Decimal(str(subset_result.rows[0][0])).quantize(Decimal("0.0001")) == Decimal(
            "0.8621"
        )

        set_query = translator.translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'SELECT "区域" FROM "销售经营" WHERE "渠道" = \'直营\' '
                'UNION SELECT "区域" FROM "销售经营" WHERE "渠道" = \'电商\''
            ),
        )
        set_result = executor.execute(query=set_query.physical_query, release=sales_release)
        assert {row[0] for row in set_result.rows} == {"华东", "华南"}

        ranking = translator.translate(
            release=sales_release,
            dataset_id="sales_dataset",
            corrected_s2sql=(
                'WITH "_target_" AS ('
                'SELECT SUM("净收入") AS "_amount_" FROM "销售经营" '
                "WHERE \"区域\" = '华南'"
                ") "
                'SELECT COUNT(*) + 1 AS "_rank_" FROM ('
                'SELECT SUM("净收入") AS "_amount_" FROM "销售经营" GROUP BY "区域" '
                'HAVING SUM("净收入") > (SELECT "_amount_" FROM "_target_")'
                ') AS "_larger_"'
            ),
        )
        ranking_result = executor.execute(
            query=ranking.physical_query,
            release=sales_release,
        )
        assert ranking_result.rows == ((2,),)

        ranking_with_cte_alias = translator.translate(
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
        cte_ranking_result = executor.execute(
            query=ranking_with_cte_alias.physical_query,
            release=sales_release,
        )
        assert cte_ranking_result.rows == ((2,),)

        dataset = sales_release.datasets[0].model_copy(
            update={"default_time_dimension_id": "order_date"}
        )
        rule = QueryRuleSpec(
            id="recent-thirty-days",
            dataset_id="sales_dataset",
            priority=3,
            rule_type=QueryRuleType.ADD_DATE,
            mode=QueryRuleMode.RECENT,
            parameters=(30,),
        )
        ruled_release = sales_release.model_copy(
            update={"datasets": (dataset,), "query_rules": (rule,)}
        )
        ruled = QueryRuleEngine().apply(
            release=ruled_release,
            dataset_id="sales_dataset",
            corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
            now=datetime(2026, 8, 21, tzinfo=UTC),
        )
        ruled_query = translator.translate(
            release=ruled_release,
            dataset_id="sales_dataset",
            corrected_s2sql=ruled.corrected_s2sql,
        )
        ruled_result = executor.execute(query=ruled_query.physical_query, release=ruled_release)
        assert Decimal(str(ruled_result.rows[0][0])) == Decimal("380")
    finally:
        executor.close()
