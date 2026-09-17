"""多阶段形态的回归护栏。

Cube 需要在建模期声明「分组层级」（multi_stage 的 group_by / reduce_by），因为它的查询
接口是封闭的结构化查询，用户写不了 SQL。我们的查询接口是文本 SQL，模型可以现写分区窗口，
所以不引入这个建模概念（2026-09-16 用户评审）。

代价是这些形态的可用性完全依赖翻译器不收紧。这个文件把它们钉住：任何一条被将来的治理
规则挡住，这里先红。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  # 先加载 query 包，避免循环导入
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'
_SHAPES = {
    "占全体比": f'SELECT "区域", SUM("净收入") / SUM(SUM("净收入")) OVER () AS _占比_'
    f' FROM {_T} GROUP BY "区域"',
    "占本组比": f'SELECT "区域", "渠道", SUM("净收入")'
    f' / SUM(SUM("净收入")) OVER (PARTITION BY "区域") AS _占比_'
    f' FROM {_T} GROUP BY "区域", "渠道"',
    "组内排名": f'SELECT "区域", "渠道",'
    f' RANK() OVER (PARTITION BY "区域" ORDER BY SUM("净收入") DESC) AS _名次_'
    f' FROM {_T} GROUP BY "区域", "渠道"',
    "与组内均值比": f'SELECT "区域", "渠道",'
    f' SUM("净收入") - AVG(SUM("净收入")) OVER (PARTITION BY "区域") AS _差额_'
    f' FROM {_T} GROUP BY "区域", "渠道"',
    "CTE 版占全体比": f'WITH a AS (SELECT "区域", SUM("净收入") AS _s FROM {_T} GROUP BY "区域"),'
    " t AS (SELECT SUM(_s) AS _all FROM a)"
    ' SELECT a."区域", a._s / t._all AS _占比_ FROM a, t',
    "标量子查询与整体比": f'SELECT "区域" FROM {_T}'
    f' WHERE "净收入" > (SELECT AVG("净收入") FROM {_T})',
    "集合运算": f'SELECT "区域" AS _值_ FROM {_T} UNION SELECT "渠道" FROM {_T}',
}


@pytest.mark.parametrize("name", sorted(_SHAPES))
def test_multi_stage_shape_translates(sales_release, name: str) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=sales_release,
        dataset_id="sales_dataset",
        corrected_s2sql=_SHAPES[name],
    )

    assert translated.physical_query.sql


_KEEPS_DRILLDOWN = {
    "普通聚合": f'SELECT "区域", SUM("净收入") FROM {_T} GROUP BY "区域"',
    "窗口占本组比": f'SELECT "区域", "渠道",'
    f' SUM("净收入") / SUM(SUM("净收入")) OVER (PARTITION BY "区域") AS _占比_'
    f' FROM {_T} GROUP BY "区域", "渠道"',
    "窗口排名": f'SELECT "区域", RANK() OVER (ORDER BY SUM("净收入") DESC) AS _名次_'
    f' FROM {_T} GROUP BY "区域"',
}

_LOSES_DRILLDOWN = {
    "CTE 条件占比": f'WITH a AS (SELECT "区域", SUM("净收入") AS _s FROM {_T} GROUP BY "区域")'
    " SELECT COUNT(CASE WHEN _s > 2000 THEN 1 END) AS _户数_ FROM a",
    "标量子查询": f'SELECT "区域" FROM {_T} WHERE "净收入" > (SELECT AVG("净收入") FROM {_T})',
    "集合运算": f'SELECT "区域" AS _值_ FROM {_T} UNION SELECT "渠道" FROM {_T}',
}


@pytest.mark.parametrize("name", sorted(_KEEPS_DRILLDOWN))
def test_window_shapes_keep_their_drilldown_options(name: str) -> None:
    """窗口形态仍是「单条 SELECT 直接 FROM 数据集」，五种确定性编辑照样成立。

    这条性质决定了教学时优先推窗口而不是 CTE：同样答得出来，后续还能继续拆。
    """

    from knowflow_analytics.query.s2sql_edit import editable_select

    assert editable_select(_KEEPS_DRILLDOWN[name]) is not None


@pytest.mark.parametrize("name", sorted(_LOSES_DRILLDOWN))
def test_multi_layer_shapes_give_up_drilldown_rather_than_edit_the_wrong_layer(
    name: str,
) -> None:
    """过滤与分组都在 CTE 内部，在外层做编辑会让 chip 上写的和实际执行的不一致。

    那正是静默错答。这类问题的自然追问要重写整条 CTE，没有机械编辑能做到，
    所以不给选项，让用户重新问一句。
    """

    from knowflow_analytics.query.s2sql_edit import editable_select

    assert editable_select(_LOSES_DRILLDOWN[name]) is None
