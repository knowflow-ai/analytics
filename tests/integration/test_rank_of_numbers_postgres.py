"""RANK_OF 的名次对着真库核过：先过滤再排名给出的名次，与全量排名后取目标给出的不是一个数。

种子：华东 100+200=300，华南 80。华南按净收入在各区域中排第 2。
先 WHERE 到华南再 RANK()——子集里只剩一行——永远第 1。
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


def test_the_rank_is_taken_from_the_unfiltered_whole(sales_release) -> None:
    engine = _engine()
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=f'SELECT RANK_OF("区域", \'华南\', "净收入") AS "_排名_" FROM {_T}',
    )

    measured = int(_run(engine, translated)[0][0])

    with engine.connect() as connection:
        naive = int(
            connection.execute(
                text(
                    """
                    SELECT RANK() OVER (ORDER BY SUM(net_amount) DESC)
                    FROM analytics_v0.orders WHERE region = '华南' GROUP BY region
                    """
                )
            ).scalar_one()
        )

    assert measured == 2
    assert naive == 1


def test_the_rank_can_be_taken_within_each_region(sales_release) -> None:
    """华东：直营 100 对电商 200 → 直营第 2；华南只有直营 → 第 1。"""

    engine = _engine()
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            f'SELECT "区域", RANK_OF("渠道", \'直营\', "净收入") AS "_排名_" '
            f'FROM {_T} GROUP BY "区域"'
        ),
    )

    rows = {row[0]: int(row[1]) for row in _run(engine, translated)}

    assert rows == {"华东": 2, "华南": 1}
