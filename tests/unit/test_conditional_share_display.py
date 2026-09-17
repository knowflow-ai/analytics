"""现算的条件占比按百分比展示。

现场（2026-09-16）：「账户余额大于 2000 的账户占比」答出 0.2155，界面显示「0.21」。
百分比展示此前只认受治理函数 RATIO_TO_TOTAL；模型写的是两个计数相除，被归成普通计算列。
上游 SuperSonic 也认不出来：它的百分比是建模者在指标上声明的 dataFormatType，
现算列没有声明就按原始数字显示。

两个计数聚合相除，取值必然落在 0..1，这是表达式形状本身保证的，不用读问句也不用猜列名。
"""

from __future__ import annotations

import knowflow_analytics.query.parser  # noqa: F401  # 先加载 query 包，避免循环导入
from knowflow_analytics.contracts import SemanticQuery
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_GROUPED = (
    'WITH a AS (SELECT "区域", SUM("净收入") AS _bal FROM "销售经营" GROUP BY "区域") '
    "SELECT {projection} AS _占比_ FROM a"
)


def _columns(release, sql: str):
    return (
        S2SqlSemanticTranslator()
        .translate(release=release, dataset_id="sales_dataset", corrected_s2sql=sql)
        .physical_query.columns
    )


def test_two_counts_divided_is_a_share(sales_release) -> None:
    columns = _columns(
        sales_release,
        _GROUPED.format(projection='COUNT(CASE WHEN _bal > 2000 THEN 1 END) * 1.0 / COUNT("区域")'),
    )

    assert [(item.name, item.kind, item.ratio_form) for item in columns] == [
        ("_占比_", "ratio", "share")
    ]


def test_a_zero_one_case_summed_over_the_row_count_is_a_share(sales_release) -> None:
    columns = _columns(
        sales_release,
        'SELECT SUM(CASE WHEN "区域" = \'华东\' THEN 1 ELSE 0 END) * 1.0 / COUNT("区域")'
        ' AS _占比_ FROM "销售经营"',
    )

    assert [(item.kind, item.ratio_form) for item in columns] == [("ratio", "share")]


def test_a_per_entity_average_is_not_a_share(sales_release) -> None:
    """SUM(金额)/COUNT(订单) 是每单均值，不是占比：分子不是计数。"""

    columns = _columns(
        sales_release,
        'SELECT SUM("净收入") * 1.0 / COUNT("区域") AS _每单均值_ FROM "销售经营"',
    )

    assert [(item.kind, item.ratio_form) for item in columns] == [("calculation", None)]


def test_a_share_column_is_displayed_as_a_percentage(sales_release) -> None:
    columns = _columns(
        sales_release,
        'SELECT SUM(CASE WHEN "区域" = \'华东\' THEN 1 ELSE 0 END) * 1.0 / COUNT("区域")'
        ' AS _占比_ FROM "销售经营"',
    )

    visualization = AnalyticsQueryService._visualization(
        sales_release,
        # 语义投影里带着分组维度：占比是先按实体聚合再算的，那个维度在 WITH 里，
        # 不进输出。用它判断「有没有分组」会把单值占比判成柱图，单值卡就不走百分比了
        # （现场实测：界面仍显示 0.21）。
        SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
        ),
        'SELECT SUM(CASE WHEN "区域" = \'华东\' THEN 1 ELSE 0 END) * 1.0 / COUNT("区域")'
        ' AS _占比_ FROM "销售经营"',
        columns,
    )

    assert visualization["y_formats"] == ["percent"]
    assert visualization["type"] == "ratio"
