"""聚合方式从列分类枚举里搬到独立的 metrics 区块。

建模格式对照实验（8 组 × 21 表 × 2 次）里，58 个真正可加的列在旧结构下只有
20 个被判成度量——`measure` 要和 `categorical` 抢 `filedType` 这一个槽位，
抢不过。提成独立区块后是 56 个，键（55→54）和时间维度（17→16）在噪声内。

失败模式是「漏建模」而不是「算错」：用户问「总交易额」时压根没有指标可映射。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.catalog_contracts import (
    AggOperator,
    ModelSchemaContract,
    SemanticColumnContract,
    SemanticColumnType,
    SemanticMetricContract,
)


def _column(name: str, filed_type: SemanticColumnType) -> SemanticColumnContract:
    return SemanticColumnContract(
        column_name=name, data_type="numeric", filed_type=filed_type, name=name, expr=name
    )


def test_aggregation_no_longer_competes_for_the_column_slot() -> None:
    """filedType 只描述分组/连接角色，没有 measure 可选。"""

    assert [item.value for item in SemanticColumnType] == [
        "primary_key",
        "foreign_key",
        "partition_time",
        "time",
        "categorical",
    ]
    assert "agg" not in SemanticColumnContract.model_fields


def test_metrics_are_independent_of_the_column_classification() -> None:
    schema = ModelSchemaContract(
        name="订单",
        biz_name="orders",
        semantic_columns=(
            _column("order_id", SemanticColumnType.PRIMARY_KEY),
            _column("net_amount", SemanticColumnType.CATEGORICAL),
        ),
        metrics=(SemanticMetricContract(column_name="net_amount", agg=AggOperator.SUM),),
    )
    assert schema.metrics[0].column_name == "net_amount"


def test_a_metric_on_an_undeclared_column_is_dropped_not_fatal() -> None:
    """模型偶尔把可聚合的列只写进 metrics、漏掉 semanticColumns。

    硬拒会让一次小失误搞挂整个建模跑：重试输入相同、输出相同，三次全烧掉——
    这正是聚合方式还在列上时 measure + NONE 那次事故的形状。
    """

    schema = ModelSchemaContract.model_validate(
        {
            "name": "订单",
            "bizName": "orders",
            "semanticColumns": [
                {
                    "columnName": "net_amount",
                    "dataType": "numeric",
                    "filedType": "categorical",
                    "name": "净额",
                    "expr": "net_amount",
                }
            ],
            "metrics": [
                {"columnName": "net_amount", "agg": "SUM"},
                {"columnName": "no_such_column", "agg": "SUM"},
            ],
        }
    )
    assert [item.column_name for item in schema.metrics] == ["net_amount"]


def test_the_prompt_requires_every_physical_column_in_semantic_columns() -> None:
    """生产 prompt 曾说「最多输出一次」,模型据此把度量列整个省略掉,
    于是那一列既没有维度也没有指标。实验里 H 组用的是「必须且只出现一次」。"""

    import inspect

    from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller

    source = inspect.getsource(AiSemanticModeller._messages)
    assert "必须且只输出一次 SemanticColumn" in source
    assert "可聚合的数值列同样要出现在 semanticColumns 里" in source


def test_one_column_cannot_carry_two_metrics() -> None:
    with pytest.raises(ValidationError):
        ModelSchemaContract(
            name="订单",
            biz_name="orders",
            semantic_columns=(_column("net_amount", SemanticColumnType.CATEGORICAL),),
            metrics=(
                SemanticMetricContract(column_name="net_amount", agg=AggOperator.SUM),
                SemanticMetricContract(column_name="net_amount", agg=AggOperator.AVG),
            ),
        )


def test_a_metric_without_a_governed_aggregation_is_rejected() -> None:
    """旧结构下 measure + NONE 让整个建模跑挂；现在结构上不可能。"""

    for bad in (AggOperator.NONE, AggOperator.TOPN, AggOperator.UNKNOWN):
        with pytest.raises(ValidationError):
            SemanticMetricContract(column_name="net_amount", agg=bad)


def test_a_column_in_the_metrics_block_becomes_a_measure_field() -> None:
    """metrics 优先于列分类：可聚合的数值列在 filedType 上通常是 categorical。"""

    column = _column("net_amount", SemanticColumnType.CATEGORICAL)
    metric = SemanticMetricContract(column_name="net_amount", agg=AggOperator.SUM, unit="元")
    changes = AiSemanticModeller._column_changes(column, {"net_amount": metric})
    assert changes["kind"] == "measure"
    assert changes["aggregation"] == "sum"
    assert changes["create_metric"] is True
    assert changes["unit"] == "元"


def test_a_column_outside_the_metrics_block_stays_a_dimension() -> None:
    column = _column("channel", SemanticColumnType.CATEGORICAL)
    changes = AiSemanticModeller._column_changes(column, {})
    assert changes["kind"] == "dimension"
    assert changes["dimension_type"] == "categorical"
    assert "aggregation" not in changes


def test_keys_and_time_are_unaffected_by_the_metrics_block() -> None:
    """实验里键与时间维度的准确率没有因为这次改动下降，转换也必须保持原样。"""

    for filed_type, expected in (
        (SemanticColumnType.PRIMARY_KEY, ("identifier", "primary")),
        (SemanticColumnType.FOREIGN_KEY, ("identifier", "foreign")),
    ):
        changes = AiSemanticModeller._column_changes(_column("k", filed_type), {})
        assert (changes["kind"], changes["identifier_type"]) == expected
    for filed_type, expected in (
        (SemanticColumnType.TIME, "time"),
        (SemanticColumnType.PARTITION_TIME, "partition_time"),
    ):
        changes = AiSemanticModeller._column_changes(_column("t", filed_type), {})
        assert changes["kind"] == "time"
        assert changes["dimension_type"] == expected
