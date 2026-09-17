"""扇出算对不算错：对着真库核数字。

形状对不等于数对。这个文件在真实 PostgreSQL 上跑翻译出来的 SQL，把它与手写的正确
答案、以及直连会得到的错误答案三方对照——错的那个也一起断言，因为它就是此前禁掉
这条路径的理由，也是算错时会悄悄回到的地方。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator
from tests.support import create_sales_fixture

_BY_PRODUCT = 'SELECT "商品", SUM("净收入") FROM "销售经营" GROUP BY "商品"'


def _release_with_primary(sales_release):
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == "orders.id" else item
        for item in sales_release.fields
    )
    return sales_release.model_copy(update={"fields": fields})


@pytest.mark.postgres
def test_a_fanned_out_sum_is_not_multiplied_by_the_line_items(sales_release):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        # 同一张订单里出现两次 A：这才是扇出真正开始骗人的地方。
        connection.execute(
            text("INSERT INTO analytics_v0.order_items VALUES (4, 1, 'A')")
        )

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_primary(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql=_BY_PRODUCT,
    )

    with engine.connect() as connection:
        measured = {
            row[0]: float(row[1])
            for row in connection.execute(
                text(translated.physical_query.sql), translated.physical_query.parameters
            )
            if row[0] is not None
        }
        # 直连的写法——此前禁掉这条路径就是因为它：订单 1 被复制成三行（A、B、A）。
        naive = {
            row[0]: float(row[1])
            for row in connection.execute(
                text(
                    """
                    SELECT i.product, SUM(o.net_amount)
                    FROM analytics_v0.orders o
                    LEFT JOIN analytics_v0.order_items i ON o.id = i.order_id
                    WHERE i.product IS NOT NULL
                    GROUP BY i.product
                    """
                )
            )
        }
    engine.dispose()

    # 订单 1 = 100，订单 2 = 200。A 出现在这两张订单里，各算一次。
    assert measured == {"A": 300.0, "B": 100.0}
    assert naive["A"] == 400.0, "直连确实会多算一次——这正是要绕开的东西"


@pytest.mark.postgres
def test_a_filter_on_the_many_side_counts_each_fact_row_once(sales_release):
    """「买过 A 的订单总额」：过滤决定哪些订单入选，金额每张订单只算一次。"""

    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO analytics_v0.order_items VALUES (4, 1, 'A')"))

    translated = S2SqlSemanticTranslator().translate(
        release=_release_with_primary(sales_release),
        dataset_id="sales_dataset",
        corrected_s2sql="SELECT SUM(\"净收入\") FROM \"销售经营\" WHERE \"商品\" = 'A'",
    )

    with engine.connect() as connection:
        total = float(
            connection.execute(
                text(translated.physical_query.sql), translated.physical_query.parameters
            ).scalar_one()
        )
    engine.dispose()

    assert total == 300.0
