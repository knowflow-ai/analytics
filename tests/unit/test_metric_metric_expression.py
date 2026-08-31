"""METRIC 型指标（指标组合指标）的表达式校验。

上游 MetricCheckUtils.java:20-32 对这一形态有三条硬校验:typeParams 非空、
metrics 列表非空、**表达式中不可再包含聚合函数**（"基于指标来创建指标"）。

我们此前只检查「依赖 bizName 是否都出现在 formula」,不解析表达式:
- SUM(指标A) 能通过建模,翻译期字符串替换展开成 SUM((SUM(x))) 嵌套聚合;
- 未声明的 token 原样穿过,直接进物理 SQL。

这是建模期唯一无表达式校验的路径。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.semantic_expression import (
    validate_metric_metric_expression,
)


def test_rejects_aggregate_function() -> None:
    """依赖指标自带聚合,再包一层会翻成嵌套聚合。"""

    with pytest.raises(SemanticValidationError) as exc_info:
        validate_metric_metric_expression("SUM(net_revenue)", dependencies=("net_revenue",))
    assert exc_info.value.code == "METRIC_METRIC_EXPRESSION_INVALID"


def test_rejects_window_function() -> None:
    with pytest.raises(SemanticValidationError) as exc_info:
        validate_metric_metric_expression(
            "SUM(net_revenue) OVER ()", dependencies=("net_revenue",)
        )
    assert exc_info.value.code == "METRIC_METRIC_EXPRESSION_INVALID"


def test_rejects_undeclared_token() -> None:
    """未声明的依赖不能原样穿过进物理 SQL。"""

    with pytest.raises(SemanticValidationError) as exc_info:
        validate_metric_metric_expression(
            "net_revenue - refund_amount", dependencies=("net_revenue",)
        )
    assert exc_info.value.code == "METRIC_METRIC_EXPRESSION_INVALID"


def test_accepts_plain_arithmetic_over_declared_metrics() -> None:
    """正常形态:纯算术组合已声明的依赖指标。"""

    referenced = validate_metric_metric_expression(
        "net_revenue - refund_amount",
        dependencies=("net_revenue", "refund_amount"),
    )
    assert set(referenced) == {"net_revenue", "refund_amount"}


def test_accepts_parentheses_and_division() -> None:
    referenced = validate_metric_metric_expression(
        "(net_revenue - refund_amount) / net_revenue",
        dependencies=("net_revenue", "refund_amount"),
    )
    assert set(referenced) == {"net_revenue", "refund_amount"}


def test_rejects_unparseable_expression() -> None:
    with pytest.raises(SemanticValidationError) as exc_info:
        validate_metric_metric_expression("net_revenue -", dependencies=("net_revenue",))
    assert exc_info.value.code == "METRIC_METRIC_EXPRESSION_INVALID"


def test_compiler_rejects_nested_aggregate_in_a_metric_of_metrics(sales_catalog) -> None:
    """校验必须接进编译期,否则建模照样能存进 SUM(指标A)。"""

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.catalog_contracts import (
        MetricDefineByMetricParamsContract,
        MetricParamContract,
    )

    source = next(item for item in sales_catalog.metrics if item.name == "净收入")
    broken = source.model_copy(
        update={
            "id": "metric_of_metrics",
            "name": "净收入放大",
            "biz_name": "net_revenue_boost",
            "metric_define_type": "METRIC",
            "metric_define_by_field_params": None,
            "metric_define_by_measure_params": None,
            "metric_define_by_metric_params": MetricDefineByMetricParamsContract(
                expr=f"SUM({source.biz_name})",
                metrics=(
                    MetricParamContract(id=source.id, biz_name=source.biz_name),
                ),
            ),
        }
    )
    catalog = sales_catalog.model_copy(
        update={"metrics": (*sales_catalog.metrics, broken)}
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        compile_semantic_catalog(catalog)
    assert exc_info.value.code == "METRIC_METRIC_EXPRESSION_INVALID"
