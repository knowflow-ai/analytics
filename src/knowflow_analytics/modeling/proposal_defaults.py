from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from knowflow_analytics.contracts import FieldSpec, ModelSpec
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    SchemaSnapshot,
    SuggestionDecision,
    SuggestionPatch,
    SuggestionSource,
)

# 这些值是"还没分类"的默认态，不是人填的：FieldSpec 上 kind 默认 "field"、
# create_dimension / create_metric 默认 False，一张刚导入的表每个字段都是它们。
# 把它们当成已有值，AI 的每条分类建议都会被判成"覆盖"而默认不勾选 ——
# 「确认并应用全部」只应用勾选项，于是分类一条都没进去。
_UNSET_VALUES: dict[str, frozenset[Any]] = {
    "kind": frozenset({"field"}),
    "create_dimension": frozenset({False}),
    "create_metric": frozenset({False}),
}


def _is_placeholder_name(current: dict[str, Any], value: Any) -> bool:
    """导入时 name 就是物理列名 / 表名。它还等于物理标识，说明没人改过。

    AI 对一个字段的建议把 name、kind、create_dimension 打包在一条里；只要
    name 被当成人填的值，整条（含分类）就默认不勾选 —— 刚导入的表上每条都是。
    """

    physical = current.get("column") or current.get("table")
    return bool(physical) and value == physical


def _is_blank(
    key: str, value: Any, current: dict[str, Any], physical_comment: str | None = None
) -> bool:
    if value is None or value == "":
        return True
    if value in _UNSET_VALUES.get(key, frozenset()):
        return True
    # 导入把 biz_name 抄成表名/列名、description 抄成数据库注释 —— 都不是人写的。
    # 把它们当人工内容，AI 对刚导入模型的每条建议都会被判成"覆盖"而默认不勾选。
    if key in {"name", "biz_name"} and _is_placeholder_name(current, value):
        return True
    return key == "description" and physical_comment is not None and value == physical_comment


def suggestion_overwrites_existing_value(
    suggestion: SuggestionPatch,
    current: dict[str, Any] | None,
    *,
    physical_comment: str | None = None,
) -> bool:
    if not current:
        return False
    for key, proposed in suggestion.changes.items():
        existing = current.get(key)
        if _is_blank(key, existing, current, physical_comment):
            continue
        if existing != proposed:
            return True
    return False


def default_accept_for_suggestion(
    suggestion: SuggestionPatch,
    current: dict[str, Any] | None,
    *,
    physical_comment: str | None = None,
) -> bool:
    """建议的默认勾选。

    2026-08-23 产品决定：一律默认采用，包括 measure / identifier 这类
    high_impact 分类。理由是这个弹窗本身就是"逐条审核 AI 草稿"，按钮写着
    「确认并应用全部」—— 默认关掉等于要求用户把 AI 做的事再做一遍。
    high_impact 仍保留在建议上，表格里以「高影响，需重点核对」标记提示。

    唯一仍然默认不采用的是"会覆盖人工已填内容"：那不是 AI 的判断问题，
    是不该悄悄丢掉用户已经写下的东西。导入时的占位值（列名、kind="field"、
    create_* = False）不算人工内容，见 _is_blank。
    """

    if suggestion.source is SuggestionSource.DATABASE_CONSTRAINT:
        return True
    return not suggestion_overwrites_existing_value(
        suggestion, current, physical_comment=physical_comment
    )


def _current_record(
    revision: ModelingRevision, suggestion: SuggestionPatch
) -> dict[str, Any] | None:
    spec = revision.semantic_spec
    pool = {
        "model": spec.models,
        "field": spec.fields,
        "relation": spec.relations,
    }[suggestion.target_kind]
    found = next((item for item in pool if item.id == suggestion.target_id), None)
    return found.model_dump(mode="json") if found is not None else None


def physical_comments_for(
    models: Iterable[ModelSpec],
    fields: Iterable[FieldSpec],
    snapshot: SchemaSnapshot,
) -> dict[str, str]:
    """target_id → 数据库注释。只收非空注释；判定"description 是不是导入抄来的"用。"""

    tables = {(item.schema_name, item.name): item for item in snapshot.tables}
    fields_by_model: dict[str, list[FieldSpec]] = {}
    for field in fields:
        fields_by_model.setdefault(field.model_id, []).append(field)
    out: dict[str, str] = {}
    for model in models:
        table = tables.get((model.schema_name, model.table))
        if table is None:
            continue
        if table.comment:
            out[model.id] = table.comment
        comments = {column.name: column.comment for column in table.columns}
        for field in fields_by_model.get(model.id, ()):
            comment = comments.get(field.column)
            if comment:
                out[field.id] = comment
    return out


def default_decisions(
    revision: ModelingRevision,
    suggestions: tuple[SuggestionPatch, ...],
    *,
    physical_comments: Mapping[str, str] | None = None,
) -> tuple[SuggestionDecision, ...]:
    comments = physical_comments or {}
    return tuple(
        SuggestionDecision(
            suggestion_id=item.id,
            accept=default_accept_for_suggestion(
                item,
                _current_record(revision, item),
                physical_comment=comments.get(item.target_id),
            ),
        )
        for item in suggestions
    )
