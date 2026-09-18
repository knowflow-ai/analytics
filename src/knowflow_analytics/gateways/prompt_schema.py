"""把 pydantic 的 JSON Schema 收拾成只剩约束的形状，再发给模型。

12 处 ``infer_json`` 调用一律把 ``model_json_schema()`` 原样发出去（网关再包成
``response_format: json_schema``），于是每次请求都替开发者的注释付一遍钱：自动生成的
``title``（``"title": "Sql"`` 就挨在键 ``sql`` 旁边）、类 docstring 变成的
``description``（实测里是我们的提交沿革），以及 ``default``（默认值是 pydantic 解析时
补的，与模型怎么生成无关）。

剥离不动任何一条真正的约束——字段名、类型、``required``、``additionalProperties``、
长度与枚举全部原样保留，所以模型能生成的合法输出集合一个字都没变。

``properties`` / ``$defs`` 这些容器下面的键是**字段名和类型名**，不是 JSON Schema
关键字。``ModelSchemaContract`` 就有一个真的叫 ``description`` 的字段——朴素地递归删 key
会把它删掉，AI 建模从此不再产出描述，而且不报错。
"""

from __future__ import annotations

from typing import Any

#: 只对开发者有意义的注解关键字。
_ANNOTATIONS = frozenset({"title", "description", "default"})

#: 这些关键字底下的键是名字（字段名、类型名、模式名），不是关键字，一个都不能删。
_NAME_KEYED = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)


def prompt_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """返回同一份 schema，去掉只给开发者看的注解。"""

    stripped = _strip(schema)
    assert isinstance(stripped, dict)
    return stripped


def _strip(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip(item) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _NAME_KEYED and isinstance(value, dict):
            result[key] = {name: _strip(sub) for name, sub in value.items()}
            continue
        if key in _ANNOTATIONS:
            continue
        result[key] = _strip(value)
    return result
