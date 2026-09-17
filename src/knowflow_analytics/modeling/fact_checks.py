"""按对象缓存真实数据核对结果的键。

建模页要能对着一列、一条关系、一个指标当场查数，就得有缓存——每次点开编辑器都
去客户库扫全表不可接受。缓存的难点从来不是存，是**什么时候作废**。

这里不做「改完标灰」的比对，键本身就是失效规则：``subject_hash`` 是内容寻址的，
只哈希决定那条 SQL 的东西。对象一变，键就变，旧结果根本查不到，不可能被错误地
展示在改过的对象上。代价是重命名指标不会失效任何检查（它确实不改变数字），改主
标识会同时失效该模型的粒度检查和一切碰它的关系检查（它确实会改变结论）。

这是 ``semantic_evidence_hash`` 的思路下沉到单个对象：那个哈希绑一整版 Release，
对整版发布门是对的，用在单对象上会让每次保存把所有标记一起变灰。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from knowflow_analytics.contracts import FrozenModel, ModelSpec, SemanticRelease
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.quality import QualityStatus

# 指标核对的对象是「哪个数据集里的哪个指标」——同一个指标在不同数据集里
# 连接路径不同，数字也可能不同。
METRIC_SUBJECT_SEPARATOR = "::"

# 数据会漂移：键没变不代表数字还是昨天那个。超过这个时长仍然显示，但标注"可能已过时"。
FACT_CHECK_TTL_HOURS = 24

# 建模页点一下就得有反应。发布前那次完整跑允许 30 秒，这里不行——量不完就直说，
# 不要让人对着转圈等。
FACT_CHECK_STATEMENT_TIMEOUT_MS = 8_000


class FactCheckKind(StrEnum):
    GRAIN = "grain"
    RELATION = "relation"
    METRIC = "metric"
    ROWS = "rows"


class FactCheckRecord(FrozenModel):
    """一次核对的结果。``payload`` 是对应的 profile 原样序列化。

    ``computed_at`` 一并存下来，因为数据会漂移：键没变不代表数字还是昨天那个，
    界面要能说出「统计于 3 小时前」，读取方也才有依据决定要不要重算。
    """

    kind: FactCheckKind
    subject_id: str
    subject_hash: str
    status: QualityStatus
    payload: dict[str, Any] = {}
    computed_at: datetime


class FactCheckEntry(FrozenModel):
    """一个可核对对象的当前状态：键是什么、有没有量过、量的还算不算新。"""

    kind: FactCheckKind
    subject_id: str
    subject_hash: str
    result: FactCheckRecord | None = None
    expired: bool = False


class FactCheckSubjectError(AnalyticsError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="FACT_CHECK_SUBJECT_UNKNOWN", stage="MODELING")


def metric_subject_id(dataset_id: str, metric_id: str) -> str:
    return f"{dataset_id}{METRIC_SUBJECT_SEPARATOR}{metric_id}"


def split_metric_subject_id(subject_id: str) -> tuple[str, str]:
    dataset_id, separator, metric_id = subject_id.partition(METRIC_SUBJECT_SEPARATOR)
    if not separator or not dataset_id or not metric_id:
        raise FactCheckSubjectError(
            f"metric subject must be dataset{METRIC_SUBJECT_SEPARATOR}metric"
        )
    return dataset_id, metric_id


def fact_subject_hash(
    kind: FactCheckKind,
    subject_id: str,
    *,
    release: SemanticRelease,
    schema_snapshot_hash: str,
) -> str:
    """这次核对的内容指纹。同样的库、同样的对象定义，必然得到同一个键。"""

    return content_hash(
        {
            "kind": kind.value,
            "schema_snapshot_hash": schema_snapshot_hash,
            "subject": _fingerprint(kind, subject_id, release),
        }
    )


def _fingerprint(kind: FactCheckKind, subject_id: str, release: SemanticRelease) -> Any:
    if kind is FactCheckKind.METRIC:
        dataset_id, metric_id = split_metric_subject_id(subject_id)
        return _metric_fingerprint(dataset_id, metric_id, release)
    if kind is FactCheckKind.RELATION:
        return _relation_fingerprint(subject_id, release)
    model = _model(subject_id, release)
    if kind is FactCheckKind.ROWS:
        # 样例行只取决于来源，与谁是主标识无关。
        return _model_source_fingerprint(model, release)
    return {
        "model": _model_source_fingerprint(model, release),
        # 复合主标识调换顺序不改变量出来的数字，所以归一成有序集合；换一列才是新的键。
        "identifiers": sorted(
            item.column
            for item in release.fields
            if item.model_id == model.id and item.identifier_type == "primary"
        ),
    }


def _relation_fingerprint(relation_id: str, release: SemanticRelease) -> Any:
    relation = next((item for item in release.relations if item.id == relation_id), None)
    if relation is None:
        raise FactCheckSubjectError(f"relation {relation_id} is not in this revision")
    field_by_id = {item.id: item for item in release.fields}

    def column(field_id: str) -> str:
        field = field_by_id.get(field_id)
        if field is None:
            raise FactCheckSubjectError(f"relation {relation_id} references an unknown field")
        return field.column

    return {
        "left": _model_source_fingerprint(_model(relation.left_model_id, release), release),
        "right": _model_source_fingerprint(_model(relation.right_model_id, release), release),
        "conditions": [
            [column(item.left_field_id), column(item.right_field_id)]
            for item in relation.conditions
        ],
        # 声明基数不进 SQL，但它决定结论：声明 many_to_one 而实测一对多是拦截项。
        # join_type 刻意不在此列——核对语句两端都全取，改它不会改变任何数字。
        "declared_cardinality": relation.cardinality.value,
    }


def _metric_fingerprint(dataset_id: str, metric_id: str, release: SemanticRelease) -> Any:
    dataset = next((item for item in release.datasets if item.id == dataset_id), None)
    if dataset is None:
        raise FactCheckSubjectError(f"dataset {dataset_id} is not in this revision")
    metric = next((item for item in release.metrics if item.id == metric_id), None)
    if metric is None:
        raise FactCheckSubjectError(f"metric {metric_id} is not in this revision")
    if metric_id not in dataset.metric_ids:
        raise FactCheckSubjectError(f"metric {metric_id} is not open in dataset {dataset_id}")
    field = next((item for item in release.fields if item.id == metric.field_id), None)
    return {
        "model": _model_source_fingerprint(_model(metric.model_id, release), release),
        # 口径：怎么算、算哪一列、沿哪条时间轴、带不带自己的过滤。
        "aggregation": metric.aggregation.value if metric.aggregation else None,
        "column": field.column if field else None,
        "formula": metric.formula,
        "define_type": metric.define_type,
        "raw_filter_sql": metric.raw_filter_sql,
        "filters": [item.model_dump(mode="json") for item in metric.filters],
        "agg_time_dimension_id": metric.agg_time_dimension_id,
        "non_additive_dimension": (
            metric.non_additive_dimension.model_dump(mode="json")
            if metric.non_additive_dimension
            else None
        ),
        # 数据集的时间默认值会改变取样数字，名字与描述不会。
        "dataset": {
            "id": dataset.id,
            "default_time_dimension_id": dataset.default_time_dimension_id,
            "default_time_days": dataset.default_time_days,
            "detail_time_default": (
                dataset.detail_time_default.model_dump(mode="json")
                if dataset.detail_time_default
                else None
            ),
            "aggregate_time_default": (
                dataset.aggregate_time_default.model_dump(mode="json")
                if dataset.aggregate_time_default
                else None
            ),
            "timezone": dataset.timezone,
        },
    }


def _model(model_id: str, release: SemanticRelease) -> ModelSpec:
    model = next((item for item in release.models if item.id == model_id), None)
    if model is None:
        raise FactCheckSubjectError(f"model {model_id} is not in this revision")
    return model


def _model_source_fingerprint(model: ModelSpec, release: SemanticRelease) -> Any:
    """受治理来源的指纹，与 ``compile_governed_model_source`` 读的东西一一对应。

    固定过滤按**物理列**入指纹而不是字段 ID：字段改指到另一列时 ID 可以不变，
    而 WHERE 换了列，行集就变了。
    """

    field_by_id = {item.id: item for item in release.fields}
    return {
        "query_type": model.query_type,
        "schema_name": model.schema_name,
        "table": model.table,
        "sql_query": model.sql_query,
        "sql_variables": [dict(item) for item in model.sql_variables],
        "filters": [
            {
                "column": (
                    field_by_id[item.field_id].column if item.field_id in field_by_id else None
                ),
                "operator": item.operator.value,
                "value": item.value,
            }
            for item in model.filters
        ],
    }


def fact_check_subjects(
    release: SemanticRelease, *, schema_snapshot_hash: str
) -> tuple[tuple[FactCheckKind, str, str], ...]:
    """草稿里所有可以用数据核对的对象，连同它们此刻的内容键。

    样例行不在其列：它没有结论，只是给人看的，按需要才取。
    """

    subjects: list[tuple[FactCheckKind, str]] = [
        (FactCheckKind.GRAIN, model.id) for model in release.models
    ]
    subjects += [(FactCheckKind.RELATION, relation.id) for relation in release.relations]
    subjects += [
        (FactCheckKind.METRIC, metric_subject_id(dataset.id, metric_id))
        for dataset in release.datasets
        for metric_id in dataset.metric_ids
    ]
    return tuple(
        (
            kind,
            subject_id,
            fact_subject_hash(
                kind,
                subject_id,
                release=release,
                schema_snapshot_hash=schema_snapshot_hash,
            ),
        )
        for kind, subject_id in subjects
    )
