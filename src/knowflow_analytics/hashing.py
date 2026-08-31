from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from knowflow_analytics.contracts import SemanticRelease


def canonical_json(value: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: BaseModel | dict[str, Any] | list[Any] | tuple[Any, ...]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# 纯展示字段：改它们不会改变物理 SQL、扇出判定或数值口径，因此不该作废
# 需要真实扫库的质量证据。
#
# 只在 SemanticRelease 的一级 spec 元素上剥离，绝不递归：同名键在嵌套结构里
# 承载语义（DatasetTimeDefaultConfig.unit 是默认时间窗口天数、参与 WHERE 边界
# 计算），递归剥离会把它们一起删掉，让改窗口不作废证据。
#
# display_name 刻意不在此列：它参与精确维度值 grounding，影响数值口径。
#
# biz_name 同样不在此列，但理由与上游不同：S2SQL 的 FROM 与符号解析用的是
# 中文 name（serialize_s2sql 用 symbols.dataset.name，canonical_name 返回
# resolved.name），顶层 biz_name 不参与任何口径计算。它是可选的 ASCII 业务
# 标识，中文表名派生不出有信息量的值时会被主动跳过（_biz_name_is_degenerate），
# 因此在中文部署里普遍为空。留在非装饰集合里是保守选择：它是资源身份的一部分，
# 一旦被填上，改动它应当作废证据。
_COSMETIC_FIELDS = frozenset(
    {
        "name",
        "description",
        "aliases",
        "unit",
        "format",
        "sensitive_level",
        "classifications",
    }
)

# 这些顶层键是 spec 对象列表，其元素的一级展示字段可以剥离。
# modeling_catalog 是原始上游 DTO，整体保留。
_SPEC_COLLECTIONS = frozenset(
    {
        "models",
        "fields",
        "relations",
        "dimensions",
        "metrics",
        "datasets",
        "terms",
        "dimension_values",
    }
)


def _strip_cosmetic(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        # Reviewed context changes the immutable Release and natural-language
        # evaluation, but never changes joins, expressions or physical data
        # evidence. Avoid invalidating an expensive database quality scan.
        if key == "semantic_context":
            continue
        if key == "analysis_topic_routes" and isinstance(value, list):
            result[key] = [
                {k: v for k, v in item.items() if k != "ai_context"}
                if isinstance(item, dict)
                else item
                for item in value
            ]
            continue
        if key == "modeling_catalog" and isinstance(value, dict):
            catalog = dict(value)
            catalog.pop("semantic_context", None)
            catalog.pop("semanticContext", None)
            for routes_key in ("analysis_topic_routes", "analysisTopicRoutes"):
                routes = catalog.get(routes_key)
                if isinstance(routes, list):
                    catalog[routes_key] = [
                        {
                            route_key: route_value
                            for route_key, route_value in item.items()
                            if route_key not in {"ai_context", "aiContext"}
                        }
                        if isinstance(item, dict)
                        else item
                        for item in routes
                    ]
            result[key] = catalog
            continue
        if key not in _SPEC_COLLECTIONS or not isinstance(value, list):
            result[key] = value
            continue
        result[key] = [
            {k: v for k, v in item.items() if k not in _COSMETIC_FIELDS}
            if isinstance(item, dict)
            else item
            for item in value
        ]
    return result


def semantic_evidence_hash(release: SemanticRelease) -> str:
    """Hash only what real-data evidence actually depends on.

    The quality report was keyed on ``spec_hash``, which covers the entire
    release, so renaming one metric invalidated a three-minute full-table scan.
    This hash ignores presentation-only fields on the top-level spec collections
    so a cosmetic edit keeps that evidence valid, while any change to joins,
    cardinality, aggregation, expressions, default time windows or dataset scope
    still invalidates it.

    Only the quality report is bound to this hash today; the evaluation gate and
    the index-consistency check still use ``spec_hash`` — the conservative
    choice, since re-running those is cheaper than a full-table scan.
    """

    payload = release.model_dump(
        mode="json",
        exclude={"id", "spec_hash", "revision_id", "index_snapshot_id"},
    )
    return content_hash(_strip_cosmetic(payload))


def semantic_release_hash(release: SemanticRelease) -> str:
    payload = release.model_dump(
        mode="json",
        exclude={"id", "spec_hash", "revision_id", "index_snapshot_id"},
    )
    return content_hash(payload)
