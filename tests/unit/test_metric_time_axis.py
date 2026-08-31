"""逻辑时间轴:指标声明了不同聚合时间轴时,按各自的轴对齐。

没有它,「本月收入和订单数」只能共用一根物理时间列。跨月支付很常见,实测
2000 笔半年数据首月订单数偏差 -123 笔、末月 +95 笔——SQL 合法、趋势图上看不出
异常。LLM 写 S2SQL 时只会写具体维度名,没有词汇表达「各按各的」,这根轴就是
那个词。MetricFlow 的 metric_time 与 Cube 的 multi-fact view 同样放在查询期。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    DimensionSpec,
    FieldSpec,
    MetricKind,
    MetricSpec,
    ModelSpec,
    SemanticQuery,
    SemanticRelease,
)
from knowflow_analytics.semantic.translator import SemanticTranslator


def _release(*, logical_axis: bool) -> SemanticRelease:
    dimensions = [
        DimensionSpec(
            id="d_od", name="下单日期", model_id="m_o", field_id="f_od", semantic_type="time"
        ),
        DimensionSpec(
            id="d_pd", name="支付日期", model_id="m_o", field_id="f_pd", semantic_type="time"
        ),
    ]
    dimension_ids = ["d_od", "d_pd"]
    if logical_axis:
        dimensions.append(
            DimensionSpec(
                id="d_mt",
                name="统计时间",
                model_id="m_o",
                field_id="f_od",
                semantic_type="time",
                metric_time_axis=True,
            )
        )
        dimension_ids.append("d_mt")
    return SemanticRelease(
        id="rel_axis",
        project_id="exp",
        spec_hash="sha256:axis",
        models=(ModelSpec(id="m_o", name="订单", schema_name="s", table="orders"),),
        fields=(
            FieldSpec(id="f_id", name="id", model_id="m_o", column="id", data_type="int"),
            FieldSpec(
                id="f_od", name="下单日期", model_id="m_o", column="order_date", data_type="date"
            ),
            FieldSpec(
                id="f_pd", name="支付日期", model_id="m_o", column="paid_date", data_type="date"
            ),
            FieldSpec(id="f_amt", name="金额", model_id="m_o", column="amount", data_type="number"),
        ),
        metrics=(
            MetricSpec(
                id="mt_rev",
                name="收入",
                model_id="m_o",
                kind=MetricKind.ATOMIC,
                field_id="f_amt",
                aggregation=Aggregation.SUM,
                agg_time_dimension_id="d_pd",
            ),
            MetricSpec(
                id="mt_cnt",
                name="订单数",
                model_id="m_o",
                kind=MetricKind.ATOMIC,
                field_id="f_id",
                aggregation=Aggregation.COUNT,
                agg_time_dimension_id="d_od",
            ),
        ),
        dimensions=tuple(dimensions),
        datasets=(
            DatasetSpec(
                id="ds_o",
                name="订单分析",
                model_ids=("m_o",),
                metric_ids=("mt_rev", "mt_cnt"),
                dimension_ids=tuple(dimension_ids),
            ),
        ),
        relations=(),
    )


def test_grouping_by_the_logical_axis_expands_each_metric_to_its_own_axis() -> None:
    physical = SemanticTranslator().translate(
        release=_release(logical_axis=True),
        query=SemanticQuery(
            dataset_id="ds_o", metric_ids=("mt_rev", "mt_cnt"), dimension_ids=("d_mt",)
        ),
    )

    assert "UNION ALL" in physical.sql
    assert "__kf_metric_time" in physical.sql
    # 每个指标只统计属于自己那根轴的行。
    assert physical.sql.count("CASE WHEN") == 2
    assert {"d_od", "d_pd"} <= set(physical.parameters.values())


def test_a_physical_time_dimension_keeps_the_single_table_scan() -> None:
    """没引用逻辑轴时完全不展开,现有行为零变化。"""

    physical = SemanticTranslator().translate(
        release=_release(logical_axis=True),
        query=SemanticQuery(
            dataset_id="ds_o", metric_ids=("mt_rev", "mt_cnt"), dimension_ids=("d_od",)
        ),
    )

    assert "UNION ALL" not in physical.sql
    assert "CASE WHEN" not in physical.sql


def test_a_release_without_the_logical_axis_is_unaffected() -> None:
    physical = SemanticTranslator().translate(
        release=_release(logical_axis=False),
        query=SemanticQuery(
            dataset_id="ds_o", metric_ids=("mt_rev", "mt_cnt"), dimension_ids=("d_od",)
        ),
    )

    assert "UNION ALL" not in physical.sql


def test_the_textual_path_expands_the_logical_axis_too() -> None:
    """客户实际走的是文本路径,只做结构化等于把修复装在没人走的门上。"""

    # 局部导入:``semantic.s2sql_translator`` 与 ``query`` 包之间有既有的循环导入,
    # 模块级先导它会炸。本文件不需要 query 模块,不为绕开而加无用导入。
    import knowflow_analytics.query  # noqa: F401
    from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

    translated = S2SqlSemanticTranslator().translate(
        release=_release(logical_axis=True),
        dataset_id="ds_o",
        corrected_s2sql=(
            'SELECT "统计时间", SUM("收入"), COUNT("订单数") '
            'FROM "订单分析" GROUP BY "统计时间"'
        ),
    )

    sql = translated.physical_query.sql
    assert "UNION ALL" in sql
    assert "__kf_metric_time" in sql
    assert "__kf_axis" in sql


def test_the_textual_path_keeps_one_branch_for_a_physical_axis() -> None:
    # 局部导入:``semantic.s2sql_translator`` 与 ``query`` 包之间有既有的循环导入,
    # 模块级先导它会炸。本文件不需要 query 模块,不为绕开而加无用导入。
    import knowflow_analytics.query  # noqa: F401
    from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

    translated = S2SqlSemanticTranslator().translate(
        release=_release(logical_axis=True),
        dataset_id="ds_o",
        corrected_s2sql=(
            'SELECT "下单日期", SUM("收入"), COUNT("订单数") '
            'FROM "订单分析" GROUP BY "下单日期"'
        ),
    )

    assert "UNION ALL" not in translated.physical_query.sql


def test_filtering_on_the_logical_axis_uses_each_metric_own_axis() -> None:
    """逻辑轴用作过滤时同样逐指标对齐。

    实测漏掉这条会让过滤渲染成锚点轴的物理列(``m0."order_date"``),两个指标
    又共用一根轴——正是逻辑轴要消除的那个错答。
    """

    from knowflow_analytics.contracts import FilterOperator, QueryFilter

    physical = SemanticTranslator().translate(
        release=_release(logical_axis=True),
        query=SemanticQuery(
            dataset_id="ds_o",
            metric_ids=("mt_rev", "mt_cnt"),
            filters=(
                QueryFilter(
                    dimension_id="d_mt",
                    operator=FilterOperator.GTE,
                    value="2026-08-01",
                ),
            ),
        ),
    )

    assert "UNION ALL" in physical.sql
    assert '"__kf_metric_time" >=' in physical.sql
    assert '"order_date" >=' not in physical.sql


def _apply_axis_filters(release, *, metric_ids):
    from datetime import UTC, datetime

    from knowflow_analytics.contracts import SemanticQueryType
    from knowflow_analytics.query.parser import _apply_time_filters

    dataset = release.datasets[0]
    return _apply_time_filters(
        question="2026年8月的收入和订单数",
        release=release,
        dataset=dataset,
        mapped_dimension_ids=[],
        selected_metric_ids=list(metric_ids),
        existing_filters=[],
        now=datetime(2026, 9, 15, tzinfo=UTC),
        selected_time_dimension_id=None,
        query_type=SemanticQueryType.AGGREGATE,
    )


def test_multiple_axes_route_to_the_logical_axis_instead_of_asking() -> None:
    """有逻辑轴时不再追问:用户答不上「按下单还是按支付」,他要的就是各按各的。

    而且无论他选哪根,另一个指标的数都是错的——那个澄清并没有解决问题,只是让
    用户参与选择哪个数字错。
    """

    filters, _ = _apply_axis_filters(_release(logical_axis=True), metric_ids=("mt_rev", "mt_cnt"))

    assert {item.dimension_id for item in filters} == {"d_mt"}


def test_a_release_without_the_logical_axis_still_asks() -> None:
    """存量 Release 没有合成逻辑轴,那里仍然追问——总比静默给错数好。"""

    from knowflow_analytics.query.errors import ClarificationSignal

    with pytest.raises(ClarificationSignal) as excinfo:
        _apply_axis_filters(_release(logical_axis=False), metric_ids=("mt_rev", "mt_cnt"))
    assert excinfo.value.code == "AMBIGUOUS_TIME_DIMENSION"
