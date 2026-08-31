import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
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
    QueryRequest,
    QueryResponse,
    StructuredQueryRequest,
)
from knowflow_analytics.query.diagnostics import QueryDiagnosticExport

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
    columns = []
    seen: dict[str, int] = {}
    for index, column in enumerate(response.data.columns, start=1):
        label = column_labels.get(column, f"结果列 {index}")
        seen[label] = seen.get(label, 0) + 1
        columns.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    payload.update(
        {
            "interpretation": {
                "query_type": response.interpretation.query_type.value,
                "metrics": response.interpretation.metrics,
                "dimensions": response.interpretation.dimensions,
                "filters": response.interpretation.filters,
                "applied_defaults": (),
            },
            "data": {
                **response.data.model_dump(mode="json"),
                "columns": columns,
            },
            "visualization": _ordinary_visualization(response, columns),
            "resolved_by_llm": [item.model_dump(mode="json") for item in response.resolved_by_llm],
            "semantic_decisions": [
                item.model_dump(mode="json") for item in response.semantic_decisions
            ],
            # Signed follow-up cuts: opaque token + governed label only.
            "drilldown": [
                {"token": item.token, "kind": item.kind, "label": item.label}
                for item in response.drilldown
            ],
        }
    )
    return payload


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
    return {
        "type": chart_type if isinstance(chart_type, str) else "table",
        "x": label_by_source_column.get(x_id) if isinstance(x_id, str) else None,
        "y": [
            label_by_source_column[item]
            for item in y_ids
            if isinstance(item, str) and item in label_by_source_column
        ],
    }


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


def create_api(
    *,
    application: AnalyticsApplication,
    service_secret: str,
    allow_debug_sql: bool = False,
    request_body_limit_bytes: int = 1_048_576,
    requests_per_minute: int = 120,
    expensive_requests_per_minute: int = 10,
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

    @app.get("/v1/analytics/projects")
    def list_projects(
        request_context: Context,
        id_prefix: Annotated[str, Query(min_length=1, max_length=64)],
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
    ):
        """退出项目后此前从 UI 上不可达，只靠 sessionStorage。"""

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

    @app.post("/v1/analytics/projects/{project_id}/releases:rollback")
    def rollback_active_release(project_id: str, request_context: Context):
        require_project(project_id, request_context)
        return {"active_release_id": application.rollback_active_release(project_id=project_id)}

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
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        """被拒答的问题是"系统听不懂什么"的唯一一手数据；此前只写不读。"""

        require_project(project_id, request_context)
        return {
            "items": [
                item.model_dump(mode="json")
                for item in application.list_query_failures(project_id, limit=limit)
            ]
        }

    @app.get("/v1/analytics/projects/{project_id}/confirmation-memories")
    def list_confirmation_memories(
        project_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return {
            "items": [
                {
                    "id": item.id,
                    "detected_text": item.detected_text,
                    "selection_kind": item.selection_kind,
                    "created_at": item.created_at.isoformat(),
                    "expires_at": item.expires_at.isoformat(),
                }
                for item in application.list_confirmation_memories(
                    project_id=project_id,
                    actor_id=request_context.actor_id,
                )
            ]
        }

    @app.delete("/v1/analytics/projects/{project_id}/confirmation-memories/{memory_id}")
    def revoke_confirmation_memory(
        project_id: str,
        memory_id: Annotated[str, Path(min_length=1, max_length=128)],
        request_context: Context,
    ):
        require_project(project_id, request_context)
        revoked = application.revoke_confirmation_memory(
            project_id=project_id,
            actor_id=request_context.actor_id,
            memory_id=memory_id,
        )
        if not revoked:
            raise HTTPException(status_code=404, detail="confirmation memory not found")
        return {"revoked": True}

    @app.get("/v1/analytics/projects/{project_id}/confirmation-suggestions")
    def list_confirmation_suggestions(
        project_id: str,
        request_context: Context,
    ):
        require_project(project_id, request_context)
        return {
            "items": [
                {
                    "id": item.id,
                    "detected_text": item.detected_text,
                    "selection_kind": item.selection_kind,
                    "confirmation_count": item.confirmation_count,
                    "latest_confirmed_at": item.latest_confirmed_at.isoformat(),
                    "status": item.status,
                }
                for item in application.list_confirmation_suggestions(
                    project_id=project_id,
                    actor_id=request_context.actor_id,
                )
            ]
        }

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

    class ReleaseStructuredQueryRequest(_RequestModel):
        """Release 级结构化查询：语义 ID 直达 Corrector→Translator→Guard→Executor。"""

        project_id: str = Field(min_length=1, max_length=128)
        semantic_query: SemanticQuery
        query_id: str | None = Field(default=None, min_length=1, max_length=128)
        include_debug_sql: bool = False

    @app.post("/v1/analytics/structured-query", response_model=QueryResponse)
    def structured_query(payload: ReleaseStructuredQueryRequest, request_context: Context):
        """受治理结构化查询，绑定 Active Release；集成方入口，返回完整响应。"""

        require_project(payload.project_id, request_context)
        expensive(request_context)
        return application.structured_query(
            StructuredQueryRequest(
                project_id=payload.project_id,
                semantic_query=payload.semantic_query,
                query_id=payload.query_id,
                include_debug_sql=payload.include_debug_sql and allow_debug_sql,
            ),
            actor_id=request_context.actor_id,
            permission_scope_hash=request_context.permission_scope_hash,
        )

    class DrilldownRequest(_RequestModel):
        project_id: str = Field(min_length=1, max_length=128)
        query_id: str = Field(min_length=1, max_length=128)
        token: str = Field(min_length=1, max_length=1_024)

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
            )
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
