from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from knowflow_analytics.contracts import FrozenModel, SemanticRelease
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.contracts import SchemaSnapshot, TableSnapshot


class SchemaDriftSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class SchemaDriftImpact(FrozenModel):
    resource_kind: Literal["model", "field", "relation", "dimension", "metric", "dataset", "term"]
    resource_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


class SchemaDriftChange(FrozenModel):
    change_type: Literal[
        "table_added",
        "table_removed",
        "column_added",
        "column_removed",
        "column_type_changed",
        "column_nullability_changed",
        "column_key_changed",
        "foreign_key_changed",
    ]
    schema_name: str
    table_name: str
    column_name: str | None = None
    before: Any = None
    after: Any = None
    severity: SchemaDriftSeverity
    message: str
    impacts: tuple[SchemaDriftImpact, ...] = ()


class SchemaDriftReport(FrozenModel):
    id: str
    project_id: str
    revision_id: str
    revision_etag: int = Field(ge=1)
    baseline_schema_hash: str
    current_schema_hash: str
    semantic_spec_hash: str
    content_hash: str
    ready: bool
    blocking_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    changes: tuple[SchemaDriftChange, ...]
    created_at: datetime


class SchemaDriftAnalyzer:
    """Compare a frozen snapshot with current PostgreSQL metadata.

    Model resources bind physical fields but do not version a full datasource
    snapshot, so drift needs its own report.  It never edits the model and only
    exposes the resources that require review.
    """

    def analyze(
        self,
        *,
        project_id: str,
        revision_id: str,
        revision_etag: int,
        baseline: SchemaSnapshot,
        current_tables: tuple[TableSnapshot, ...],
        available_table_keys: tuple[tuple[str, str], ...],
        semantic_spec: SemanticRelease,
    ) -> SchemaDriftReport:
        baseline_by_key = {(item.schema_name, item.name): item for item in baseline.tables}
        current_by_key = {(item.schema_name, item.name): item for item in current_tables}
        available = set(available_table_keys)
        changes: list[SchemaDriftChange] = []

        for schema_name, table_name in sorted(available - set(baseline_by_key)):
            changes.append(
                SchemaDriftChange(
                    change_type="table_added",
                    schema_name=schema_name,
                    table_name=table_name,
                    severity=SchemaDriftSeverity.INFO,
                    message="数据源新增表；不会自动进入当前项目",
                )
            )
        for key, baseline_table in sorted(baseline_by_key.items()):
            current_table = current_by_key.get(key)
            if current_table is None:
                changes.append(
                    self._change(
                        change_type="table_removed",
                        table=baseline_table,
                        severity=SchemaDriftSeverity.BLOCKING,
                        message="当前语义模型引用的物理表已不存在",
                        semantic_spec=semantic_spec,
                    )
                )
                continue
            changes.extend(
                self._compare_table(
                    baseline_table,
                    current_table,
                    semantic_spec=semantic_spec,
                )
            )

        ordered = tuple(
            sorted(
                changes,
                key=lambda item: (
                    item.schema_name,
                    item.table_name,
                    item.column_name or "",
                    item.change_type,
                ),
            )
        )
        current_hash = content_hash(
            {
                "available_table_keys": sorted(available),
                "tables": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        current_tables,
                        key=lambda value: (value.schema_name, value.name),
                    )
                ],
            }
        )
        payload = {
            "project_id": project_id,
            "revision_id": revision_id,
            "revision_etag": revision_etag,
            "baseline_schema_hash": baseline.content_hash,
            "current_schema_hash": current_hash,
            "semantic_spec_hash": semantic_spec.spec_hash,
            "changes": [item.model_dump(mode="json") for item in ordered],
        }
        digest = content_hash(payload)
        blocking = sum(item.severity is SchemaDriftSeverity.BLOCKING for item in ordered)
        warnings = sum(item.severity is SchemaDriftSeverity.WARNING for item in ordered)
        return SchemaDriftReport(
            id=f"schema_drift_{uuid.uuid4().hex}",
            project_id=project_id,
            revision_id=revision_id,
            revision_etag=revision_etag,
            baseline_schema_hash=baseline.content_hash,
            current_schema_hash=current_hash,
            semantic_spec_hash=semantic_spec.spec_hash,
            content_hash=digest,
            ready=blocking == 0,
            blocking_count=blocking,
            warning_count=warnings,
            changes=ordered,
            created_at=datetime.now(UTC),
        )

    def _compare_table(
        self,
        baseline: TableSnapshot,
        current: TableSnapshot,
        *,
        semantic_spec: SemanticRelease,
    ) -> list[SchemaDriftChange]:
        changes: list[SchemaDriftChange] = []
        baseline_columns = {item.name: item for item in baseline.columns}
        current_columns = {item.name: item for item in current.columns}
        for column_name in sorted(set(current_columns) - set(baseline_columns)):
            changes.append(
                self._change(
                    change_type="column_added",
                    table=baseline,
                    column_name=column_name,
                    after=current_columns[column_name].model_dump(mode="json"),
                    severity=SchemaDriftSeverity.WARNING,
                    message="物理表新增字段；需人工决定是否扩展语义模型",
                    semantic_spec=semantic_spec,
                )
            )
        for column_name in sorted(set(baseline_columns) - set(current_columns)):
            changes.append(
                self._change(
                    change_type="column_removed",
                    table=baseline,
                    column_name=column_name,
                    before=baseline_columns[column_name].model_dump(mode="json"),
                    severity=SchemaDriftSeverity.BLOCKING,
                    message="语义字段引用的物理列已不存在",
                    semantic_spec=semantic_spec,
                )
            )
        for column_name in sorted(set(baseline_columns) & set(current_columns)):
            before = baseline_columns[column_name]
            after = current_columns[column_name]
            if before.data_type.casefold() != after.data_type.casefold():
                changes.append(
                    self._change(
                        change_type="column_type_changed",
                        table=baseline,
                        column_name=column_name,
                        before=before.data_type,
                        after=after.data_type,
                        severity=SchemaDriftSeverity.BLOCKING,
                        message="物理字段类型已变化；原指标或过滤语义不可直接复用",
                        semantic_spec=semantic_spec,
                    )
                )
            if before.nullable != after.nullable:
                changes.append(
                    self._change(
                        change_type="column_nullability_changed",
                        table=baseline,
                        column_name=column_name,
                        before=before.nullable,
                        after=after.nullable,
                        severity=SchemaDriftSeverity.WARNING,
                        message="字段 NULL 约束已变化；需重新运行事实粒度与指标核对",
                        semantic_spec=semantic_spec,
                    )
                )
            if (before.primary_key, before.unique) != (
                after.primary_key,
                after.unique,
            ):
                changes.append(
                    self._change(
                        change_type="column_key_changed",
                        table=baseline,
                        column_name=column_name,
                        before=(before.primary_key, before.unique),
                        after=(after.primary_key, after.unique),
                        severity=SchemaDriftSeverity.BLOCKING,
                        message="主键或唯一约束已变化；事实粒度与关系基数需要重新确认",
                        semantic_spec=semantic_spec,
                    )
                )
        before_fk = {_foreign_key_key(item) for item in baseline.foreign_keys}
        after_fk = {_foreign_key_key(item) for item in current.foreign_keys}
        if before_fk != after_fk:
            changes.append(
                self._change(
                    change_type="foreign_key_changed",
                    table=baseline,
                    before=sorted(before_fk),
                    after=sorted(after_fk),
                    severity=SchemaDriftSeverity.BLOCKING,
                    message="数据库外键已变化；模型关系与 Join 路径需要重新确认",
                    semantic_spec=semantic_spec,
                )
            )
        return changes

    @staticmethod
    def _change(
        *,
        change_type,
        table: TableSnapshot,
        severity: SchemaDriftSeverity,
        message: str,
        semantic_spec: SemanticRelease,
        column_name: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> SchemaDriftChange:
        return SchemaDriftChange(
            change_type=change_type,
            schema_name=table.schema_name,
            table_name=table.name,
            column_name=column_name,
            before=before,
            after=after,
            severity=severity,
            message=message,
            impacts=_semantic_impacts(
                semantic_spec,
                schema_name=table.schema_name,
                table_name=table.name,
                column_name=column_name,
            ),
        )


def _foreign_key_key(item) -> tuple[Any, ...]:
    return (
        item.constrained_columns,
        item.referred_schema,
        item.referred_table,
        item.referred_columns,
    )


def _semantic_impacts(
    release: SemanticRelease,
    *,
    schema_name: str,
    table_name: str,
    column_name: str | None,
) -> tuple[SchemaDriftImpact, ...]:
    model_ids = {
        item.id
        for item in release.models
        if item.query_type == "table_query"
        and item.schema_name == schema_name
        and item.table == table_name
    }
    field_ids = {
        item.id
        for item in release.fields
        if item.model_id in model_ids and (column_name is None or item.column == column_name)
    }
    dimension_ids = {
        item.id
        for item in release.dimensions
        if item.model_id in model_ids and (column_name is None or item.field_id in field_ids)
    }
    metric_ids = {
        item.id
        for item in release.metrics
        if item.model_id in model_ids
        and (column_name is None or item.field_id in field_ids or item.field_id is None)
    }
    relation_ids = {
        item.id
        for item in release.relations
        if item.left_model_id in model_ids or item.right_model_id in model_ids
        if column_name is None
        or any(
            condition.left_field_id in field_ids or condition.right_field_id in field_ids
            for condition in item.conditions
        )
    }
    dataset_ids = {
        item.id
        for item in release.datasets
        if model_ids.intersection(item.model_ids)
        or dimension_ids.intersection(item.dimension_ids)
        or metric_ids.intersection(item.metric_ids)
    }
    term_ids = {
        item.id
        for item in release.terms
        if dimension_ids.intersection(item.dimension_ids)
        or metric_ids.intersection(item.metric_ids)
        or dataset_ids.intersection(item.dataset_ids)
    }
    resources = (
        *(("model", item) for item in model_ids),
        *(("field", item) for item in field_ids),
        *(("relation", item) for item in relation_ids),
        *(("dimension", item) for item in dimension_ids),
        *(("metric", item) for item in metric_ids),
        *(("dataset", item) for item in dataset_ids),
        *(("term", item) for item in term_ids),
    )
    return tuple(
        SchemaDriftImpact(
            resource_kind=kind,
            resource_id=resource_id,
            reason="该资源依赖发生漂移的物理表或字段",
        )
        for kind, resource_id in sorted(resources)
    )
