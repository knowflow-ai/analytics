"""被一对多放大的事实，按主标识塌回去再聚合。

「各商品的净收入」要把订单连到订单明细，每张订单于是被复制成它的明细条数，
``SUM(净收入)`` 跟着翻倍。此前这条路径整个被禁掉（``FANOUT_RISK``），跨粒度的问题
一律答不了。

Cube 的做法是算对而不是禁止：先 ``SELECT DISTINCT 分组维度 + 事实表主键`` 把重复行
塌回去，再按主键接回事实表聚合。我们照搬这一步，并且补上 Cube 拿不到的那道前提——
主标识是不是真的唯一，发布前的质量报告用真实数据核对过（Cube 从不验证建模者的断言，
它唯一的数据探针 cubeCardinalityQueries 是零调用的死代码）。

我们比 Cube 少一层麻烦：作用域里的指标必然属于事实根（ANALYSIS_TOPIC_METRIC_OUTSIDE_ROOT），
所以被放大的永远只有事实根一个，不需要 Cube 那套「每个被放大的 cube 一个子查询再按
维度缝合」。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'
_BY_PRODUCT = f'SELECT "商品", SUM("净收入") FROM {_T} GROUP BY "商品"'


def _with_primary_identifier(sales_release, *, on: str = "orders.id"):
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == on else item
        for item in sales_release.fields
    )
    return sales_release.model_copy(update={"fields": fields})


def _translate(release, s2sql: str = _BY_PRODUCT):
    return S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=s2sql,
    )


def test_a_fanned_out_sum_collapses_on_the_primary_identifier(sales_release) -> None:
    sql = _translate(_with_primary_identifier(sales_release)).physical_query.sql

    # 三步：放大的连接照跑 → DISTINCT(分组维度 + 主键) 塌回去 → 按主键接回事实表。
    assert "SELECT DISTINCT" in sql
    assert "__kf_grain_0" in sql
    # 明细表只出现在 keys 子查询里；外层再连回去的是事实表本身。
    assert sql.count('"analytics_v0"."order_items"') == 1


def test_without_a_primary_identifier_it_refuses_instead_of_doubling(sales_release) -> None:
    """Cube 的条件性要求：参与一对多连接且带可加度量时，必须有主键。"""

    with pytest.raises(AnalyticsError) as raised:
        _translate(sales_release)

    assert raised.value.code == "FANOUT_RISK"
    assert "主标识" in str(raised.value)


def test_a_distinct_count_still_takes_the_plain_join(sales_release) -> None:
    """COUNT DISTINCT 本来就不受重复行影响，没必要多绕一层子查询。"""

    sql = _translate(
        _with_primary_identifier(sales_release),
        f'SELECT "商品", COUNT(DISTINCT "订单数") FROM {_T} GROUP BY "商品"',
    ).physical_query.sql

    assert "SELECT DISTINCT" not in sql


def test_an_unrelated_query_is_not_reshaped(sales_release) -> None:
    """没有放大就没有这层子查询——绝大多数查询的 SQL 一个字都不该变。"""

    sql = _translate(
        _with_primary_identifier(sales_release),
        f'SELECT "区域", SUM("净收入") FROM {_T} GROUP BY "区域"',
    ).physical_query.sql

    assert "SELECT DISTINCT" not in sql
    assert "__kf_grain" not in sql


def test_a_filter_on_the_multiplied_side_is_applied_before_collapsing(sales_release) -> None:
    """「买过咖啡的订单总额」：过滤决定哪些订单入选，塌回去之后每张订单只算一次。"""

    sql = _translate(
        _with_primary_identifier(sales_release),
        f"SELECT SUM(\"净收入\") FROM {_T} WHERE \"商品\" = '咖啡'",
    ).physical_query.sql

    assert "SELECT DISTINCT" in sql
