"""普通字段（没有角色的物理列）也要能起业务名。

客户实机（knowflow-ai/analytics#2）：业务实体里改字段名，提示已保存但名字没变。
此前 ModelFieldContract 只有 fieldName / dataType，普通字段的名字无处可存。
"""

from __future__ import annotations

import json
from pathlib import Path

from knowflow_analytics.contracts import FieldKind
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _payload() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _orders(payload: dict) -> dict:
    return next(item for item in payload["models"] if item["id"] == "model_orders")


def test_a_plain_field_display_name_is_kept_and_compiled_into_the_field_name():
    payload = _payload()
    detail = _orders(payload)["modelDetail"]
    # 让 channel 变成普通字段（去掉它的维度角色），再给它起业务名
    detail["dimensions"] = [item for item in detail["dimensions"] if item["expr"] != "channel"]
    payload["dimensions"] = [item for item in payload["dimensions"] if item["expr"] != "channel"]
    payload["dataSets"] = []
    payload["terms"] = []
    payload["dimensionValues"] = [
        item for item in payload["dimensionValues"] if item["dimensionId"] != "dimension_channel"
    ]
    for field in detail["fields"]:
        if field["fieldName"] == "channel":
            field["name"] = "销售渠道编码"
    catalog = SemanticCatalog.model_validate(payload)

    round_tripped = catalog.canonical_payload()
    channel_entry = next(
        f for f in _orders(round_tripped)["modelDetail"]["fields"] if f["fieldName"] == "channel"
    )
    assert channel_entry["name"] == "销售渠道编码"

    release = compile_semantic_catalog(catalog)
    field = next(item for item in release.fields if item.column == "channel")
    assert field.kind is FieldKind.FIELD
    assert field.name == "销售渠道编码"


def test_a_role_name_wins_over_the_physical_display_name():
    payload = _payload()
    for field in _orders(payload)["modelDetail"]["fields"]:
        if field["fieldName"] == "channel":
            field["name"] = "物理层起的名字"
    catalog = SemanticCatalog.model_validate(payload)

    release = compile_semantic_catalog(catalog)
    field = next(item for item in release.fields if item.column == "channel")
    assert field.name == "渠道"


def test_catalogs_without_display_names_serialize_exactly_as_before():
    """spec_hash 覆盖目录投影；没起名的存量目录序列化不能多出一个键，否则升级后
    全部 Release 的哈希漂移，语义索引与 AI 建模产物一起被判过期。"""
    catalog = SemanticCatalog.model_validate(_payload())

    for model in catalog.canonical_payload()["models"]:
        for field in model["modelDetail"]["fields"]:
            assert set(field) == {"fieldName", "dataType"}
