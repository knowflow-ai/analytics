from __future__ import annotations

from knowflow_analytics.query.parser import _dimension_payload


def test_time_dimensions_reach_the_model_even_without_partition_time(
    sales_release,
) -> None:
    """普通「时间」角色的维度也必须让模型看见。

    此前只有 dimension_type == "partition_time" 的字段才会被当成时间维度。
    「订单日期」这类普通时间维度因此完全没暴露，模型在 rationale 里写下
    "no date dimension is exposed"，然后把「截止到 8 月 2 日」整个丢掉，
    返回了全部历史数据 —— 一个看起来正常的错误数字。
    """

    dataset = sales_release.datasets[0]
    payload = _dimension_payload(sales_release, dataset)
    time_names = [item["name"] for item in payload if item.get("semantic_type") == "time"]
    assert time_names, "至少一个时间维度必须出现在送给模型的 schema 里"


def test_every_scope_dimension_reaches_the_model_regardless_of_mapping(sales_release) -> None:
    """Scope 的每个维度都送给模型，不按 Mapper 命中裁剪。

    此前按命中裁剪成"最小 schema"，时间维度天然匹配不上——用户说「截止到
    8 月 1 日」不会提到「订单日期」这个词——于是被过滤掉，模型如实报告
    "没有时间维度"并丢掉时间条件，返回全部历史数据，一个看起来正常的错误
    数字。同一个坑还坑掉过事实根的实体名称维度（「各图书馆的藏品数量」丢掉
    GROUP BY 返回总数）。裁剪一旦回归，这条会红。
    """

    dataset = sales_release.datasets[0]
    payload = _dimension_payload(sales_release, dataset)

    exposed = {item["name"] for item in payload}
    expected = {item.name for item in sales_release.dimensions if item.id in dataset.dimension_ids}
    assert exposed == expected
    assert any(
        item.semantic_type == "time"
        for item in sales_release.dimensions
        if item.id in dataset.dimension_ids
    ), "fixture 必须含至少一个时间维度"
