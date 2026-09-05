import json
import logging
import queue
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogError
from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionValueSpec,
    QueryRuleSpec,
    SemanticQuery,
    TermSpec,
)
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.evaluation.contracts import GoldenSuite
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetContract,
    DimensionContract,
    HierarchyContract,
    IdentifierContract,
    MeasureContract,
    MetricContract,
    ModelContract,
    ModelDimensionContract,
    ModelFieldContract,
    ModelRelationContract,
    SqlVariableContract,
)
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryPolicy,
    DimensionValueDecision,
    ModelingRunSource,
    SemanticAliasReview,
    SuggestionDecision,
)
from knowflow_analytics.modeling.deletion import ResourceKind
from knowflow_analytics.modeling.domain import DomainLifecycle
from knowflow_analytics.modeling.layout import GraphNodePosition, GraphViewport
from knowflow_analytics.modeling.product import DecisionChoice
from knowflow_analytics.modeling.quality import MetricPreviewDecision
from knowflow_analytics.query.contracts import (
    ClarificationQueryResponse,
    CompletedQueryResponse,
    FailedQueryResponse,
    QueryOptions,
    QueryRequest,
    QueryResponse,
    QueryRowFilter,
    QueryState,
    QueryTraceStep,
    StructuredQueryRequest,
)
from knowflow_analytics.query.diagnostics import QueryDiagnosticExport
from knowflow_analytics.query.service import apply_relative_time_window

LOGGER = logging.getLogger(__name__)
_MAX_ALIAS_REVIEW_CHARS = 1_000_000
_MAX_ALIAS_REVIEW_VALUES = 50_000


def _validate_catalog_resource_path_id(resource_id: str) -> str:
    if (
        resource_id in {".", ".."}
        or "/" in resource_id
        or "\\" in resource_id
        or any(ord(character) < 32 or ord(character) == 127 for character in resource_id)
    ):
        raise HTTPException(status_code=422, detail="catalog resource id is not path-safe")
    return resource_id


def _ordinary_query_projection(response: QueryResponse) -> dict[str, Any]:
    """Business-only wire shape for active-release Ask.

    The immutable full artifact is persisted before this API projection and is
    available through the separately authorized diagnostic export. Ordinary
    Ask never receives Scope, semantic IDs, parser evidence, or executable SQL.
    """

    payload: dict[str, Any] = {
        "query_id": response.query_id,
        "state": response.state.value,
        "release_id": response.release_id,
        "spec_hash": response.spec_hash,
        "index_snapshot_id": response.index_snapshot_id,
        "trace": [
            {
                "stage": item.stage.value,
                "status": item.status,
                "detail": (
                    {"code": item.detail["code"]}
                    if isinstance(item.detail.get("code"), str)
                    else {}
                ),
            }
            for item in response.trace
        ],
        "diagnostics": (
            {
                "category": response.diagnostics.category.value,
                "stage": response.diagnostics.stage,
                "severity": response.diagnostics.severity,
                "summary": response.diagnostics.summary,
                "recommendation": "",
                "user_hint": response.diagnostics.user_hint,
            }
            if response.diagnostics is not None
            else None
        ),
    }
    if isinstance(response, ClarificationQueryResponse):
        payload.update(
            {
                "question": response.question,
                "options": [item.model_dump(mode="json") for item in response.options],
            }
        )
        return payload
    if isinstance(response, FailedQueryResponse):
        payload["error"] = {
            "stage": response.error.stage,
            "code": response.error.code,
            "message": "当前问题未能安全执行，请换一种说法或联系建模管理员。",
            "retryable": response.error.retryable,
        }
        return payload
    assert isinstance(response, CompletedQueryResponse)
    column_labels = {
        **dict(
            zip(
                response.semantic_query.dimension_ids,
                response.interpretation.dimensions,
                strict=False,
            )
        ),
        **dict(
            zip(
                response.semantic_query.metric_ids,
                response.interpretation.metrics,
                strict=False,
            )
        ),
    }
    # 服务端按结果列逐位给出的展示名优先：textual 路径的结果列是 SQL 别名
    # （RATIO_OVER("净收入") AS "同比"），只按语义 ID 查会整列退化成「结果列 N」。
    served_labels = dict(zip(response.data.columns, response.column_labels, strict=False))
    columns = []
    seen: dict[str, int] = {}
    for index, column in enumerate(response.data.columns, start=1):
        label = column_labels.get(column) or served_labels.get(column) or f"结果列 {index}"
        seen[label] = seen.get(label, 0) + 1
        columns.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    grains = dict(zip(response.data.columns, response.column_grains, strict=False))
    grain_by_index = [grains.get(column) for column in response.data.columns]
    rows = [
        [_format_grain_value(value, grain_by_index[index]) for index, value in enumerate(row)]
        for row in response.data.rows
    ]
    payload.update(
        {
            "interpretation": {
                "query_type": response.interpretation.query_type.value,
                "metrics": response.interpretation.metrics,
                "dimensions": response.interpretation.dimensions,
                "filters": response.interpretation.filters,
                # 标记原文带语义 ID（query_rule:… / time:<维度 id>…），普通 wire 不出；
                # 系统补的时间窗以业务名单独投影在下面。
                "applied_defaults": (),
                "default_time_window": (
                    response.interpretation.default_time_window.model_dump(mode="json")
                    if response.interpretation.default_time_window is not None
                    else None
                ),
            },
            "data": {
                **response.data.model_copy(update={"rows": tuple(map(tuple, rows))}).model_dump(
                    mode="json"
                ),
                "columns": columns,
            },
            # 与 data.columns 逐位对齐；上游二次投影（BFF）据此透传而非重猜。
            "column_labels": columns,
            "visualization": _ordinary_visualization(response, columns),
            "resolved_by_llm": [item.model_dump(mode="json") for item in response.resolved_by_llm],
            "semantic_decisions": [
                item.model_dump(mode="json") for item in response.semantic_decisions
            ],
            # Signed follow-up cuts: opaque token + governed label only.
            "drilldown": [
                {
                    "token": item.token,
                    "kind": item.kind,
                    "action": item.action,
                    "label": item.label,
                }
                for item in response.drilldown
            ],
        }
    )
    return payload


_GRAIN_FORMATS = {
    "YEAR": "%Y",
    "MONTH": "%Y-%m",
    "DAY": "%Y-%m-%d",
    "WEEK": "%Y-%m-%d",
}


def _format_grain_value(value: Any, grain: str | None) -> Any:
    """把 DATE_TRUNC 的 timestamptz 收敛到分组粒度。

    按年分组的结果值仍是「2026-01-01T00:00:00+08:00」，展示成「2026」才是
    用户问的那个粒度。季度没有 strftime 记号，单独算。
    """

    if not grain or not isinstance(value, (datetime, date)):
        return value
    if grain == "QUARTER":
        return f"{value.year} Q{(value.month - 1) // 3 + 1}"
    pattern = _GRAIN_FORMATS.get(grain)
    return value.strftime(pattern) if pattern else value


def _ordinary_visualization(
    response: CompletedQueryResponse,
    projected_columns: list[str],
) -> dict[str, Any]:
    """Chart hint for ordinary Ask, expressed in projected column labels only.

    The full artifact keeps ``visualization`` keyed by semantic IDs; the
    ordinary wire must never carry those, so axes are re-expressed as the exact
    deduplicated business labels shipped in ``data.columns``.  An axis whose
    element does not appear in the result columns is dropped rather than leaked.
    """

    source = response.visualization if isinstance(response.visualization, dict) else {}
    label_by_source_column = dict(zip(response.data.columns, projected_columns, strict=False))
    chart_type = source.get("type")
    x_id = source.get("x")
    raw_y = source.get("y")
    y_ids = raw_y if isinstance(raw_y, (list, tuple)) else ()

    def aligned(key: str) -> dict[str, Any]:
        raw = source.get(key)
        return dict(zip(y_ids, raw, strict=False)) if isinstance(raw, (list, tuple)) else {}

    y_units_by_id = aligned("y_units")
    y_formats_by_id = aligned("y_formats")
    kept = [item for item in y_ids if isinstance(item, str) and item in label_by_source_column]
    series_id = source.get("series")
    return {
        "type": chart_type if isinstance(chart_type, str) else "table",
        "x": label_by_source_column.get(x_id) if isinstance(x_id, str) else None,
        "series": (label_by_source_column.get(series_id) if isinstance(series_id, str) else None),
        "x_time": bool(source.get("x_time")),
        "x_grain": source.get("x_grain") if isinstance(source.get("x_grain"), str) else None,
        "y": [label_by_source_column[item] for item in kept],
        # 与 y 逐位对齐的展示单位；非字符串一律置 None。
        "y_units": [
            unit if isinstance(unit := y_units_by_id.get(item), str) else None for item in kept
        ],
        # 与 y 逐位对齐的数值形态；未知一律按常规数值。
        "y_formats": [
            fmt
            if (fmt := y_formats_by_id.get(item)) in {"number", "percent", "delta"}
            else "number"
            for item in kept
        ],
    }


def _release_ask_context(release) -> dict[str, Any]:
    """Ask 消费者的最小 Release 投影：治理名与值字典，无建模产物与物理信息。

    带上 ``id`` 与 ``model_id``：列级权限的白名单以受治理元素 ID 为键，按实体
    （``model_id``）推导；只给名字的投影让调用方物理上无法过滤，联想词表与值
    字典就只能整份铺开。这些是**建模侧标识**，不是普通问数 wire——问数响应的
    零泄漏合同约束的是 `/v1/analytics/query` 的投影，不是本上下文接口。
    """

    return {
        "release_id": release.id,
        "spec_hash": release.spec_hash,
        "datasets": [
            {
                "id": item.id,
                "metric_ids": list(item.metric_ids),
                "dimension_ids": list(item.dimension_ids),
            }
            for item in release.datasets
        ],
        # 实体清单：列级权限按实体授权，配置界面要按名字选，所以名字得随着 ID 出来。
        "models": [{"id": item.id, "name": item.name} for item in release.models],
        "metrics": [
            {
                "id": item.id,
                "model_id": item.model_id,
                "name": item.name,
                "aliases": list(item.aliases),
            }
            for item in release.metrics
        ],
        "dimensions": [
            {
                "id": item.id,
                "model_id": item.model_id,
                "name": item.name,
                "aliases": list(item.aliases),
            }
            for item in release.dimensions
        ],
        "terms": [
            {
                "id": item.id,
                "name": item.name,
                "aliases": list(item.aliases),
                # 术语绑定到受治理成员；成员不可见时该术语也不该出现在联想里。
                "metric_ids": list(item.metric_ids),
                "dimension_ids": list(item.dimension_ids),
            }
            for item in release.terms
        ],
        "dimension_values": [
            {
                "dimension_id": item.dimension_id,
                "value": item.value,
                "display_name": item.display_name,
            }
            for item in release.dimension_values
        ],
    }


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDataSourceRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=256)
    engine: str = Field(min_length=1, max_length=32)
    # 连接串带凭据。只进不出：任何响应里都不会有它。
    dsn: str = Field(min_length=1, max_length=2048)


class UpdateDataSourceRequest(_RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    dsn: str | None = Field(default=None, min_length=1, max_length=2048)


class TestDataSourceRequest(_RequestModel):
    engine: str = Field(min_length=1, max_length=32)
    dsn: str = Field(min_length=1, max_length=2048)


class BindDataSourceRequest(_RequestModel):
    data_source_id: str = Field(min_length=1, max_length=128)


class CreateProjectRequest(_RequestModel):
    name: str = Field(min_length=1, max_length=256)
    project_id: str | None = Field(default=None, min_length=1, max_length=128)


class UpdateDomainGovernanceRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    parent_project_id: str | None = Field(default=None, min_length=1, max_length=128)
    classifications: tuple[str, ...] = Field(default=(), max_length=100)
    lifecycle: DomainLifecycle


class UpdateModelGraphLayoutRequest(_RequestModel):
    expected_etag: int = Field(ge=0)
    positions: tuple[GraphNodePosition, ...] = Field(max_length=1_000)
    viewport: GraphViewport = Field(default_factory=GraphViewport)


class _SchemaScopeRequest(_RequestModel):
    schemas: tuple[str, ...] = Field(min_length=1, max_length=20)
    selected_tables: dict[str, tuple[str, ...]] | None = None
    include_views: bool = False

    @field_validator("selected_tables")
    @classmethod
    def explicit_table_scope_must_not_be_empty(
        cls,
        value: dict[str, tuple[str, ...]] | None,
    ) -> dict[str, tuple[str, ...]] | None:
        if value is not None and not any(value.values()):
            raise ValueError("selected table scope must not be empty")
        return value


class CreateSchemaSnapshotRequest(_SchemaScopeRequest):
    pass


class CreateRevisionRequest(_RequestModel):
    schema_snapshot_id: str = Field(min_length=1, max_length=128)


class AddTableModelRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    schema_name: str = Field(min_length=1, max_length=256)
    table_name: str = Field(min_length=1, max_length=256)


class AddSqlModelRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    model_id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    biz_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4_000)
    sql_query: str = Field(min_length=1, max_length=100_000)
    sql_variables: tuple[SqlVariableContract, ...] = Field(default=(), max_length=100)


class ExtendRevisionTablesRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    selected_tables: dict[str, tuple[str, ...]] = Field(min_length=1, max_length=20)
    include_views: bool = False

    @field_validator("selected_tables")
    @classmethod
    def selected_tables_must_be_bounded(
        cls,
        value: dict[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]]:
        if any(not schema.strip() or len(schema) > 256 for schema in value):
            raise ValueError("schema names are invalid")
        tables = [table for values in value.values() for table in values]
        if not tables or len(tables) > 500:
            raise ValueError("selected tables are invalid")
        if any(not table.strip() or len(table) > 256 for table in tables):
            raise ValueError("table names are invalid")
        if len({(schema, table) for schema, values in value.items() for table in values}) != len(
            tables
        ):
            raise ValueError("selected tables must be unique")
        return value


class ProposeAnalysisTopicsRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)


class UpsertAnalysisTopicRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    dataset: DatasetSpec
    route: AnalysisTopicRouteSpec


class UpsertCatalogTermRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    term: TermSpec


class UpsertCatalogDimensionValueRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    dimension_value: DimensionValueSpec


class UpsertCatalogModelRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    model: ModelContract


class UpsertCatalogIdentifierRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    identifier: IdentifierContract


class UpsertCatalogModelDimensionRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    dimension: ModelDimensionContract


class UpsertCatalogMeasureRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    measure: MeasureContract


class UpsertCatalogModelFieldRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    field: ModelFieldContract


class UpsertCatalogRelationRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    relation: ModelRelationContract


class UpsertCatalogDimensionRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    dimension: DimensionContract


class UpsertCatalogHierarchyRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    hierarchy: HierarchyContract


class UpsertCatalogMetricRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    metric: MetricContract


class UpsertCatalogDatasetRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    data_set: DataSetContract


class UpsertQueryRuleRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    query_rule: QueryRuleSpec


class PreviewCatalogDeletionRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)


class DeleteCatalogResourceRequest(PreviewCatalogDeletionRequest):
    expected_impact_hash: str = Field(min_length=1, max_length=128)
    confirmation: Literal["delete"]


class CreateSuggestionRunRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    manifest_hash: str | None = Field(default=None, max_length=128)
    source: ModelingRunSource = ModelingRunSource.API
    source_task_id: str | None = Field(default=None, min_length=1, max_length=128)


class ApplySuggestionRunRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    decisions: tuple[SuggestionDecision, ...] = Field(max_length=10_000)


class CreateModelingProposalRequest(CreateSuggestionRunRequest):
    pass


class SuggestAliasesRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    resource_type: Literal["dimension", "metric"]
    model_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    biz_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4_000)
    existing_aliases: tuple[str, ...] = Field(default=(), max_length=100)


class SaveModelingProposalRequest(_RequestModel):
    expected_proposal_etag: int = Field(ge=1)
    expected_proposal_hash: str = Field(min_length=1, max_length=128)
    decisions: tuple[SuggestionDecision, ...] = Field(max_length=10_000)
    alias_reviews: tuple[SemanticAliasReview, ...] = Field(max_length=20_000)

    @model_validator(mode="after")
    def alias_review_payload_is_bounded(self):
        alias_count = sum(len(item.aliases) for item in self.alias_reviews)
        character_count = sum(
            len(item.resource_id)
            + len(item.display_name or "")
            + sum(len(alias) for alias in item.aliases)
            for item in self.alias_reviews
        )
        if alias_count > _MAX_ALIAS_REVIEW_VALUES or character_count > _MAX_ALIAS_REVIEW_CHARS:
            raise ValueError("AI alias review payload is too large")
        return self


class ApplyModelingProposalRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    expected_proposal_etag: int = Field(ge=1)
    expected_proposal_hash: str = Field(min_length=1, max_length=128)
    confirmation: Literal["apply"]


class ApplyDecisionsRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    decisions: tuple[SuggestionDecision, ...] = Field(max_length=10_000)


class CreateModelingPlanRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    suggestion_run_id: str | None = Field(default=None, min_length=1, max_length=128)


class ApplyModelingPlanRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    choices: tuple[DecisionChoice, ...] = Field(max_length=10_000)


class CreateModelingQualityReportRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)


class ReviewModelingQualityReportRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    expected_content_hash: str = Field(min_length=1, max_length=128)
    decisions: tuple[MetricPreviewDecision, ...] = Field(max_length=2_000)


class GenerateDimensionDictionaryPreviewRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    dimension_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    policies: tuple[DimensionDictionaryPolicy, ...] | None = Field(
        default=None, min_length=1, max_length=100
    )


class ApplyDimensionDictionaryPreviewRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    confirmation: Literal["apply"]
    decisions: tuple[DimensionValueDecision, ...] = Field(max_length=10_000)


class RefreshDueDimensionDictionariesRequest(_RequestModel):
    limit: int = Field(default=20, ge=1, le=100)


class EvaluateRequest(_RequestModel):
    expected_etag: int | None = Field(default=None, ge=1)
    schema_snapshot_hash: str | None = Field(default=None, min_length=1, max_length=128)
    suite: GoldenSuite
    required_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)


class SaveGoldenSuiteRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    suite: GoldenSuite


class PublishRequest(_RequestModel):
    confirmation: Literal["publish"]
    expected_etag: int | None = Field(default=None, ge=1)
    schema_snapshot_hash: str | None = Field(default=None, min_length=1, max_length=128)


class PreviewRevisionQueryRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4_000)
    dataset_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    fixed_now: datetime | None = None
    selected_candidate_id: str | None = Field(default=None, min_length=1, max_length=1_024)
    expected_release_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_spec_hash: str | None = Field(default=None, min_length=1, max_length=128)
    expected_index_snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_diagnostics: bool = False
    include_debug_sql: bool = False

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value

    @field_validator("dataset_ids")
    @classmethod
    def dataset_scope_must_be_explicit(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("dataset scope is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("dataset scope must be unique")
        return normalized

    @field_validator("fixed_now")
    @classmethod
    def fixed_now_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("fixed_now must include a timezone")
        return value

    @model_validator(mode="after")
    def candidate_must_be_bound_to_staged_version(self) -> "PreviewRevisionQueryRequest":
        expected = (
            self.expected_release_id,
            self.expected_spec_hash,
            self.expected_index_snapshot_id,
        )
        if self.selected_candidate_id is not None and any(item is None for item in expected):
            raise ValueError("candidate selection requires its staged release version")
        if self.selected_candidate_id is None and any(item is not None for item in expected):
            raise ValueError("staged release version requires a candidate selection")
        return self


class PreviewRevisionStructuredQueryRequest(_RequestModel):
    expected_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    semantic_query: SemanticQuery
    fixed_now: datetime | None = None
    include_debug_sql: bool = False

    @field_validator("fixed_now")
    @classmethod
    def fixed_now_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("fixed_now must include a timezone")
        return value


@dataclass(frozen=True)
class ServiceContext:
    actor_id: str
    project_id: str | None
    permission_scope_hash: str


class _RateLimiter:
    def __init__(self, *, regular_limit: int, expensive_limit: int) -> None:
        self._limits = {"regular": regular_limit, "expensive": expensive_limit}
        self._requests: dict[tuple[str, str], deque[float]] = {}
        self._last_sweep = time.monotonic()
        self._lock = threading.Lock()

    def check(self, *, actor_id: str, bucket: str) -> None:
        now = time.monotonic()
        key = (actor_id, bucket)
        with self._lock:
            if now - self._last_sweep >= 60:
                for existing_key, existing_queue in tuple(self._requests.items()):
                    while existing_queue and now - existing_queue[0] >= 60:
                        existing_queue.popleft()
                    if not existing_queue:
                        del self._requests[existing_key]
                self._last_sweep = now
            queue = self._requests.setdefault(key, deque())
            while queue and now - queue[0] >= 60:
                queue.popleft()
            if len(queue) >= self._limits[bucket]:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="request rate limit exceeded",
                )
            queue.append(now)


# 上传文件的大小上限。整表要读进内存做类型推断，没有上限时一个几百兆的文件会拖垮
# 服务，而用户只会看到超时。
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


async def _upload_bytes(request: Request) -> bytes:
    data = await request.body()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "UPLOAD_TOO_LARGE",
                "message": f"文件超过 {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB。",
            },
        )
    return data


def _upload_plan(raw: str) -> tuple[tuple[str, str], ...]:
    """解析导入计划。

    计划走 query 而不是请求体：请求体已经被文件占了。宿主转发把 query 限在 4096 字符，
    这里再收紧一档并给出人话——超了是"选太多张"，不是"请求非法"。
    """

    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UPLOAD_PLAN_INVALID", "message": "导入计划格式不对。"},
        ) from exc
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UPLOAD_PLAN_INVALID", "message": "导入计划格式不对。"},
        )
    plan: list[tuple[str, str]] = []
    for item in items:
        sheet = str(item.get("sheet", ""))[:256]
        table = str(item.get("table", ""))[:63]
        plan.append((sheet, table))
    return tuple(plan)


class AnswerFeedbackRequest(BaseModel):
    """用户对一次成功的回答点了赞或踩。

    点踩必须带原因——裸的一个「踩」建模者拿到也不知道改什么；点赞不需要。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)
    liked: bool = False
    reason: Literal["", "scope", "metric", "value", "understanding", "other"] = ""
    comment: str = Field(default="", max_length=1_000)
    release_id: str = Field(min_length=1, max_length=128)
    spec_hash: str = Field(min_length=1, max_length=128)
    index_snapshot_id: str = Field(min_length=1, max_length=128)
    dataset_ids: list[str] = Field(default_factory=list, max_length=64)
    # 用来取回这一轮的自足问句（下钻和追问的原话单独看没有意义）。
    query_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def dislike_requires_a_reason(self) -> "AnswerFeedbackRequest":
        if not self.liked and not self.reason:
            raise ValueError("a disliked answer must carry a reason")
        return self


class FeedbackStatusRequest(BaseModel):
    """把反馈列表上的一行标成已处理/忽略。

    标识的是"哪一个说法"而不是"哪几条记录"：同一个说法的记录散在多页，按 id 改
    只能改掉当前页看得见的那几条。这四个字段就是列表的聚合口径。
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=32)
    phrase: str = Field(default="", max_length=256)
    resolution: str = Field(default="", max_length=256)
    question: str = Field(default="", max_length=4_000)
    status: Literal["open", "resolved", "ignored"]


def create_api(
    *,
    application: AnalyticsApplication,
    service_secret: str,
    allow_debug_sql: bool = False,
    request_body_limit_bytes: int = 1_048_576,
    requests_per_minute: int = 120,
    expensive_requests_per_minute: int = 60,
    # 助手配置面板要显示"留空意味着什么"。不告诉用户当前全局是开是关，他面对一个空
    # 输入框根本不知道系统正在用什么值——那比不给这个功能更糟，因为界面会让人以为
    # 自己知道。
    query_defaults: dict[str, object] | None = None,
) -> FastAPI:
    if len(service_secret) < 32:
        raise ValueError("analytics service secret must contain at least 32 characters")
    app = FastAPI(
        title="KnowFlow Analytics Internal API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    limiter = _RateLimiter(
        regular_limit=requests_per_minute,
        expensive_limit=expensive_requests_per_minute,
    )

    @app.middleware("http")
    async def security_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ):
        content_length = request.headers.get("content-length")
        if request.method in {"POST", "PUT", "PATCH"} and content_length is None:
            return JSONResponse(status_code=411, content={"detail": "content length required"})
        if content_length is not None:
            try:
                too_large = int(content_length) > request_body_limit_bytes
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "invalid content length"})
            if too_large:
                return JSONResponse(status_code=413, content={"detail": "request body too large"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def context(
        x_knowflow_service_token: Annotated[str | None, Header()] = None,
        x_knowflow_actor_id: Annotated[str | None, Header()] = None,
        x_knowflow_project_id: Annotated[str | None, Header()] = None,
        x_knowflow_permission_scope_hash: Annotated[str | None, Header()] = None,
    ) -> ServiceContext:
        if x_knowflow_service_token is None or not secrets.compare_digest(
            x_knowflow_service_token, service_secret
        ):
            raise HTTPException(status_code=401, detail="invalid service credentials")
        if not x_knowflow_actor_id or not x_knowflow_permission_scope_hash:
            raise HTTPException(status_code=400, detail="missing signed request context")
        signed_context_values = (
            x_knowflow_actor_id,
            x_knowflow_permission_scope_hash,
            *((x_knowflow_project_id,) if x_knowflow_project_id is not None else ()),
        )
        if any(value != value.strip() for value in signed_context_values):
            raise HTTPException(status_code=400, detail="invalid signed request context")
        if len(x_knowflow_actor_id) > 128 or len(x_knowflow_permission_scope_hash) > 128:
            raise HTTPException(status_code=400, detail="invalid signed request context")
        if x_knowflow_project_id is not None and len(x_knowflow_project_id) > 128:
            raise HTTPException(status_code=400, detail="invalid project context")
        limiter.check(actor_id=x_knowflow_actor_id, bucket="regular")
        return ServiceContext(
            actor_id=x_knowflow_actor_id,
            project_id=x_knowflow_project_id,
            permission_scope_hash=x_knowflow_permission_scope_hash,
        )

    Context = Annotated[ServiceContext, Depends(context)]

    def require_project(path_project_id: str, request_context: ServiceContext) -> None:
        if request_context.project_id != path_project_id:
            raise HTTPException(status_code=403, detail="project scope mismatch")

    def expensive(request_context: ServiceContext) -> None:
        limiter.check(actor_id=request_context.actor_id, bucket="expensive")

    def owned_revision(project_id: str, revision_id: str):
        revision = application.get_revision(revision_id)
        if revision.project_id != project_id:
            raise HTTPException(status_code=404, detail="revision not found")
        return revision

    def owned_run(project_id: str, revision_id: str, run_id: str):
        run = application.get_modeling_run(run_id)
        if run.project_id != project_id or run.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="modeling run not found")
        return run

    def owned_proposal(project_id: str, revision_id: str, proposal_id: str):
        proposal = application.get_modeling_proposal(proposal_id)
        if proposal.project_id != project_id or proposal.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="modeling proposal not found")
        return proposal

    def owned_plan(project_id: str, revision_id: str, plan_id: str):
        plan = application.get_modeling_plan(plan_id)
        if plan.project_id != project_id or plan.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="modeling plan not found")
        return plan

    def owned_dictionary_preview(project_id: str, revision_id: str, preview_id: str):
        preview = application.get_dimension_dictionary_preview(preview_id)
        if preview.project_id != project_id or preview.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="dimension dictionary preview not found")
        return preview

    def owned_quality_report(project_id: str, revision_id: str, report_id: str):
        report = application.get_modeling_quality_report(report_id)
        if report.project_id != project_id or report.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="modeling quality report not found")
        return report

    def owned_schema_drift_report(project_id: str, revision_id: str, report_id: str):
        report = application.get_schema_drift_report(report_id)
        if report.project_id != project_id or report.revision_id != revision_id:
            raise HTTPException(status_code=404, detail="schema drift report not found")
        return report

    def require_default_datasource(datasource_id: str) -> None:
        if datasource_id != "default":
            raise HTTPException(status_code=404, detail="datasource not found")

    def catalog_resource_kind(collection: str) -> ResourceKind:
        try:
            return {
                "models": ResourceKind.MODEL,
                "relations": ResourceKind.RELATION,
                "dimensions": ResourceKind.DIMENSION,
                "metrics": ResourceKind.METRIC,
                "datasets": ResourceKind.DATASET,
                "terms": ResourceKind.TERM,
                "hierarchies": ResourceKind.HIERARCHY,
                "query-rules": ResourceKind.QUERY_RULE,
            }[collection]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="catalog resource not found") from exc

    @app.exception_handler(AnalyticsError)
    async def analytics_error_handler(_request: Request, exc: AnalyticsError):
        return JSONResponse(
            status_code=409,
            content={"error": {"stage": exc.stage, "code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(_request: Request, exc: Exception):
        LOGGER.exception("Unhandled analytics API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 数据源 -------------------------------------------------------------
    #
    # 这些端点需要服务令牌，浏览器直达不了（core 直通道的路径正则要求
    # ``projects/{prj_id}/...``）。授权由宿主 BFF 判定，与项目列表同一模式。

    @app.get("/v1/analytics/data-sources")
    def list_data_sources(request_context: Context):  # noqa: ARG001
        return {"items": [item.model_dump(mode="json") for item in application.list_data_sources()]}

    @app.post("/v1/analytics/data-sources")
    def create_data_source(payload: CreateDataSourceRequest, request_context: Context):
        expensive(request_context)
        record = application.create_data_source(
            name=payload.name, engine=payload.engine, dsn=payload.dsn
        )
        return record.model_dump(mode="json")

    @app.post("/v1/analytics/uploads:inspect")
    async def inspect_upload(request: Request, request_context: Context):
        """看一眼上传的表格：每张 sheet 会建成什么样、自动改了什么。

        不落库。用户要先看到改动再决定，否则他会在建模页对着自己没写过的列名。

        文件走请求体、参数走 query——multipart 要多一颗 ``python-multipart`` 依赖，
        而这个依赖表是刻意精简的，为几个短字符串加它不划算。
        """

        expensive(request_context)
        return application.inspect_upload(data=await _upload_bytes(request))

    @app.post("/v1/analytics/uploads:commit")
    async def commit_upload(
        request: Request,
        request_context: Context,
        plan: Annotated[str, Query(min_length=2, max_length=3_500)],
    ):
        """按计划建表灌数。``plan`` 是 ``[{"sheet": ..., "table": ...}]`` 的 JSON。

        一次可以导多张。某一张失败不回滚已经成功的那几张——用户要的是"哪些进来了、
        哪些没有、为什么"，把前面的撤掉毫无道理。每张各自带回结果或原因。
        """

        expensive(request_context)
        return application.commit_upload(data=await _upload_bytes(request), plan=_upload_plan(plan))

    @app.get("/v1/analytics/query-defaults")
    def read_query_defaults(request_context: Context):
        """这个部署的全局默认。只读，给助手配置面板显示"留空 = 什么"。"""

        del request_context
        return dict(query_defaults or {})

    @app.get("/v1/analytics/uploads")
    def list_uploads(request_context: Context):
        expensive(request_context)
        return application.list_uploads()

    @app.delete("/v1/analytics/uploads/{table}")
    def delete_upload(table: str, request_context: Context):
        """删掉一张上传的表。被已发布模型用着的不让删。"""

        expensive(request_context)
        return application.delete_upload(table)

    @app.post("/v1/analytics/uploads:load")
    async def load_upload(
        request: Request,
        request_context: Context,
        sheet: Annotated[str, Query(min_length=1, max_length=256)],
        table: Annotated[str, Query(min_length=1, max_length=63)],
        mode: Annotated[Literal["append", "replace"], Query()] = "append",
    ):
        """往已有的表里追加或整表替换。结构对不上就明确说差在哪。"""

        expensive(request_context)
        return application.load_upload_rows(
            data=await _upload_bytes(request), sheet=sheet, table=table, mode=mode
        )

    @app.post("/v1/analytics/data-sources:test")
    def test_data_source(payload: TestDataSourceRequest, request_context: Context):
        """连一下但不保存。让用户在填写时就知道信息对不对。"""

        expensive(request_context)
        application.test_data_source(engine=payload.engine, dsn=payload.dsn)
        return {"ok": True}

    @app.put("/v1/analytics/data-sources/{data_source_id}")
    def update_data_source(
        data_source_id: str, payload: UpdateDataSourceRequest, request_context: Context
    ):
        expensive(request_context)
        record = application.update_data_source(
            data_source_id=data_source_id, name=payload.name, dsn=payload.dsn
        )
        if record is None:
            raise HTTPException(status_code=404, detail="data source not found")
        return record.model_dump(mode="json")

    @app.delete("/v1/analytics/data-sources/{data_source_id}")
    def delete_data_source(data_source_id: str, request_context: Context):
        expensive(request_context)
        if not application.delete_data_source(data_source_id):
            raise HTTPException(status_code=404, detail="data source not found")
        return {"deleted": True}

    @app.get("/v1/analytics/projects/{project_id}/data-source")
    def get_project_data_source(project_id: str, request_context: Context):
        require_project(project_id, request_context)
        record = application.get_project_data_source(project_id)
        return {"data_source": record.model_dump(mode="json") if record else None}

    @app.put("/v1/analytics/projects/{project_id}/data-source")
    def bind_project_data_source(
        project_id: str, payload: BindDataSourceRequest, request_context: Context
    ):
        require_project(project_id, request_context)
        expensive(request_context)
        application.bind_project_data_source(
            project_id=project_id, data_source_id=payload.data_source_id
        )
        return {"bound": True}

    @app.get("/v1/analytics/projects")
    def list_projects(
        request_context: Context,
        id_prefix: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ):
        """退出项目后此前从 UI 上不可达，只靠 sessionStorage。

        ``id_prefix`` 省略时返回全部项目：调用方（宿主 BFF）据此做资源授权过滤，
        与知识库列表"先列候选、再 batch_check"同一模式。核心不认识授权，只负责
        把候选交出去；本端点需要服务令牌，浏览器无法直达（core 直通道的路径正则
        要求 ``projects/{prj_id}/...``，列表路径匹配不上）。
        """

        return {
            "items": [
                item.model_dump(mode="json")
                for item in application.list_projects(id_prefix=id_prefix, limit=limit)
            ]
        }

    @app.post("/v1/analytics/projects")
    def create_project(payload: CreateProjectRequest, request_context: Context):
        expensive(request_context)
        if (
            request_context.project_id is not None
            and payload.project_id != request_context.project_id
        ):
            raise HTTPException(status_code=403, detail="project scope mismatch")
        return application.create_project(name=payload.name, project_id=payload.project_id)

    @app.delete("/v1/analytics/projects/{project_id}")
    def delete_project(project_id: str, request_context: Context):
        """删掉项目及其名下的一切。

        不做软删：留一份"已删除"的项目意味着它的语义模型、确认记忆、诊断产物都还
        在库里，而那些东西装着真实的维度取值和物理 SQL。用户点删除就是要它消失。
        """

        require_project(project_id, request_context)
        expensive(request_context)
        if not application.delete_project(project_id):
            raise HTTPException(status_code=404, detail="project not found")
        return {"deleted": True}

    @app.get("/v1/analytics/projects/{project_id}/domain-governance")
    def get_domain_governance(project_id: str, request_context: Context):
        require_project(project_id, request_context)
        return application.get_domain_governance(project_id)

    @app.put("/v1/analytics/projects/{project_id}/domain-governance")
    def update_domain_governance(
        project_id: str,
        payload: UpdateDomainGovernanceRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return application.update_domain_governance(
            project_id=project_id,
            expected_etag=payload.expected_etag,
            parent_project_id=payload.parent_project_id,
            classifications=payload.classifications,
            lifecycle=payload.lifecycle,
            updated_by=request_context.actor_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/datasources/{datasource_id}/schemas")
    def list_schemas(
        project_id: str,
        datasource_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        require_default_datasource(datasource_id)
        return {"items": application.list_datasource_schemas(project_id=project_id)}

    @app.get("/v1/analytics/projects/{project_id}/datasources/{datasource_id}/tables")
    def list_tables(
        project_id: str,
        datasource_id: str,
        schema_name: str,
        request_context: Context,
        include_views: bool = False,
    ):
        require_project(project_id, request_context)
        require_default_datasource(datasource_id)
        return {
            "items": application.list_datasource_tables(
                project_id=project_id,
                schema_name=schema_name,
                include_views=include_views,
            )
        }

    @app.get("/v1/analytics/projects/{project_id}/datasources/{datasource_id}/tables/{table_name}")
    def describe_table(
        project_id: str,
        datasource_id: str,
        table_name: str,
        schema_name: str,
        request_context: Context,
        include_views: bool = False,
    ):
        require_project(project_id, request_context)
        require_default_datasource(datasource_id)
        return application.describe_datasource_table(
            project_id=project_id,
            schema_name=schema_name,
            table_name=table_name,
            include_views=include_views,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/datasources/{datasource_id}/scope-recommendations"
    )
    def get_scope_recommendations(
        project_id: str,
        datasource_id: str,
        schema_name: str,
        request_context: Context,
        include_views: bool = False,
    ):
        require_project(project_id, request_context)
        require_default_datasource(datasource_id)
        expensive(request_context)
        return application.get_scope_recommendations(
            project_id=project_id,
            datasource_id=datasource_id,
            schema_name=schema_name,
            include_views=include_views,
        )

    @app.get("/v1/analytics/projects/{project_id}/modeling-summary")
    def get_modeling_summary(project_id: str, request_context: Context):
        require_project(project_id, request_context)
        return application.get_modeling_summary(project_id=project_id)

    @app.post("/v1/analytics/projects/{project_id}/schema-snapshots")
    def create_schema_snapshot(
        project_id: str,
        payload: CreateSchemaSnapshotRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        expensive(request_context)
        return application.create_schema_snapshot(
            project_id=project_id,
            schemas=payload.schemas,
            selected_tables=payload.selected_tables,
            include_views=payload.include_views,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions")
    def create_revision(
        project_id: str,
        payload: CreateRevisionRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return application.create_empty_revision(
            project_id=project_id,
            schema_snapshot_id=payload.schema_snapshot_id,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/models:from-table")
    def add_table_model(
        project_id: str,
        revision_id: str,
        payload: AddTableModelRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.add_table_model(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            schema_name=payload.schema_name,
            table_name=payload.table_name,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/models:from-sql")
    def add_sql_model(
        project_id: str,
        revision_id: str,
        payload: AddSqlModelRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.add_sql_model(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            model_id=payload.model_id,
            name=payload.name,
            biz_name=payload.biz_name,
            description=payload.description,
            sql_query=payload.sql_query,
            sql_variables=payload.sql_variables,
        )

    @app.get("/v1/analytics/projects/{project_id}/releases")
    def list_releases(
        project_id: str,
        request_context: Context,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        require_project(project_id, request_context)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in application.list_releases(project_id, limit=limit)
            ]
        }

    @app.post("/v1/analytics/projects/{project_id}/releases/{release_id}:activate")
    def activate_release(project_id: str, release_id: str, request_context: Context):
        """把线上问数切到本项目的某个已发布版本。

        取代了原先无参数的 `releases:rollback`——它只会往更早走一步，且没有回头路：
        回滚一次后线上停在最早那版，更新的版本仍列在历史里却再也切不回去。
        """

        require_project(project_id, request_context)
        return {
            "active_release_id": application.activate_release(
                project_id=project_id, release_id=release_id
            )
        }

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}:derive")
    def derive_candidate_revision(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.derive_candidate_revision(revision_id=revision_id)

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/tables:extend")
    def extend_revision_tables(
        project_id: str,
        revision_id: str,
        payload: ExtendRevisionTablesRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.extend_revision_tables(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            selected_tables=payload.selected_tables,
            include_views=payload.include_views,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/analysis-topic-proposals:generate"
    )
    def propose_analysis_topics(
        project_id: str,
        revision_id: str,
        payload: ProposeAnalysisTopicsRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.propose_analysis_topics(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/analysis-topics/{dataset_id}"
    )
    def upsert_analysis_topic(
        project_id: str,
        revision_id: str,
        dataset_id: str,
        payload: UpsertAnalysisTopicRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.dataset.id != dataset_id or payload.route.dataset_id != dataset_id:
            raise HTTPException(status_code=422, detail="analysis topic id mismatch")
        return application.upsert_analysis_topic(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            dataset=payload.dataset,
            route=payload.route,
        )

    @app.put("/v1/analytics/projects/{project_id}/revisions/{revision_id}/catalog/terms/{term_id}")
    def upsert_term(
        project_id: str,
        revision_id: str,
        term_id: str,
        payload: UpsertCatalogTermRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.term.id != term_id:
            raise HTTPException(status_code=422, detail="term id mismatch")
        return application.upsert_term(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            term=payload.term,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/dimension-values/{dimension_value_id}"
    )
    def upsert_dimension_value(
        project_id: str,
        revision_id: str,
        dimension_value_id: str,
        payload: UpsertCatalogDimensionValueRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.dimension_value.id != dimension_value_id:
            raise HTTPException(status_code=422, detail="dimension value id mismatch")
        return application.upsert_dimension_value(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            dimension_value=payload.dimension_value,
        )

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}")
    def get_revision(project_id: str, revision_id: str, request_context: Context):
        require_project(project_id, request_context)
        return owned_revision(project_id, revision_id)

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/model-graph-layout")
    def get_model_graph_layout(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.get_model_graph_layout(
            project_id=project_id,
            revision_id=revision_id,
        )

    @app.put("/v1/analytics/projects/{project_id}/revisions/{revision_id}/model-graph-layout")
    def update_model_graph_layout(
        project_id: str,
        revision_id: str,
        payload: UpdateModelGraphLayoutRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.update_model_graph_layout(
            project_id=project_id,
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            positions=payload.positions,
            viewport=payload.viewport,
            updated_by=request_context.actor_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/catalog")
    def get_modeling_catalog(project_id: str, revision_id: str, request_context: Context):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.get_semantic_catalog(revision_id)

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/catalog/models/{model_id}"
    )
    def upsert_catalog_model(
        project_id: str,
        revision_id: str,
        model_id: str,
        payload: UpsertCatalogModelRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.model.id != model_id:
            raise HTTPException(status_code=422, detail="model id mismatch")
        return application.upsert_catalog_model(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            model=payload.model,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/models/{model_id}/identifiers/{field_name}"
    )
    def upsert_catalog_identifier(
        project_id: str,
        revision_id: str,
        model_id: str,
        field_name: str,
        payload: UpsertCatalogIdentifierRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.identifier.biz_name != field_name:
            raise HTTPException(status_code=422, detail="identifier field mismatch")
        return application.upsert_catalog_identifier(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            identifier=payload.identifier,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/models/{model_id}/dimensions/{field_name}"
    )
    def upsert_catalog_model_dimension(
        project_id: str,
        revision_id: str,
        model_id: str,
        field_name: str,
        payload: UpsertCatalogModelDimensionRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.dimension.biz_name != field_name:
            raise HTTPException(status_code=422, detail="dimension field mismatch")
        return application.upsert_catalog_model_dimension(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            dimension=payload.dimension,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/models/{model_id}/measures/{field_name}"
    )
    def upsert_catalog_measure(
        project_id: str,
        revision_id: str,
        model_id: str,
        field_name: str,
        payload: UpsertCatalogMeasureRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.measure.biz_name != field_name:
            raise HTTPException(status_code=422, detail="measure field mismatch")
        return application.upsert_catalog_measure(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            measure=payload.measure,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/models/{model_id}/fields/{field_name}"
    )
    def upsert_catalog_model_field(
        project_id: str,
        revision_id: str,
        model_id: str,
        field_name: str,
        payload: UpsertCatalogModelFieldRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.field.field_name != field_name:
            raise HTTPException(status_code=422, detail="model field mismatch")
        return application.upsert_catalog_model_field(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            field=payload.field,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/relations/{relation_id}"
    )
    def upsert_catalog_relation(
        project_id: str,
        revision_id: str,
        relation_id: str,
        payload: UpsertCatalogRelationRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.relation.id != relation_id:
            raise HTTPException(status_code=422, detail="relation id mismatch")
        return application.upsert_catalog_relation(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            relation=payload.relation,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/dimensions/{dimension_id}"
    )
    def upsert_catalog_dimension(
        project_id: str,
        revision_id: str,
        dimension_id: str,
        payload: UpsertCatalogDimensionRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.dimension.id != dimension_id:
            raise HTTPException(status_code=422, detail="dimension id mismatch")
        return application.upsert_catalog_dimension(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            dimension=payload.dimension,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/hierarchies/{hierarchy_id}"
    )
    def upsert_catalog_hierarchy(
        project_id: str,
        revision_id: str,
        hierarchy_id: str,
        payload: UpsertCatalogHierarchyRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.hierarchy.id != hierarchy_id:
            raise HTTPException(status_code=422, detail="hierarchy id mismatch")
        return application.upsert_catalog_hierarchy(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            hierarchy=payload.hierarchy,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/catalog/metrics/{metric_id}"
    )
    def upsert_catalog_metric(
        project_id: str,
        revision_id: str,
        metric_id: str,
        payload: UpsertCatalogMetricRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.metric.id != metric_id:
            raise HTTPException(status_code=422, detail="metric id mismatch")
        return application.upsert_catalog_metric(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            metric=payload.metric,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/catalog/datasets/{dataset_id}"
    )
    def upsert_catalog_dataset(
        project_id: str,
        revision_id: str,
        dataset_id: str,
        payload: UpsertCatalogDatasetRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.data_set.id != dataset_id:
            raise HTTPException(status_code=422, detail="dataset id mismatch")
        return application.upsert_catalog_dataset(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            data_set=payload.data_set,
        )

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/query-rules/{query_rule_id}"
    )
    def upsert_query_rule(
        project_id: str,
        revision_id: str,
        query_rule_id: str,
        payload: UpsertQueryRuleRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.query_rule.id != query_rule_id:
            raise HTTPException(status_code=422, detail="query rule id mismatch")
        return application.upsert_query_rule(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            query_rule=payload.query_rule,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/{resource_collection}/{resource_id}/deletion-impact"
    )
    def preview_catalog_deletion(
        project_id: str,
        revision_id: str,
        resource_collection: str,
        resource_id: str,
        payload: PreviewCatalogDeletionRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        resource_id = _validate_catalog_resource_path_id(resource_id)
        return application.preview_catalog_deletion(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            resource_kind=catalog_resource_kind(resource_collection),
            resource_id=resource_id,
        )

    @app.delete(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/catalog/{resource_collection}/{resource_id}"
    )
    def delete_catalog_resource(
        project_id: str,
        revision_id: str,
        resource_collection: str,
        resource_id: str,
        payload: DeleteCatalogResourceRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        resource_id = _validate_catalog_resource_path_id(resource_id)
        return application.delete_catalog_resource(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            resource_kind=catalog_resource_kind(resource_collection),
            resource_id=resource_id,
            expected_impact_hash=payload.expected_impact_hash,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/suggestion-runs")
    def create_suggestion_run(
        project_id: str,
        revision_id: str,
        payload: CreateSuggestionRunRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.create_ai_suggestion_run(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            manifest_hash=payload.manifest_hash,
            source=payload.source,
            source_task_id=payload.source_task_id,
            # RAGFlow tenant ids are user ids, so the signed-in actor is the
            # tenant whose model configuration must serve this request.
            tenant_id=request_context.actor_id,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/alias-suggestions")
    def suggest_aliases(
        project_id: str,
        revision_id: str,
        payload: SuggestAliasesRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.suggest_resource_aliases(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            resource_type=payload.resource_type,
            model_id=payload.model_id,
            name=payload.name,
            biz_name=payload.biz_name,
            description=payload.description,
            existing_aliases=payload.existing_aliases,
            # RAGFlow tenant ids are user ids, so the signed-in actor is the
            # tenant whose model configuration must serve this request.
            tenant_id=request_context.actor_id,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/modeling-jobs")
    def start_modeling_job(
        project_id: str,
        revision_id: str,
        payload: CreateModelingProposalRequest,
        request_context: Context,
    ):
        """异步版 modeling-proposals：立即返回 job，进度逐表落盘，客户端断开不影响执行。"""

        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.start_ai_modeling_job(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            manifest_hash=payload.manifest_hash,
            source=payload.source,
            source_task_id=payload.source_task_id,
            created_by=request_context.actor_id,
            tenant_id=request_context.actor_id,
        )

    def owned_job(project_id: str, job_id: str):
        # 不存在和不属于本项目都是 404，不泄露别的项目有没有这个 id。
        try:
            job = application.get_modeling_job(job_id)
        except CatalogError as exc:
            raise HTTPException(status_code=404, detail="modeling job was not found") from exc
        if job.project_id != project_id:
            raise HTTPException(status_code=404, detail="modeling job was not found")
        return job

    @app.get("/v1/analytics/projects/{project_id}/modeling-jobs/{job_id}")
    def get_modeling_job(project_id: str, job_id: str, request_context: Context):
        require_project(project_id, request_context)
        return owned_job(project_id, job_id)

    @app.post("/v1/analytics/projects/{project_id}/modeling-jobs/{job_id}:cancel")
    def cancel_modeling_job(project_id: str, job_id: str, request_context: Context):
        require_project(project_id, request_context)
        owned_job(project_id, job_id)
        return application.cancel_modeling_job(job_id)

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/modeling-proposals")
    def create_modeling_proposal(
        project_id: str,
        revision_id: str,
        payload: CreateModelingProposalRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.create_ai_modeling_proposal(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            manifest_hash=payload.manifest_hash,
            source=payload.source,
            source_task_id=payload.source_task_id,
            created_by=request_context.actor_id,
            # RAGFlow tenant ids are user ids, so the signed-in actor is the
            # tenant whose model configuration must serve this request.
            tenant_id=request_context.actor_id,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/modeling-proposals/{proposal_id}"
    )
    def get_modeling_proposal(
        project_id: str,
        revision_id: str,
        proposal_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return owned_proposal(project_id, revision_id, proposal_id)

    @app.put(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/modeling-proposals/{proposal_id}"
    )
    def save_modeling_proposal(
        project_id: str,
        revision_id: str,
        proposal_id: str,
        payload: SaveModelingProposalRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_proposal(project_id, revision_id, proposal_id)
        return application.save_ai_modeling_proposal(
            revision_id=revision_id,
            proposal_id=proposal_id,
            expected_proposal_etag=payload.expected_proposal_etag,
            expected_proposal_hash=payload.expected_proposal_hash,
            decisions=payload.decisions,
            alias_reviews=payload.alias_reviews,
            saved_by=request_context.actor_id,
            # RAGFlow tenant ids are user ids, so the signed-in actor is the
            # tenant whose model configuration must serve this request.
            tenant_id=request_context.actor_id,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/modeling-proposals/{proposal_id}:apply"
    )
    def apply_modeling_proposal(
        project_id: str,
        revision_id: str,
        proposal_id: str,
        payload: ApplyModelingProposalRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_proposal(project_id, revision_id, proposal_id)
        return application.apply_ai_modeling_proposal(
            revision_id=revision_id,
            proposal_id=proposal_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            expected_proposal_etag=payload.expected_proposal_etag,
            expected_proposal_hash=payload.expected_proposal_hash,
            reviewed_by=request_context.actor_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/suggestion-runs/{run_id}")
    def get_suggestion_run(
        project_id: str,
        revision_id: str,
        run_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return owned_run(project_id, revision_id, run_id)

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/suggestion-runs/{run_id}:apply"
    )
    def apply_suggestion_run(
        project_id: str,
        revision_id: str,
        run_id: str,
        payload: ApplySuggestionRunRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_run(project_id, revision_id, run_id)
        return application.apply_ai_suggestion_run(
            revision_id=revision_id,
            run_id=run_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            decisions=payload.decisions,
            reviewed_by=request_context.actor_id,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/decisions")
    def apply_decisions(
        project_id: str,
        revision_id: str,
        payload: ApplyDecisionsRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.apply_modeling_decisions(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            decisions=payload.decisions,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/modeling-plans")
    def create_modeling_plan(
        project_id: str,
        revision_id: str,
        payload: CreateModelingPlanRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.create_modeling_plan(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            suggestion_run_id=payload.suggestion_run_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/modeling-plans/{plan_id}")
    def get_modeling_plan(
        project_id: str,
        revision_id: str,
        plan_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return owned_plan(project_id, revision_id, plan_id)

    @app.get(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/modeling-plans/{plan_id}/decisions"
    )
    def get_modeling_decision_queue(
        project_id: str,
        revision_id: str,
        plan_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return owned_plan(project_id, revision_id, plan_id).queue

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/modeling-plans/{plan_id}/decisions:apply"
    )
    def apply_modeling_plan(
        project_id: str,
        revision_id: str,
        plan_id: str,
        payload: ApplyModelingPlanRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_plan(project_id, revision_id, plan_id)
        return application.apply_modeling_plan(
            revision_id=revision_id,
            plan_id=plan_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            choices=payload.choices,
            reviewed_by=request_context.actor_id,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/dimension-dictionary/previews"
    )
    def generate_dimension_dictionary_preview(
        project_id: str,
        revision_id: str,
        payload: GenerateDimensionDictionaryPreviewRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.generate_dimension_dictionary_preview(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            dimension_ids=payload.dimension_ids,
            policies=payload.policies,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/dimension-dictionary/previews/{preview_id}"
    )
    def get_dimension_dictionary_preview(
        project_id: str,
        revision_id: str,
        preview_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return owned_dictionary_preview(project_id, revision_id, preview_id)

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/dimension-dictionary/previews/{preview_id}/apply"
    )
    def apply_dimension_dictionary_preview(
        project_id: str,
        revision_id: str,
        preview_id: str,
        payload: ApplyDimensionDictionaryPreviewRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_dictionary_preview(project_id, revision_id, preview_id)
        return application.apply_dimension_dictionary_preview(
            preview_id=preview_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
            decisions=payload.decisions,
            reviewed_by=request_context.actor_id,
        )

    @app.post("/v1/analytics/projects/{project_id}/dimension-dictionary:refresh-due")
    def refresh_due_dimension_dictionaries(
        project_id: str,
        payload: RefreshDueDimensionDictionariesRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        expensive(request_context)
        return {
            "items": application.run_due_dimension_dictionary_refreshes(
                project_id=project_id,
                limit=payload.limit,
            )
        }

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/validate")
    def validate(project_id: str, revision_id: str, request_context: Context):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.validate_revision(revision_id)

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/diagnostics")
    def revision_diagnostics(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return application.get_revision_diagnostics(revision_id)

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/schema-drift-reports")
    def create_schema_drift_report(
        project_id: str,
        revision_id: str,
        payload: CreateModelingQualityReportRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.create_schema_drift_report(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/schema-drift-reports/{report_id}"
    )
    def get_schema_drift_report(
        project_id: str,
        revision_id: str,
        report_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return owned_schema_drift_report(project_id, revision_id, report_id)

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/quality-reports")
    def create_quality_report(
        project_id: str,
        revision_id: str,
        payload: CreateModelingQualityReportRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.create_modeling_quality_report(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            schema_snapshot_hash=payload.schema_snapshot_hash,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/quality-reports/{report_id}"
    )
    def get_quality_report(
        project_id: str,
        revision_id: str,
        report_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return owned_quality_report(project_id, revision_id, report_id)

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/evaluations:latest")
    def get_current_evaluation(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        report = application.get_current_evaluation_report(revision_id)
        return {"report": report}

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/quality-reports:latest")
    def get_current_quality_report(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        """取当前语义内容对应的质量报告。

        没有它前端就不知道报告存在过:报告只存在卡片的局部 state 里,刷新即丢,
        发布按钮因此永远读不到质量状态,只能凭 revision.state 点亮——而后端会用
        同一份证据拒绝发布。
        """

        require_project(project_id, request_context)
        report = application.get_current_modeling_quality_report(revision_id)
        if report is None:
            return {"report": None}
        if report.project_id != project_id or report.revision_id != revision_id:
            return {"report": None}
        return {"report": report}

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}"
        "/quality-reports/{report_id}:review"
    )
    def review_quality_report(
        project_id: str,
        revision_id: str,
        report_id: str,
        payload: ReviewModelingQualityReportRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        owned_quality_report(project_id, revision_id, report_id)
        return application.review_modeling_quality_report(
            revision_id=revision_id,
            report_id=report_id,
            expected_etag=payload.expected_etag,
            expected_content_hash=payload.expected_content_hash,
            decisions=payload.decisions,
            reviewed_by=request_context.actor_id,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/query-preview",
        response_model=QueryResponse,
    )
    def preview_revision_query(
        project_id: str,
        revision_id: str,
        payload: PreviewRevisionQueryRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        query_request = QueryRequest(
            project_id=project_id,
            question=payload.question,
            dataset_ids=payload.dataset_ids,
            selected_candidate_id=payload.selected_candidate_id,
            expected_release_id=payload.expected_release_id,
            expected_spec_hash=payload.expected_spec_hash,
            expected_index_snapshot_id=payload.expected_index_snapshot_id,
            conversation_id=payload.conversation_id,
            include_debug_sql=payload.include_debug_sql and allow_debug_sql,
            include_diagnostics=payload.include_diagnostics,
        )
        return application.preview_revision_query(
            revision_id=revision_id,
            request=query_request,
            expected_etag=payload.expected_etag,
            expected_schema_snapshot_hash=payload.schema_snapshot_hash,
            now=payload.fixed_now,
            actor_id=request_context.actor_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )

    @app.post(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/structured-query-preview",
        response_model=QueryResponse,
    )
    def preview_revision_structured_query(
        project_id: str,
        revision_id: str,
        payload: PreviewRevisionStructuredQueryRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.preview_revision_structured_query(
            revision_id=revision_id,
            project_id=project_id,
            semantic_query=payload.semantic_query,
            expected_etag=payload.expected_etag,
            expected_schema_snapshot_hash=payload.schema_snapshot_hash,
            now=payload.fixed_now,
            include_debug_sql=payload.include_debug_sql and allow_debug_sql,
            actor_id=request_context.actor_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/evaluate")
    def evaluate(
        project_id: str,
        revision_id: str,
        payload: EvaluateRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.evaluate_revision(
            revision_id=revision_id,
            suite=payload.suite,
            required_accuracy=payload.required_accuracy,
            expected_etag=payload.expected_etag,
            expected_schema_snapshot_hash=payload.schema_snapshot_hash,
            saved_by=request_context.actor_id,
            tenant_id=request_context.actor_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/query-failures")
    def list_query_failures(
        project_id: str,
        request_context: Context,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        status: Annotated[
            Literal["open", "resolved", "ignored", "archived", "all"], Query()
        ] = "open",
        exclude_kinds: Annotated[list[str] | None, Query()] = None,
    ):
        """问数反馈：系统听不懂什么。

        默认只给待处理的那一页。此前是"最近 100 条、不分页、无状态"——处理过的消不掉，
        第 101 条以后也看不到，页面很快变成一堆没人敢动的东西。
        """

        require_project(project_id, request_context)
        return application.list_query_failures(
            project_id,
            limit=limit,
            offset=offset,
            status=None if status == "all" else status,
            exclude_kinds=tuple(exclude_kinds or ()),
        )

    @app.post("/v1/analytics/projects/{project_id}/query-failures:answer-feedback")
    def record_answer_feedback(
        project_id: str,
        payload: AnswerFeedbackRequest,
        request_context: Context,
    ):
        """点赞点踩进同一张待处理列表。

        查询成功、治理关全绿、数字也出来了——这类静默错答没有任何系统信号，用户
        点的这一下是唯一的入口；赞则是一条被人确认过的问答，评测集要的正是它。
        两者都和「系统听不懂」进同一个工作队列，由建模者处理。
        """

        require_project(project_id, request_context)
        application.record_answer_feedback(
            project_id=project_id,
            actor_id=request_context.actor_id,
            question=payload.question,
            liked=payload.liked,
            reason=payload.reason,
            comment=payload.comment,
            release_id=payload.release_id,
            spec_hash=payload.spec_hash,
            index_snapshot_id=payload.index_snapshot_id,
            dataset_ids=tuple(payload.dataset_ids),
            query_id=payload.query_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )
        return {"recorded": True}

    @app.post("/v1/analytics/projects/{project_id}/query-failures:status")
    def update_query_failure_status(
        project_id: str,
        payload: FeedbackStatusRequest,
        request_context: Context,
    ):
        """把反馈列表上的一行标成已处理/忽略。没有删除——处理过的收起来，不是抹掉。"""

        require_project(project_id, request_context)
        expensive(request_context)
        return application.update_query_failure_status(
            project_id,
            kind=payload.kind,
            phrase=payload.phrase,
            resolution=payload.resolution,
            question=payload.question,
            status=payload.status,
            actor_id=request_context.actor_id,
        )

    @app.get("/v1/analytics/projects/{project_id}/query-card")
    def query_card_definition(
        project_id: str,
        request_context: Context,
        query_id: Annotated[str, Query(min_length=1, max_length=128)],
    ):
        """一次回答的可重跑定义，供调用方固定成概览卡片。

        不进普通查询响应：那份投影不出语义 ID 是已评审合同。这里是明确索取，
        且只发生在「钉卡片」这一个动作上。
        """

        require_project(project_id, request_context)
        return application.query_card_definition(
            project_id=project_id,
            query_id=query_id,
            actor_id=request_context.actor_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )

    @app.get(
        "/v1/analytics/projects/{project_id}/query-diagnostics/export",
        response_model=QueryDiagnosticExport,
    )
    def export_query_diagnostic(
        project_id: str,
        request_context: Context,
        query_id: Annotated[str, Query(min_length=1, max_length=128)],
    ):
        not_found = HTTPException(
            status_code=404,
            detail={
                "code": "QUERY_DIAGNOSTIC_NOT_FOUND",
                "message": "query diagnostic was not found",
            },
        )
        # This endpoint intentionally differs from ordinary project resources:
        # cross-project and cross-actor probes must be indistinguishable from an
        # expired or unknown query id.
        if request_context.project_id != project_id or not query_id or len(query_id) > 128:
            raise not_found
        expensive(request_context)
        try:
            return application.export_query_diagnostic(
                project_id=project_id,
                query_id=query_id,
                actor_id=request_context.actor_id,
                permission_scope_hash=request_context.permission_scope_hash,
                allow_debug_sql=allow_debug_sql,
            )
        except CatalogError as exc:
            if exc.code == "QUERY_DIAGNOSTIC_NOT_FOUND":
                raise not_found from exc
            raise

    @app.get("/v1/analytics/projects/{project_id}/revisions/{revision_id}/golden-suites")
    def list_golden_suites(
        project_id: str,
        revision_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        # 前端在这里第一次需要"发布要多少条"：进度条、提示文案、发布按钮都读它。
        # 此前前端硬编码 30，运维调低 env 后 UI 仍卡在 30。
        return {
            "items": application.list_golden_suites(revision_id),
            "publish_gate": application.publish_gate(),
        }

    @app.put("/v1/analytics/projects/{project_id}/revisions/{revision_id}/golden-suites/{suite_id}")
    def save_golden_suite(
        project_id: str,
        revision_id: str,
        suite_id: str,
        payload: SaveGoldenSuiteRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        if payload.suite.id != suite_id or payload.suite.project_id != project_id:
            raise HTTPException(status_code=400, detail="golden suite scope is inconsistent")
        return application.save_golden_suite(
            revision_id=revision_id,
            expected_etag=payload.expected_etag,
            expected_schema_snapshot_hash=payload.schema_snapshot_hash,
            suite=payload.suite,
            saved_by=request_context.actor_id,
        )

    @app.delete(
        "/v1/analytics/projects/{project_id}/revisions/{revision_id}/golden-suites/{suite_id}"
    )
    def delete_golden_suite(
        project_id: str,
        revision_id: str,
        suite_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        return {
            "deleted": application.delete_golden_suite(
                revision_id=revision_id,
                suite_id=suite_id,
            )
        }

    @app.post("/v1/analytics/projects/{project_id}/revisions/{revision_id}/publish")
    def publish(
        project_id: str,
        revision_id: str,
        _payload: PublishRequest,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        owned_revision(project_id, revision_id)
        expensive(request_context)
        return application.publish_revision(
            revision_id,
            expected_etag=_payload.expected_etag,
            expected_schema_snapshot_hash=_payload.schema_snapshot_hash,
        )

    @app.get("/v1/analytics/projects/{project_id}/releases/{release_id}")
    def get_release(
        project_id: str,
        release_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        published = application.get_release(release_id)
        if published.release.project_id != project_id:
            raise HTTPException(status_code=404, detail="release not found")
        return published

    @app.get("/v1/analytics/projects/{project_id}/releases/{release_id}/ask-context")
    def get_release_ask_context(
        project_id: str,
        release_id: str,
        request_context: Context,
    ):
        """问数消费者的轻量投影：dataset 范围 + 业务说法词表 + 值字典。

        完整 Release 携带 modeling_catalog 等建模产物，问数入口按 release
        拉一次就是 MB 级；这里只投影提问所需的治理名字面。
        """

        require_project(project_id, request_context)
        published = application.get_release(release_id)
        if published.release.project_id != project_id:
            raise HTTPException(status_code=404, detail="release not found")
        return _release_ask_context(published.release)

    class ReleaseStructuredQueryRequest(_RequestModel):
        """Release 级结构化查询：语义 ID 直达 Corrector→Translator→Guard→Executor。"""

        project_id: str = Field(min_length=1, max_length=128)
        semantic_query: SemanticQuery
        query_id: str | None = Field(default=None, min_length=1, max_length=128)
        include_debug_sql: bool = False
        # 行列级权限与自然语言入口同源。这条是"集成方入口"，绕过 Mapper 与 LLM
        # 直达 Translator——不接这两个字段，任何集成方都能拿全量数据，等于给行列级
        # 权限开了一扇后门。内部下钻路径走的是同一个 query_structured，早已带上。
        allowed_element_ids: tuple[str, ...] | None = Field(default=None, max_length=5_000)
        row_filters: tuple[QueryRowFilter, ...] | None = Field(default=None, max_length=100)
        # 助手级返回行数设置：报表卡片与下钻都要和首轮一致。
        options: QueryOptions = Field(default_factory=QueryOptions)
        # 滚动时间窗：概览卡片要"每次打开跟着今天走"。
        #
        # 为什么必须由调用方显式给：语义查询里的时间过滤是**绝对下界**——解析阶段
        # 拿着 now 把「最近 30 天」算成了具体日期，与「8 月 3 日以来」在结构上完全
        # 一样，事后分辨不出。猜错的代价是卡片安静地显示旧窗口，不报错不空白，
        # 比报错更危险。所以钉卡片时问一次，之后按这个窗口重算。
        time_window_dimension_id: str | None = Field(default=None, min_length=1, max_length=128)
        time_window_days: int | None = Field(default=None, ge=1, le=3_650)

    @app.post("/v1/analytics/structured-query", response_model=QueryResponse)
    def structured_query(payload: ReleaseStructuredQueryRequest, request_context: Context):
        """受治理结构化查询，绑定 Active Release；集成方入口，返回完整响应。"""

        require_project(payload.project_id, request_context)
        expensive(request_context)
        semantic_query = payload.semantic_query
        if payload.time_window_dimension_id is not None:
            semantic_query = apply_relative_time_window(
                semantic_query,
                payload.time_window_dimension_id,
                payload.time_window_days,
            )
        return application.structured_query(
            StructuredQueryRequest(
                project_id=payload.project_id,
                semantic_query=semantic_query,
                query_id=payload.query_id,
                include_debug_sql=payload.include_debug_sql and allow_debug_sql,
                allowed_element_ids=payload.allowed_element_ids,
                row_filters=payload.row_filters,
                options=payload.options,
            ),
            actor_id=request_context.actor_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )

    class DrilldownRequest(_RequestModel):
        project_id: str = Field(min_length=1, max_length=128)
        query_id: str = Field(min_length=1, max_length=128)
        token: str = Field(min_length=1, max_length=1_024)
        # refilter 续跑携带的业务值 literal（与自然语言问句中的词同等地位）。
        value: str | None = Field(default=None, min_length=1, max_length=512)
        # 下钻的行列级权限与首轮同源、每次请求重算：token 的 TTL 内授权可能已被
        # 撤销，从 token 恢复权限等于让旧令牌继续放行。
        allowed_element_ids: tuple[str, ...] | None = Field(default=None, max_length=5_000)
        row_filters: tuple[QueryRowFilter, ...] | None = Field(default=None, max_length=100)
        # 助手级返回行数设置：报表卡片与下钻都要和首轮一致。
        options: QueryOptions = Field(default_factory=QueryOptions)

    @app.post("/v1/analytics/query:drilldown")
    def drilldown_query(payload: DrilldownRequest, request_context: Context):
        """普通问数的下钻续跑：签名 token + 持久化 artifact 恢复语义，业务投影返回。"""

        require_project(payload.project_id, request_context)
        expensive(request_context)
        return _ordinary_query_projection(
            application.drilldown_query(
                project_id=payload.project_id,
                query_id=payload.query_id,
                token=payload.token,
                actor_id=request_context.actor_id,
                permission_scope_hash=request_context.permission_scope_hash,
                value=payload.value,
                allowed_element_ids=payload.allowed_element_ids,
                row_filters=payload.row_filters,
                options=payload.options,
            )
        )

    def _stage_event(step: QueryTraceStep) -> dict[str, Any]:
        """阶段事件的普通 wire 投影：中性阶段标识、状态，以及「理解问题」完成时认出的
        成员（只有类型与业务名）。detail 永远不进这里——那是诊断产物。"""

        event: dict[str, Any] = {"stage": step.stage.value, "status": step.status}
        if step.elapsed_ms is not None:
            # 服务端时钟：前端不必再按事件到达时刻估算，网络抖动也不会算进阶段耗时。
            event["elapsed_ms"] = step.elapsed_ms
        if step.elements:
            event["elements"] = [{"kind": item.kind, "label": item.label} for item in step.elements]
        return event

    def _emit_interpretation(
        events: "queue.Queue[tuple[str, Any] | None]",
        payload: QueryRequest,
        projection: dict[str, Any],
        request_context: Context,
    ) -> None:
        """结果解读事件。任何异常都只是没有这段话，不影响已经发出去的结果。"""

        if projection.get("state") != QueryState.COMPLETED.value:
            return
        data = projection.get("data") or {}
        rows = data.get("rows") or []
        if not rows:
            return
        # 过滤条件与默认时间窗：回答卡上已经显示的 chip，模型看不到就会把
        # 「上海的门店」读成全部门店。
        interpretation = projection.get("interpretation") or {}
        # 单位来自已投影的图表提示（业务列名 → 单位），是发布配置里的事实。
        visualization = projection.get("visualization") or {}
        units = {
            label: unit
            for label, unit in zip(
                visualization.get("y") or (), visualization.get("y_units") or (), strict=False
            )
            if isinstance(label, str) and isinstance(unit, str)
        }
        # 数值形态（占比/同比）同样来自图表提示：同比列给模型看的是 +35.88%，
        # 不是 0.3588429146832662——它只会照抄。
        formats = {
            label: fmt
            for label, fmt in zip(
                visualization.get("y") or (), visualization.get("y_formats") or (), strict=False
            )
            if isinstance(label, str) and isinstance(fmt, str)
        }
        try:
            text = application.interpret_result(
                question=payload.question,
                columns=list(data.get("columns") or []),
                rows=list(rows),
                actor_id=request_context.actor_id,
                options=payload.options,
                units=units,
                formats=formats,
                filters=[
                    item for item in (interpretation.get("filters") or []) if isinstance(item, str)
                ],
                default_time_window=interpretation.get("default_time_window"),
            )
        except Exception:  # noqa: BLE001 - 解读永远不该让这条流失败
            LOGGER.exception("analytics result interpretation failed")
            return
        if text:
            events.put(("summary", {"query_id": projection.get("query_id"), "text": text}))

    @app.post("/v1/analytics/query:stream")
    def query_stream(payload: QueryRequest, request_context: Context):
        """与 /query 同一条链路，只是把阶段推进实时吐出来。

        问数要跑几十秒，前端不该只有一个转圈。事件是纯观察：`stage` 只带
        中性阶段标识与状态，不带任何 detail（普通 wire 仍然零 Scope/语义 ID/
        SQL 泄漏），产品面的用户语言由前端映射；`result` 就是 /query 的同一份
        投影。阶段名不构成新语义，也不影响任何决策。
        """

        if request_context.project_id != payload.project_id:
            raise HTTPException(status_code=403, detail="project scope mismatch")
        expensive(request_context)
        request = payload.model_copy(
            update={"include_diagnostics": True, "include_debug_sql": allow_debug_sql}
        )
        events: queue.Queue[tuple[str, Any] | None] = queue.Queue()

        def run() -> None:
            try:
                response = application.query(
                    request,
                    actor_id=request_context.actor_id,
                    permission_scope_hash=request_context.permission_scope_hash,
                    on_trace=lambda step: events.put(("stage", _stage_event(step))),
                )
                projection = _ordinary_query_projection(response)
                events.put(("result", projection))
                # 解读在 result **之后**：表和图先到，一段话随后追加。它多花一次
                # 模型调用（数秒），不能让用户多等这几秒才看到数字。
                _emit_interpretation(events, payload, projection, request_context)
            except AnalyticsError as exc:
                events.put(("error", {"code": exc.code, "message": str(exc)}))
            except Exception:  # noqa: BLE001 - the stream must always terminate
                LOGGER.exception("analytics streaming query failed")
                events.put(
                    ("error", {"code": "ANALYTICS_QUERY_FAILED", "message": "问数服务处理失败"})
                )
            finally:
                events.put(None)

        def emit() -> Any:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            while True:
                item = events.get()
                if item is None:
                    break
                name, data = item
                yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            emit(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/analytics/query")
    def query(payload: QueryRequest, request_context: Context):
        if request_context.project_id != payload.project_id:
            raise HTTPException(status_code=403, detail="project scope mismatch")
        expensive(request_context)
        # Capture the full server-authored artifact for the privileged export;
        # `_ordinary_query_projection` below removes it from the Ask response.
        payload = payload.model_copy(
            update={
                "include_diagnostics": True,
                "include_debug_sql": allow_debug_sql,
            }
        )
        return _ordinary_query_projection(
            application.query(
                payload,
                actor_id=request_context.actor_id,
                permission_scope_hash=request_context.permission_scope_hash,
            )
        )

    return app
