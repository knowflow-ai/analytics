"""「某个值占整体多少」的分母不能被同一个维度过滤掉。

实机（2026-09-18 A/B 实验里撞出来的）：「太古里店销售额占用多少？」

    SELECT RATIO_TO_TOTAL("销售金额", "门店名称", '太古里店') AS "_占比_"
    FROM "销售明细分析" WHERE "门店名称" = '太古里店'

答 **1.0**。正确答案 0.158。展开后分子是 ``agg FILTER (WHERE 门店 = '太古里店')``、
分母是同一个 ``agg``——外层 WHERE 把分母也筛成了这一家店，于是恒等于 100%。SQL 合法、
执行成功、数字是个漂亮的百分比，六道治理关全绿。

提示词第 20 条写着「该维度值只过滤分子，禁止再放入 WHERE」——又是一条没有治理关守着的
散文。按同一条判据：**这个错能被确定性发现**（分子分母的行集合由 SQL 本身决定），
所以它该是编译器检查，不是提示词里的一句话。

范围只收「恒等于 1」这一种：同一维度被限定到**恰好这一个值**。
``WHERE "区域" IN ('华东','华南')`` 是真的在缩小对比范围（在这两个区域之间看占比），
不拦；过滤别的维度更不拦。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  # 先加载 query 包，避免循环导入
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'


def _translate(release, s2sql: str):
    return S2SqlSemanticTranslator().translate(
        release=release, dataset_id="sales_dataset", corrected_s2sql=s2sql
    )


@pytest.mark.parametrize(
    "where",
    [
        "WHERE \"区域\" = '华东'",
        "WHERE \"区域\" IN ('华东')",
        "WHERE \"渠道\" = '线上' AND \"区域\" = '华东'",
    ],
)
def test_filtering_the_subset_dimension_to_that_one_value_is_refused(sales_release, where) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(
            sales_release,
            f'SELECT RATIO_TO_TOTAL("净收入", "区域", \'华东\') AS "_占比_" FROM {_T} {where}',
        )

    assert raised.value.code == "S2SQL_RATIO_SCOPE_FILTERED"


def test_the_refusal_names_the_dimension_and_the_value(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(
            sales_release,
            f'SELECT RATIO_TO_TOTAL("净收入", "区域", \'华东\') AS "_占比_" '
            f"FROM {_T} WHERE \"区域\" = '华东'",
        )

    message = str(raised.value)
    assert "区域" in message
    assert "华东" in message


@pytest.mark.parametrize(
    "where",
    [
        # 另一个维度：合法的范围限定。
        "WHERE \"渠道\" = '线上'",
        # 同一维度但留了多个值：在这几个之间比占比，是真需求。
        "WHERE \"区域\" IN ('华东', '华南')",
        # 同一维度、不同的值：分子会是空集，但那是问句自己的事，不是恒等式。
        "WHERE \"区域\" = '华南'",
        # 没有过滤。
        "",
    ],
)
def test_a_real_scope_restriction_still_works(sales_release, where: str) -> None:
    translated = _translate(
        sales_release,
        f'SELECT RATIO_TO_TOTAL("净收入", "区域", \'华东\') AS "_占比_" FROM {_T} {where}',
    )

    assert translated.physical_query.sql


def test_the_group_share_form_is_untouched(sales_release) -> None:
    """单参数形态（各组占全体）没有第三个参数，这道关与它无关。"""

    translated = _translate(
        sales_release,
        f'SELECT "区域", RATIO_TO_TOTAL("净收入") AS "_占比_" FROM {_T} '
        f"WHERE \"渠道\" = '线上' GROUP BY \"区域\"",
    )

    assert translated.physical_query.sql
