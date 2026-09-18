"""ENTITY_SHARE 的数字对着真库核过：按实体聚合再比阈值，与按明细行判断给出的不是一个数。

种子：渠道「直营」两单 100 + 80 = 180，「电商」一单 200。阈值 150。
按实体算（对）：两个渠道都过线 → 1.0。
按明细行算（错，也是模型此前最常写的）：只有电商那一单 > 150 → 0.5。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator
from tests.support import create_sales_fixture

_T = '"销售经营"'


def _engine():
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    return create_engine(database_url)


def _run(engine, translated):
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(
                text(translated.physical_query.sql), translated.physical_query.parameters
            )
        ]


def test_the_share_is_counted_at_entity_grain_not_row_grain(sales_release) -> None:
    engine = _engine()
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=f'SELECT ENTITY_SHARE("渠道", "净收入" > 150) AS "_占比_" FROM {_T}',
    )

    measured = float(_run(engine, translated)[0][0])

    with engine.connect() as connection:
        correct = float(
            connection.execute(
                text(
                    """
                    WITH t AS (SELECT channel, SUM(net_amount) AS v
                               FROM analytics_v0.orders GROUP BY channel)
                    SELECT COUNT(CASE WHEN v > 150 THEN 1 END)::float / COUNT(channel) FROM t
                    """
                )
            ).scalar_one()
        )
        # 阈值打在明细行上——合法 SQL、正常数字、六道关全绿，只是错了。
        naive = float(
            connection.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT channel)::float
                         / (SELECT COUNT(DISTINCT channel) FROM analytics_v0.orders)
                    FROM analytics_v0.orders WHERE net_amount > 150
                    """
                )
            ).scalar_one()
        )

    assert measured == correct == 1.0
    assert naive == 0.5
    assert measured != naive


def test_the_share_can_be_broken_down_by_region(sales_release) -> None:
    """华东有直营(100)与电商(200)，华南只有直营(80)：按区域拆，各自在实体粒度上数。"""

    engine = _engine()
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            f'SELECT "区域", ENTITY_SHARE("渠道", "净收入" > 150) AS "_占比_" '
            f'FROM {_T} GROUP BY "区域"'
        ),
    )

    rows = {row[0]: float(row[1]) for row in _run(engine, translated)}

    assert rows == {"华东": 0.5, "华南": 0.0}
