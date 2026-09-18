"""数值阈值必须是数字——这条此前只有 PostgreSQL 在守，而那时已经太晚。

提示词第 46 条写着「数值条件只保留问题中原始数字本身，不得把万、亿、元、%、个等单位
或量词写进 value，也不得自行缩放」。它把两件事捆成了一句，一件对、一件正好写反：

- 「不得把单位或量词写进 value」**对**，但零拦截。``"净收入" > '2万'`` 翻译一路放行，
  以参数 ``{'p0': '2万'}`` 送到 PostgreSQL 才炸（invalid input syntax for type numeric），
  那时已在 EXECUTING——越过了能救回这次查询的重试链。与 2026-08-30 那个
  ``SUM(SUM(...))`` 是同一类：六道治理关全绿，只有数据库说不。
- 「不得自行缩放」**写反了**。用户问「净收入超过 2 万的区域」，模型照规则写 ``> 2``，
  而列里存的是元——SQL 合法、执行成功、返回几乎全部行，没有任何异常信号。规则本身
  保证了这个错误。

数量级词（万/亿/千）是数字的一部分，把「2 万」写成 20000 是算术，不是业务定义，属于
「可以推导计算」；单位（元/万元）是业务定义，只能按指标声明的 unit 换算，没声明就不换算。
提示词那句照此改写，而「必须是数字」这一半收成编译器检查——判据不变：错了能被确定性
发现的才允许模型做。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  # 先加载 query 包，避免循环导入
from knowflow_analytics.contracts import DimensionSpec
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'


def _translate(release, s2sql: str):
    return S2SqlSemanticTranslator().translate(
        release=release, dataset_id="sales_dataset", corrected_s2sql=s2sql
    )


@pytest.mark.parametrize(
    "s2sql",
    [
        # 量词抄进了字面量。
        f'SELECT "区域" FROM {_T} WHERE "净收入" > \'2万\'',
        # 单位抄进了字面量。
        f'SELECT "区域" FROM {_T} WHERE "净收入" >= \'20000元\'',
        # 聚合侧同理：HAVING 上的阈值。
        f'SELECT "区域", SUM("净收入") FROM {_T} GROUP BY "区域" HAVING SUM("净收入") > \'2万\'',
        # COUNT 的结果也是数值，与被数的列本身是不是数值无关。
        f'SELECT "区域" FROM {_T} GROUP BY "区域" HAVING COUNT("渠道") > \'1千\'',
        # 字面量写在左边。
        f'SELECT "区域" FROM {_T} WHERE \'2万\' < "净收入"',
        # IN 列表里任何一个都算。
        f'SELECT "区域" FROM {_T} WHERE "净收入" IN (20000, \'3万\')',
        # BETWEEN 的任一端。
        f'SELECT "区域" FROM {_T} WHERE "净收入" BETWEEN \'1万\' AND 30000',
    ],
)
def test_a_unit_or_quantifier_in_the_threshold_is_refused_at_translate_time(
    sales_release, s2sql: str
) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(sales_release, s2sql)

    assert raised.value.code == "S2SQL_NON_NUMERIC_THRESHOLD"


def test_the_refusal_names_the_metric_and_the_literal(sales_release) -> None:
    """重试链拿到的必须是模型改得动的话：哪个成员、哪个字面量。"""

    with pytest.raises(SemanticParsingError) as raised:
        _translate(sales_release, f'SELECT "区域" FROM {_T} WHERE "净收入" > \'2万\'')

    message = str(raised.value)
    assert "净收入" in message
    assert "2万" in message


@pytest.mark.parametrize(
    "s2sql",
    [
        # 裸数字，本来就对。
        f'SELECT "区域" FROM {_T} WHERE "净收入" > 20000',
        # 纯数字的字符串：PostgreSQL 照样能转，不是错误，不拦。
        f'SELECT "区域" FROM {_T} WHERE "净收入" > \'20000\'',
        # 小数与负数同理。
        f'SELECT "区域" FROM {_T} WHERE "净收入" > \'-1.5\'',
        # 文本维度比文本，天经地义。
        f"SELECT \"区域\" FROM {_T} WHERE \"区域\" = '华东'",
        # 时间维度比日期字面量。
        f"SELECT \"区域\" FROM {_T} WHERE \"下单日期\" >= '2024-01-01'",
    ],
)
def test_legitimate_comparisons_are_untouched(sales_release, s2sql: str) -> None:
    assert _translate(sales_release, s2sql).physical_query.sql


def test_a_numeric_dimension_is_governed_the_same_way(sales_release) -> None:
    """数值维度（年份、编号）与指标同一待遇：依据是声明的 data_type，不是猜的。"""

    release = sales_release.model_copy(
        update={
            "dimensions": (
                *sales_release.dimensions,
                DimensionSpec(
                    id="net_amount_dim",
                    name="净收入金额",
                    model_id="orders",
                    field_id="orders.net_amount",
                ),
            ),
            "datasets": (
                sales_release.datasets[0].model_copy(
                    update={
                        "dimension_ids": (
                            *sales_release.datasets[0].dimension_ids,
                            "net_amount_dim",
                        )
                    }
                ),
            ),
        }
    )

    with pytest.raises(SemanticParsingError) as raised:
        S2SqlSemanticTranslator().translate(
            release=release,
            dataset_id="sales_dataset",
            corrected_s2sql=f'SELECT "区域" FROM {_T} WHERE "净收入金额" > \'2万\'',
        )

    assert raised.value.code == "S2SQL_NON_NUMERIC_THRESHOLD"
