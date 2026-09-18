"""发给模型的 JSON Schema 里不该有只给开发者看的东西。

12 处 ``infer_json`` 调用一律把 ``model_json_schema()`` 原样发出去，于是每次请求都带上：

- pydantic 自动生成的 ``title``（``"title": "Sql"`` 就挨在键 ``sql`` 旁边，零信息），
  连内部类名都漏出去（``"title": "_LlmS2SqlOutput"``）；
- 类 docstring 变成的 ``description``——那是写给维护者的工程笔记。实测 S2SQL 那份里是
  「**模型说的不算数，要过一道确定性校验**……实验里 12 题没出现过编造」，建模那份里是
  「此前聚合方式是 ``filedType`` 单选枚举里的一个取值……」。模型读到的是我们的提交历史。
- ``default``——默认值是 pydantic 解析时补的，与模型怎么生成无关。

剥掉不改变 schema 约束的任何一条（字段名、类型、required、additionalProperties 全留），
所以不影响功能。

**但不能朴素地递归删 key**：``properties`` 与 ``$defs`` 下面的键是字段名和类型名，不是
JSON Schema 关键字。``ModelSchemaContract`` 就有一个真的叫 ``description`` 的字段，朴素
删法会把它删掉，AI 建模从此不再产出描述——一个不报错的功能回归。
"""

from __future__ import annotations

import json
from pathlib import Path

from knowflow_analytics.gateways.prompt_schema import prompt_json_schema

_SRC = Path(__file__).parents[2] / "src" / "knowflow_analytics"


def test_pydantic_titles_and_docstrings_are_stripped() -> None:
    schema = {
        "title": "_LlmS2SqlOutput",
        "description": "内部工程笔记",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sql": {"title": "Sql", "type": "string", "minLength": 1},
            "thought": {"title": "Thought", "type": "string", "default": ""},
        },
        "required": ["sql"],
    }

    stripped = prompt_json_schema(schema)

    assert stripped == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sql": {"type": "string", "minLength": 1},
            "thought": {"type": "string"},
        },
        "required": ["sql"],
    }


def test_a_field_actually_named_description_survives() -> None:
    """``ModelSchemaContract.description`` 是真字段。删掉它就是不报错的功能回归。"""

    schema = {
        "title": "ModelSchemaContract",
        "type": "object",
        "properties": {
            "name": {"title": "Name", "type": "string"},
            "description": {"title": "Description", "type": "string"},
            "default": {"title": "Default", "type": "string"},
            "title": {"title": "Title", "type": "string"},
        },
        "required": ["name", "description"],
    }

    stripped = prompt_json_schema(schema)

    assert set(stripped["properties"]) == {"name", "description", "default", "title"}
    assert stripped["properties"]["description"] == {"type": "string"}
    assert stripped["required"] == ["name", "description"]


def test_nested_defs_and_arrays_are_stripped_too() -> None:
    schema = {
        "$defs": {
            "InferredTerm": {
                "title": "InferredTerm",
                "description": "docstring",
                "type": "object",
                "properties": {"phrase": {"title": "Phrase", "type": "string"}},
            }
        },
        "properties": {
            "inferred_terms": {
                "title": "Inferred Terms",
                "type": "array",
                "default": [],
                "items": {"$ref": "#/$defs/InferredTerm"},
            }
        },
        "type": "object",
    }

    stripped = prompt_json_schema(schema)

    assert stripped["$defs"]["InferredTerm"] == {
        "type": "object",
        "properties": {"phrase": {"type": "string"}},
    }
    assert stripped["properties"]["inferred_terms"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/InferredTerm"},
    }


def test_the_real_s2sql_schema_loses_nothing_that_constrains_the_output() -> None:
    from knowflow_analytics.query.parser import _LlmS2SqlOutput

    raw = _LlmS2SqlOutput.model_json_schema()
    stripped = prompt_json_schema(raw)

    assert stripped["required"] == raw["required"]
    assert set(stripped["properties"]) == set(raw["properties"])
    assert stripped["additionalProperties"] is False
    assert stripped["properties"]["sql"]["maxLength"] == raw["properties"]["sql"]["maxLength"]
    # 省下来的是纯注解。
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    assert len(json.dumps(stripped, **compact)) < len(json.dumps(raw, **compact)) * 0.6


def test_model_guidance_must_live_in_the_prompt_not_in_the_schema() -> None:
    """这道剥离会把 ``Field(description=...)`` 一起剥掉。

    全仓当前一处都没有——每条 description 都来自 docstring。谁想给模型写指引，写进
    prompt，不要写进 schema：schema 是约束，prompt 才是教学。这里先红，省得将来
    有人加了一句指引、再被这道剥离静默吃掉。
    """

    offenders = [
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if "description=" in path.read_text(encoding="utf-8").replace("\n", "")
        and any(
            "description=" in segment.split(")")[0]
            for segment in path.read_text(encoding="utf-8").split("Field(")[1:]
        )
    ]

    assert offenders == [], offenders
