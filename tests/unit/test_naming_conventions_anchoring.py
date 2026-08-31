"""跨表命名复用的锚定合同（I1/I2）。

事故背景（2026-08-28 城市/图书馆回归，3/12）：NamingConventions 以裸列名为
全局 key，城市表先把 名称 定为「城市名称」，图书馆表的 名称 列被直接沿用、
不再询问模型——整个 Release 不存在「图书馆名称」，且「城市」一词被劫持。

合同：跨表硬沿用只允许含义有锚点的列（非主键标识列，如 客户ID/图书馆id）；
描述本表实体自身属性的列（名称/地址/类型……）必须逐表询问模型。兜底永远是
零信息的裸列名，绝不是从别的表借来的实体名。
"""

from __future__ import annotations

import json

from knowflow_analytics.contracts import FieldKind, FieldSpec
from knowflow_analytics.modeling.classify import Prefill
from knowflow_analytics.modeling.contracts import SchemaColumnSnapshot, TableSnapshot
from knowflow_analytics.modeling.workflow import NamingConventions, StagedTableModeler


class _ScriptedNamingGateway:
    """按 (table, column) 返回名字；记录每次命名调用问了哪些列。"""

    def __init__(self, answers: dict[tuple[str, str], str]) -> None:
        self._answers = answers
        self.asked: list[tuple[str, tuple[str, ...]]] = []

    def generate_json(self, *, purpose, messages, response_schema, trace):
        assert purpose == "analytics.modeling.naming", purpose
        payload = json.loads(messages[-1]["content"])
        table = payload["table"]["name"].split(".", 1)[1]
        columns = tuple(item["column"] for item in payload["columns"])
        self.asked.append((table, columns))
        return {
            "columns": [
                {
                    "column_name": column,
                    "name": self._answers.get((table, column), column),
                    "description": "",
                }
                for column in columns
            ]
        }


def _table(name: str, columns: tuple[str, ...]) -> TableSnapshot:
    return TableSnapshot(
        schema_name="public",
        name=name,
        columns=tuple(
            SchemaColumnSnapshot(
                name=column,
                data_type="text",
                nullable=True,
                ordinal_position=index,
            )
            for index, column in enumerate(columns)
        ),
    )


def _field(table: str, column: str, kind: FieldKind) -> FieldSpec:
    return FieldSpec(
        id=f"{table}.{column}",
        model_id=table,
        name=column,
        column=column,
        kind=kind,
    )


def _prefill(column: str, kind: FieldKind, identifier_type: str | None = None) -> Prefill:
    return Prefill(
        column=column,
        kind=kind,
        identifier_type=identifier_type,
        confidence=1.0,
        reason="test",
    )


def _build(modeler, gateway, conventions, table_name, specs):
    table = _table(table_name, tuple(column for column, _kind, _id_type in specs))
    fields = tuple(_field(table_name, column, kind) for column, kind, _id_type in specs)
    prefills = {
        column: _prefill(column, kind, identifier_type) for column, kind, identifier_type in specs
    }
    return modeler.build_table(
        table=table,
        fields=fields,
        role=None,
        role_name="dimension",
        topology=None,
        profile=None,
        prefills=prefills,
        conventions=conventions,
        trace={"revision_id": "rev_test"},
    )


def test_entity_attribute_columns_are_named_per_table_not_borrowed() -> None:
    gateway = _ScriptedNamingGateway(
        {
            ("城市", "名称"): "城市名称",
            ("图书馆", "名称"): "图书馆名称",
            ("图书馆", "地址"): "图书馆地址",
            ("城市", "地址"): "城市地址",
        }
    )
    modeler = StagedTableModeler(gateway, max_concurrency=1)
    conventions = NamingConventions()

    first = _build(
        modeler,
        gateway,
        conventions,
        "城市",
        (("名称", FieldKind.DIMENSION, None), ("地址", FieldKind.DIMENSION, None)),
    )
    second = _build(
        modeler,
        gateway,
        conventions,
        "图书馆",
        (("名称", FieldKind.DIMENSION, None), ("地址", FieldKind.DIMENSION, None)),
    )

    # 图书馆表的实体属性列必须真的送给了模型（没有被约定 settle 掉）。
    library_asked = dict(gateway.asked)["图书馆"]
    assert "名称" in library_asked and "地址" in library_asked
    names = {item.column_name: item.name for item in second.contract.semantic_columns}
    assert names["名称"] == "图书馆名称"
    assert names["地址"] == "图书馆地址"
    first_names = {item.column_name: item.name for item in first.contract.semantic_columns}
    assert first_names["名称"] == "城市名称"


def test_foreign_identifier_names_are_still_reused_across_tables() -> None:
    gateway = _ScriptedNamingGateway({("员工数量", "图书馆id"): "图书馆ID"})
    modeler = StagedTableModeler(gateway, max_concurrency=1)
    conventions = NamingConventions()

    _build(
        modeler,
        gateway,
        conventions,
        "员工数量",
        (("图书馆id", FieldKind.IDENTIFIER, "foreign"),),
    )
    second = _build(
        modeler,
        gateway,
        conventions,
        "借阅记录",
        (("图书馆id", FieldKind.IDENTIFIER, "foreign"),),
    )

    # 外键列含义锚在所指实体上：第二张表不再询问，直接沿用。
    asked_tables = [table for table, columns in gateway.asked if "图书馆id" in columns]
    assert asked_tables == ["员工数量"]
    names = {item.column_name: item.name for item in second.contract.semantic_columns}
    assert names["图书馆id"] == "图书馆ID"


def test_primary_keys_never_share_names_across_tables() -> None:
    gateway = _ScriptedNamingGateway(
        {("城市", "词条id"): "城市ID", ("图书馆", "词条id"): "图书馆标识"}
    )
    modeler = StagedTableModeler(gateway, max_concurrency=1)
    conventions = NamingConventions()

    _build(modeler, gateway, conventions, "城市", (("词条id", FieldKind.IDENTIFIER, "primary"),))
    second = _build(
        modeler, gateway, conventions, "图书馆", (("词条id", FieldKind.IDENTIFIER, "primary"),)
    )

    names = {item.column_name: item.name for item in second.contract.semantic_columns}
    assert names["词条id"] == "图书馆标识"


def test_naming_prompt_forbids_borrowing_entity_names_across_tables() -> None:
    from knowflow_analytics.modeling.prompts import NAMING_SYSTEM

    assert "直接沿用" not in NAMING_SYSTEM
    assert "本表" in NAMING_SYSTEM
