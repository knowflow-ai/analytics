"""非日期类型的时间列必须按 dateFormat 渲染过滤字面量。

我们自己的规则分类器 classify.py:132 有一条明确分支:列名像时间
(date|time|at|ts|dt|日期|时间)且类型是数值或文本时,判为时间维度。所以
`stat_date int` / `dt varchar(8)` 这类列会真的成为时间维度。

确定性时间过滤此前一律绑定 Python date 对象。PG16 实测:
    int     >= date  ->  operator does not exist: integer >= date
    varchar >= date  ->  operator does not exist: character varying >= date
    date    >= date  ->  正常
而按格式渲染成字符串后,int 与 varchar 两种列都能正常比较(PG 会把未定型
字面量强制转换),date 列则继续用 date 对象。

上游把 dateFormat 写进 ext[time_format] 并在 TimeCorrector 用它格式化自动
补的时间区间;我们此前写进 ext["dateFormat"] 后全仓无任何读取点。
"""

from __future__ import annotations

from datetime import date

import pytest

from knowflow_analytics.query.time_literals import render_time_bound


@pytest.mark.parametrize(
    ("data_type", "date_format", "expected"),
    [
        # 真正的日期列:保持 date 对象,不做任何转换
        ("date", "yyyy-MM-dd", date(2026, 8, 2)),
        ("timestamp", "yyyy-MM-dd", date(2026, 8, 2)),
        # 数值/文本列:按格式渲染成字符串
        ("int", "yyyyMMdd", "20260802"),
        ("bigint", "yyyyMMdd", "20260802"),
        ("varchar", "yyyy-MM-dd", "2026-08-02"),
        ("text", "yyyyMMdd", "20260802"),
    ],
)
def test_bound_matches_the_physical_column(data_type, date_format, expected) -> None:
    rendered = render_time_bound(
        date(2026, 8, 2), data_type=data_type, date_format=date_format
    )
    assert rendered == expected


def test_missing_format_falls_back_to_the_upstream_default() -> None:
    """上游 Dimension.dateFormat 默认 yyyy-MM-dd;缺失时不应退回 date 对象。"""

    rendered = render_time_bound(date(2026, 8, 2), data_type="varchar", date_format=None)
    assert rendered == "2026-08-02"


def test_unknown_format_tokens_are_left_alone() -> None:
    """不认识的格式串原样处理,不猜:宁可让用户看到不匹配,也不悄悄换一种格式。"""

    rendered = render_time_bound(
        date(2026, 8, 2), data_type="varchar", date_format="yyyy年MM月dd日"
    )
    assert rendered == "2026年08月02日"


def test_unknown_data_type_keeps_the_date_object() -> None:
    """类型信息缺失时保持既有行为,不引入新的猜测。"""

    rendered = render_time_bound(date(2026, 8, 2), data_type=None, date_format="yyyyMMdd")
    assert rendered == date(2026, 8, 2)


def _time_dimension_variant(sales_catalog, *, data_type: str, date_format: str):
    """把销售目录里的「下单日期」改成非日期物理类型 + 指定格式。"""

    dimensions = tuple(
        item.model_copy(
            update={
                "data_type": data_type,
                "ext": {**item.ext, "dateFormat": date_format},
            }
        )
        if item.name == "下单日期"
        else item
        for item in sales_catalog.dimensions
    )
    return sales_catalog.model_copy(update={"dimensions": dimensions})


def test_date_format_survives_compilation(sales_catalog) -> None:
    """建模期录入的格式必须编译进查询投影,否则渲染无从判断。"""

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog

    catalog = _time_dimension_variant(sales_catalog, data_type="int", date_format="yyyyMMdd")
    release = compile_semantic_catalog(catalog)

    dimension = next(item for item in release.dimensions if item.name == "下单日期")
    assert dimension.date_format == "yyyyMMdd"
    assert dimension.data_type == "int"


def test_date_format_defaults_to_the_upstream_value(sales_catalog) -> None:
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog

    release = compile_semantic_catalog(sales_catalog)
    dimension = next(item for item in release.dimensions if item.name == "下单日期")
    assert dimension.date_format == "yyyy-MM-dd"


def test_date_format_reaches_the_prompt(sales_catalog) -> None:
    """对齐上游 PromptHelper 的 FORMAT '...':模型要知道列里长什么样。"""

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.query.parser import _dimension_payload

    catalog = _time_dimension_variant(sales_catalog, data_type="int", date_format="yyyyMMdd")
    release = compile_semantic_catalog(catalog)
    payload = _dimension_payload(release, release.datasets[0])

    entry = next(item for item in payload if item["name"] == "下单日期")
    assert entry["date_format"] == "yyyyMMdd"


def test_categorical_dimension_omits_the_format_key(sales_catalog) -> None:
    """只有时间维度需要格式;普通维度带上它是纯噪声。"""

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.query.parser import _dimension_payload

    release = compile_semantic_catalog(sales_catalog)
    payload = _dimension_payload(release, release.datasets[0])

    entry = next(item for item in payload if item["name"] == "区域")
    assert "date_format" not in entry


def test_deterministic_filter_binds_a_comparable_literal(sales_catalog) -> None:
    """确定性时间过滤的边界必须与物理列可比较。

    这是本条修复的落点:int 列上绑定 date 对象,PG 直接报
    operator does not exist: integer >= date。
    """

    from knowflow_analytics.contracts import (
        FilterOperator,
        QueryFilter,
        SemanticQuery,
    )
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.semantic import SemanticTranslator

    catalog = _time_dimension_variant(sales_catalog, data_type="int", date_format="yyyyMMdd")
    release = compile_semantic_catalog(catalog)
    time_dimension = next(item for item in release.dimensions if item.name == "下单日期")

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id=release.datasets[0].id,
            metric_ids=("net_revenue",),
            filters=(
                QueryFilter(
                    dimension_id=time_dimension.id,
                    operator=FilterOperator.GTE,
                    value=date(2026, 8, 2),
                ),
            ),
        ),
    )

    assert "20260802" in {str(value) for value in physical.parameters.values()}
    assert date(2026, 8, 2) not in physical.parameters.values()


def test_real_date_column_still_binds_a_date(sales_catalog) -> None:
    """日期列保持既有行为:参数化绑定 date 对象最精确,不要一刀切转字符串。"""

    from knowflow_analytics.contracts import (
        FilterOperator,
        QueryFilter,
        SemanticQuery,
    )
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.semantic import SemanticTranslator

    release = compile_semantic_catalog(sales_catalog)
    time_dimension = next(item for item in release.dimensions if item.name == "下单日期")

    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id=release.datasets[0].id,
            metric_ids=("net_revenue",),
            filters=(
                QueryFilter(
                    dimension_id=time_dimension.id,
                    operator=FilterOperator.GTE,
                    value=date(2026, 8, 2),
                ),
            ),
        ),
    )

    assert date(2026, 8, 2) in physical.parameters.values()
