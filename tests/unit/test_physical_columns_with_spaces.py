"""物理列名带空格（MySQL 允许 `Enc Type` 这样的列名）不能让建模失败。

客户实机：AI 自动建模报 DIMENSION_EXPRESSION_INVALID
「semantic expression references an unknown field: Enc」。裸列名当 SQL 解析，
`Enc Type` 被读成 `Enc AS Type`，于是去找一个叫 Enc 的字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from knowflow_analytics.modeling.ai_artifacts import ensure_default_count_metrics
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _rename_column(payload: dict, model_id: str, old: str, new: str) -> None:
    """把一个物理列改名，并同步所有裸引用它的地方（模型内标识/维度/度量、目录维度）。"""
    for model in payload["models"]:
        if model["id"] != model_id:
            continue
        detail = model["modelDetail"]
        for field in detail["fields"]:
            if field["fieldName"] == old:
                field["fieldName"] = new
        for item in detail["identifiers"]:
            if item["bizName"] == old:
                item["bizName"] = new
        for item in detail["dimensions"] + detail["measures"]:
            if item["expr"] == old:
                item["expr"] = new
    for item in payload["dimensions"]:
        if item["modelId"] == model_id and item["expr"] == old:
            item["expr"] = new


def _catalog_with(model_id: str, old: str, new: str) -> SemanticCatalog:
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    _rename_column(payload, model_id, old, new)
    return SemanticCatalog.model_validate(payload)


def test_a_dimension_on_a_column_with_a_space_compiles_and_binds_that_column():
    catalog = _catalog_with("model_orders", "channel", "Enc Type")

    release = compile_semantic_catalog(catalog)

    fields = {item.id: item for item in release.fields}
    dimension = next(item for item in release.dimensions if item.id == "dimension_channel")
    assert fields[dimension.field_id].column == "Enc Type"
    # 裸列引用不是计算表达式，投影里不应带 expression
    assert dimension.expression is None
    assert dimension.expression_field_ids == ()


def test_a_measure_on_a_column_with_a_space_compiles():
    catalog = _catalog_with("model_orders", "amount", "Order Amount")

    release = compile_semantic_catalog(catalog)

    columns = {item.column for item in release.fields if item.model_id == "model_orders"}
    assert "Order Amount" in columns


def test_the_default_count_metric_quotes_a_primary_identifier_column_with_a_space():
    """AI 建模为主标识派生 COUNT(...) 指标；列名带空格时表达式必须仍可解析。"""
    payload = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    _rename_column(payload, "model_orders", "id", "Order Id")
    # 夹具里人工写的 COUNT(DISTINCT id) 也跟着列改名，用户自己写表达式时会加引号
    for metric in payload["metrics"]:
        params = metric.get("metricDefineByFieldParams")
        if params and params["expr"] == "COUNT(DISTINCT id)":
            params["expr"] = 'COUNT(DISTINCT "Order Id")'
            params["fields"] = [{"fieldName": "Order Id"}]
    catalog = SemanticCatalog.model_validate(payload)

    counted, generated = ensure_default_count_metrics(catalog)

    assert generated, "主标识存在时应派生默认计数指标"
    release = compile_semantic_catalog(counted)
    default_count = next(item for item in release.metrics if item.id == generated[0].id)
    assert default_count is not None
