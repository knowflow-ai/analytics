"""最终 LLM prompt 的指标/维度成员以带表头的竖线表渲染，而不是逐条 dict repr。"""

from __future__ import annotations

from knowflow_analytics.query.parser import (
    _DIMENSION_COLUMNS,
    _METRIC_COLUMNS,
    _render_member_table,
)


def test_empty_members_render_as_an_empty_list() -> None:
    assert _render_member_table([], _METRIC_COLUMNS) == "[]"


def test_columns_nobody_fills_are_dropped_and_name_always_leads() -> None:
    entries = [
        {
            "name": "销售金额",
            "description": "",
            "aliases": (),
            "aggregation": "sum",
            "unit": None,
            "format": None,
        },
        {
            "name": "门店数量",
            "description": "门店总数",
            "aliases": ("门店数",),
            "aggregation": "count",
            "unit": None,
        },
    ]

    rendered = _render_member_table(entries, _METRIC_COLUMNS)

    assert rendered.splitlines() == [
        "(name|aggregation|aliases|description)",
        "销售金额|sum||",
        "门店数量|count|门店数|门店总数",
    ]


def test_cells_collapse_whitespace_and_never_contain_the_separator() -> None:
    entries = [
        {
            "name": "销售日期",
            "description": "销售发生\n的日期 | 按天",
            "aliases": ("售卖日期", "下单|日期"),
            "semantic_type": "time",
            "time_granularity": "day",
            "data_type": "DATE",
            "date_format": "yyyy-MM-dd",
        }
    ]

    header, row = _render_member_table(entries, _DIMENSION_COLUMNS).splitlines()

    assert (
        header == "(name|semantic_type|time_granularity|data_type|date_format|aliases|description)"
    )
    assert row.count("|") == 6
    assert row == "销售日期|time|day|DATE|yyyy-MM-dd|售卖日期;下单｜日期|销售发生 的日期 ｜ 按天"
