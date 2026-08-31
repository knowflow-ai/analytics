from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.query.contracts import (
    ClarificationQueryResponse,
    CompletedQueryResponse,
    FailedQueryResponse,
    QueryRequest,
    QueryResponse,
    QueryStage,
    QueryTraceStep,
    StructuredQueryRequest,
)

QUERY_DIAGNOSTIC_VERSION = "knowflow-query-diagnostic-v1"
QUERY_DIAGNOSTIC_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
QUERY_DIAGNOSTIC_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES = 512 * 1024
QUERY_DIAGNOSTIC_MAX_MARKDOWN_BYTES = 1024 * 1024
QUERY_DIAGNOSTIC_MAX_RESULT_ROWS = 20
QUERY_DIAGNOSTIC_MAX_RESULT_CELLS = 200
QUERY_DIAGNOSTIC_MAX_TRACE_EVENTS = 200
QUERY_DIAGNOSTIC_MAX_METRIC_SNAPSHOT_ITEMS = 100
QUERY_DIAGNOSTIC_MAX_PER_ACTOR_PROJECT = 100
QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE = 100

_CONTEXT_KEYS = ("MODELING_CONTEXT", "REVISION_CONTEXT")
QUERY_DIAGNOSTIC_TIMELINE_KEYS = (*_CONTEXT_KEYS, *(stage.value for stage in QueryStage))

_STAGE_LABELS = {
    QueryStage.PRECHECK: "请求与版本预检",
    QueryStage.CANDIDATE_DISCOVERY: "候选发现与语义映射",
    QueryStage.FINAL_PARSING: "最终语义解析",
    QueryStage.S2SQL_CORRECTING: "S2SQL 校正",
    QueryStage.ROUTE_BINDING: "查询作用域与路由绑定",
    QueryStage.TRANSLATING: "物理 SQL 翻译",
    QueryStage.PHYSICAL_SQL_CORRECTING: "物理 SQL 校正",
    QueryStage.PHYSICAL_SQL_VALIDATING: "物理 SQL 安全校验",
    QueryStage.EXECUTING: "数据库执行",
    QueryStage.POST_PROCESSING: "结果后处理",
    QueryStage.FINISHED: "响应完成",
}
_STATUS_LABELS = {
    "completed": "已完成",
    "failed": "失败",
    "clarification": "等待确认",
    "started": "已开始未结束",
    "not_run": "未运行",
    "not_recorded": "未记录",
}
_STRUCTURED_SKIPPED_STAGES = frozenset(
    {
        QueryStage.CANDIDATE_DISCOVERY,
        QueryStage.FINAL_PARSING,
        QueryStage.PHYSICAL_SQL_CORRECTING,
    }
)
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "servicetoken",
    "continuationtoken",
    "candidateid",
    "selectedcandidateid",
    "connectionstring",
    "databaseurl",
    "permission scope hash",
    "permissionscopehash",
    "credential",
    "clientsecret",
    "privatekey",
    "dsn",
    "awsaccesskeyid",
    "awssecretaccesskey",
    "awssessiontoken",
)
_CONNECTION_URI_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+@[^\s,;'\"<>()]+")
_URI_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?:token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"service[_-]?token|client[_-]?secret|api[_-]?key|password|passwd|pwd|secret)=)"
    r"(?P<value>[^&#\s]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<key>password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|service[_-]?token|session[_-]?token|token|"
    r"private[_-]?key|dsn|database[_-]?url|connection[_-]?string|"
    r"aws[_-]?(?:access[_-]?key[_-]?id|secret[_-]?access[_-]?key|session[_-]?token))"
    r"\s*(?P<separator>[:=])\s*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;&]+)"
)
_SENSITIVE_HEADER_RE = re.compile(r"(?i)\b(authorization|cookie|set-cookie)\s*[:=]\s*([^\r\n]+)")
_SIGNED_SELECTION_RE = re.compile(r"\bsel1(?:\.[A-Za-z0-9_-]+){2,}\b")
_OPENAI_STYLE_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox(?:[aboprs]|app)-[A-Za-z0-9-]{16,}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
_SENSITIVE_IDENTIFIER_KEYS = frozenset(
    {
        "name",
        "key",
        "variable",
        "variablename",
        "parameter",
        "parametername",
        "field",
        "fieldname",
        "column",
        "columnname",
    }
)
_SIBLING_VALUE_KEYS = frozenset({"value", "values", "default", "defaultvalue", "defaultvalues"})

TimelineStatus = Literal[
    "completed",
    "failed",
    "clarification",
    "started",
    "not_run",
    "not_recorded",
]
RecordedStatus = Literal["completed", "failed", "clarification", "started"]


class QueryDiagnosticMetricAggregation(FrozenModel):
    metric_id: str = Field(min_length=1, max_length=128)
    metric_name: str = Field(min_length=1, max_length=256)
    model_id: str | None = Field(default=None, max_length=128)
    aggregation: str | None = Field(default=None, max_length=64)


class QueryDiagnosticArtifact(FrozenModel):
    version: Literal[QUERY_DIAGNOSTIC_VERSION] = QUERY_DIAGNOSTIC_VERSION
    query_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    permission_scope_hash: str = Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    mode: Literal["natural", "structured"]
    question: str = Field(max_length=4_000)
    request: dict[str, Any]
    response: dict[str, Any]
    trace: tuple[QueryTraceStep, ...] = Field(max_length=QUERY_DIAGNOSTIC_MAX_TRACE_EVENTS)
    trace_truncated: bool = False
    release_id: str = Field(default="", max_length=128)
    spec_hash: str = Field(default="", max_length=128)
    index_snapshot_id: str | None = Field(default=None, max_length=128)
    revision_id: str | None = Field(default=None, max_length=128)
    revision_etag: int | None = Field(default=None, ge=1)
    revision_schema_snapshot_hash: str | None = Field(default=None, max_length=128)
    revision_semantic_spec_hash: str | None = Field(default=None, max_length=128)
    modeling_job_id: str | None = Field(default=None, max_length=128)
    metric_aggregation_snapshot: tuple[QueryDiagnosticMetricAggregation, ...] = Field(
        default=(),
        max_length=QUERY_DIAGNOSTIC_MAX_METRIC_SNAPSHOT_ITEMS,
    )

    @field_validator("actor_id", "project_id", "permission_scope_hash")
    @classmethod
    def identity_has_no_edge_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("query diagnostic identity cannot contain edge whitespace")
        return value

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> QueryDiagnosticArtifact:
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("query diagnostic timestamps must include timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("query diagnostic expiry must follow creation")
        if (self.expires_at - self.created_at).total_seconds() > (QUERY_DIAGNOSTIC_MAX_TTL_SECONDS):
            raise ValueError("query diagnostic ttl exceeds its hard retention limit")
        return self


class QueryDiagnosticTimelineEvent(FrozenModel):
    status: RecordedStatus
    detail: dict[str, Any] = Field(default_factory=dict)


class QueryDiagnosticTimelineItem(FrozenModel):
    key: str
    label: str
    group: Literal["context", "query"]
    status: TimelineStatus
    summary: str
    events: tuple[QueryDiagnosticTimelineEvent, ...] = ()
    artifacts: dict[str, Any] = Field(default_factory=dict)


class QueryDiagnosticExport(FrozenModel):
    filename: str
    media_type: Literal["text/markdown; charset=utf-8"] = "text/markdown; charset=utf-8"
    markdown: str
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    summary: dict[str, Any]
    timeline: tuple[QueryDiagnosticTimelineItem, ...]


def build_query_diagnostic_artifact(
    *,
    request: QueryRequest | StructuredQueryRequest,
    response: QueryResponse,
    actor_id: str,
    permission_scope_hash: str,
    mode: Literal["natural", "structured"],
    revision_id: str | None = None,
    revision_etag: int | None = None,
    revision_schema_snapshot_hash: str | None = None,
    revision_semantic_spec_hash: str | None = None,
    modeling_job_id: str | None = None,
    metric_aggregation_snapshot: tuple[QueryDiagnosticMetricAggregation, ...] = (),
    created_at: datetime | None = None,
    ttl_seconds: int = QUERY_DIAGNOSTIC_DEFAULT_TTL_SECONDS,
    max_result_rows: int = 0,
) -> QueryDiagnosticArtifact:
    """Create the bounded, permanently redacted query-side evidence.

    This function observes a response that already exists. It never calls a
    Mapper, parser, corrector, translator, executor, or semantic-model lookup.
    In particular, physical SQL can enter the artifact only through the public
    response field that the deployment already authorized.
    """

    if not 0 < ttl_seconds <= QUERY_DIAGNOSTIC_MAX_TTL_SECONDS:
        raise ValueError("query diagnostic ttl is outside its retention limit")
    if max_result_rows < 0 or max_result_rows > QUERY_DIAGNOSTIC_MAX_RESULT_ROWS:
        raise ValueError("query diagnostic result sample limit is invalid")
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    raw_trace = response.trace[:QUERY_DIAGNOSTIC_MAX_TRACE_EVENTS]
    trace = tuple(
        step.model_copy(update={"detail": _bounded_detail(step.detail)}) for step in raw_trace
    )
    question = request.question if isinstance(request, QueryRequest) else "结构化语义查询"
    artifact = QueryDiagnosticArtifact(
        query_id=response.query_id,
        actor_id=str(actor_id or ""),
        project_id=request.project_id,
        permission_scope_hash=permission_scope_hash,
        created_at=timestamp,
        expires_at=timestamp + timedelta(seconds=ttl_seconds),
        mode=mode,
        question=_redact_string(question, limit=4_000),
        request=_bounded_detail(_request_projection(request), max_bytes=32 * 1024),
        response=_response_projection(response, max_result_rows=max_result_rows),
        trace=trace,
        trace_truncated=len(response.trace) > len(trace),
        release_id=response.release_id,
        spec_hash=response.spec_hash,
        index_snapshot_id=response.index_snapshot_id or None,
        revision_id=revision_id,
        revision_etag=revision_etag,
        revision_schema_snapshot_hash=revision_schema_snapshot_hash,
        revision_semantic_spec_hash=revision_semantic_spec_hash,
        modeling_job_id=modeling_job_id,
        metric_aggregation_snapshot=metric_aggregation_snapshot,
    )
    artifact = _compact_artifact_to_limit(artifact)
    if _json_size(artifact.model_dump(mode="json")) > QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES:
        raise ValueError("query diagnostic artifact exceeds its hard size limit")
    return artifact


def permanently_redact_artifact(artifact: QueryDiagnosticArtifact) -> QueryDiagnosticArtifact:
    """Reapply redaction at the persistence boundary.

    Builders are not trusted as the only enforcement point. CatalogStore calls
    this immediately before every insert, so secrets cannot be recovered later
    by changing export settings or using a different renderer.
    """

    redacted = artifact.model_copy(
        update={
            "question": _redact_string(artifact.question, limit=4_000),
            "request": _bounded_detail(artifact.request, max_bytes=32 * 1024),
            "response": _bounded_detail(
                _redact_sensitive_result_columns(artifact.response),
                max_bytes=256 * 1024,
            ),
            "trace": tuple(
                step.model_copy(update={"detail": _bounded_detail(step.detail)})
                for step in artifact.trace[:QUERY_DIAGNOSTIC_MAX_TRACE_EVENTS]
            ),
            "trace_truncated": (
                artifact.trace_truncated or len(artifact.trace) > QUERY_DIAGNOSTIC_MAX_TRACE_EVENTS
            ),
            "metric_aggregation_snapshot": tuple(
                item.model_copy(
                    update={
                        "metric_name": _redact_string(item.metric_name, limit=256),
                        "model_id": (
                            _redact_string(item.model_id, limit=128)
                            if item.model_id is not None
                            else None
                        ),
                    }
                )
                for item in artifact.metric_aggregation_snapshot
            ),
        }
    )
    return _compact_artifact_to_limit(redacted)


def render_query_diagnostic_export(
    artifact: QueryDiagnosticArtifact,
    *,
    context: Mapping[str, Any] | None = None,
    version_status: str = "VERSION_UNAVAILABLE",
    allow_debug_sql: bool = False,
) -> QueryDiagnosticExport:
    export_artifact = artifact if allow_debug_sql else _gate_artifact_executable_sql(artifact)
    safe_context = {
        str(key): _bounded_context(value, allow_debug_sql=allow_debug_sql)
        for key, value in (context or {}).items()
    }
    safe_version_status = _redact_string(version_status, limit=128)
    summary = _build_summary(
        export_artifact,
        context=safe_context,
        version_status=safe_version_status,
    )
    timeline = _build_timeline(
        export_artifact,
        context=safe_context,
        version_status=safe_version_status,
    )
    markdown = _render_markdown(
        export_artifact,
        summary=summary,
        timeline=timeline,
    )
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    safe_query_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", export_artifact.query_id).strip("._")
    safe_query_id = (safe_query_id or "query")[:96]
    return QueryDiagnosticExport(
        filename=f"knowflow-diagnostic-{safe_query_id}.md",
        markdown=markdown,
        sha256=f"sha256:{digest}",
        summary=summary,
        timeline=timeline,
    )


def _gate_artifact_executable_sql(
    artifact: QueryDiagnosticArtifact,
) -> QueryDiagnosticArtifact:
    response = dict(artifact.response)
    if response.get("physical_sql") is not None:
        response["physical_sql"] = "[REDACTED]"
    physical_stages = {
        QueryStage.TRANSLATING,
        QueryStage.PHYSICAL_SQL_CORRECTING,
        QueryStage.PHYSICAL_SQL_VALIDATING,
        QueryStage.EXECUTING,
    }
    trace = tuple(
        step.model_copy(update={"detail": _redact_executable_sql_fields(step.detail)})
        if step.stage in physical_stages
        else step
        for step in artifact.trace
    )
    return artifact.model_copy(update={"response": response, "trace": trace})


def _request_projection(
    request: QueryRequest | StructuredQueryRequest,
) -> dict[str, Any]:
    payload = request.model_dump(mode="json")
    # The opaque continuation is deliberately never part of persisted evidence.
    # Its semantic consequence remains visible in the response Trace.
    if isinstance(request, QueryRequest) and request.selected_candidate_id is not None:
        payload["selected_candidate_id"] = "[REDACTED]"
    return _sanitize_value(payload, string_limit=20_000, collection_limit=100)


def _response_projection(
    response: QueryResponse,
    *,
    max_result_rows: int,
) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "query_id": response.query_id,
        "state": response.state.value,
        "release_id": response.release_id,
        "spec_hash": response.spec_hash,
        "index_snapshot_id": response.index_snapshot_id,
        "diagnostics": (
            response.diagnostics.model_dump(mode="json")
            if response.diagnostics is not None
            else None
        ),
    }
    if isinstance(response, CompletedQueryResponse):
        columns = [_redact_string(str(column), limit=256) for column in response.data.columns[:100]]
        sensitive_column_indexes = {
            index for index, column in enumerate(columns) if _sensitive_key(column)
        }
        max_columns_per_row = max(1, min(len(columns), QUERY_DIAGNOSTIC_MAX_RESULT_CELLS))
        remaining_cells = QUERY_DIAGNOSTIC_MAX_RESULT_CELLS
        rows: list[list[Any]] = []
        for source_row in response.data.rows[:max_result_rows]:
            if remaining_cells <= 0:
                break
            take = min(len(source_row), max_columns_per_row, remaining_cells)
            rows.append(
                [
                    (
                        "[REDACTED]"
                        if index in sensitive_column_indexes
                        else _sanitize_value(value, string_limit=1_000, collection_limit=20)
                    )
                    for index, value in enumerate(source_row[:take])
                ]
            )
            remaining_cells -= take
        sample_truncated = (
            response.data.truncated
            or len(response.data.rows) > len(rows)
            or len(response.data.columns) > len(columns)
            or any(
                len(row) > len(rows[index])
                for index, row in enumerate(response.data.rows[: len(rows)])
            )
        )
        projected.update(
            {
                "interpretation": response.interpretation.model_dump(mode="json"),
                "data": {
                    "columns": columns,
                    "rows": rows,
                    "row_count": response.data.row_count,
                    "source_truncated": response.data.truncated,
                    "sample_truncated": sample_truncated,
                    "sample_row_limit": max_result_rows,
                    "sample_cell_limit": QUERY_DIAGNOSTIC_MAX_RESULT_CELLS,
                },
                "visualization": response.visualization,
                "semantic_query": response.semantic_query.model_dump(mode="json"),
                "resolved_by_llm": [
                    item.model_dump(mode="json") for item in response.resolved_by_llm
                ],
                "parsed_s2sql": response.parsed_s2sql,
                "corrected_s2sql": response.corrected_s2sql,
                # Do not call a translator or inspect executor internals here.
                "physical_sql": response.physical_sql,
            }
        )
    elif isinstance(response, ClarificationQueryResponse):
        projected.update(
            {
                "question": response.question,
                "options": [
                    {
                        "kind": option.kind,
                        "label": option.label,
                        "description": option.description,
                    }
                    for option in response.options
                ],
            }
        )
    elif isinstance(response, FailedQueryResponse):
        projected["error"] = response.error.model_dump(mode="json")
    return _bounded_detail(projected, max_bytes=256 * 1024)


def _redact_sensitive_result_columns(response: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(response)
    data = projected.get("data")
    if not isinstance(data, Mapping):
        return projected
    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, (list, tuple)) or not isinstance(rows, (list, tuple)):
        return projected
    sensitive_indexes = {
        index for index, column in enumerate(columns) if _sensitive_key(str(column))
    }
    if not sensitive_indexes:
        return projected
    safe_data = dict(data)
    safe_data["rows"] = [
        ["[REDACTED]" if index in sensitive_indexes else value for index, value in enumerate(row)]
        if isinstance(row, (list, tuple))
        else row
        for row in rows
    ]
    projected["data"] = safe_data
    return projected


def _compact_artifact_to_limit(
    artifact: QueryDiagnosticArtifact,
) -> QueryDiagnosticArtifact:
    if _json_size(artifact.model_dump(mode="json")) <= QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES:
        return artifact
    compact_trace = tuple(
        step.model_copy(
            update={
                "detail": {
                    "_truncated": True,
                    "recorded_keys": list(step.detail)[:20],
                }
            }
        )
        for step in artifact.trace
    )
    response = dict(artifact.response)
    data = response.get("data")
    if isinstance(data, dict):
        response["data"] = {
            "columns": data.get("columns", [])[:50],
            "rows": data.get("rows", [])[:2],
            "row_count": data.get("row_count"),
            "source_truncated": data.get("source_truncated", False),
            "sample_truncated": True,
        }
    for key in ("parsed_s2sql", "corrected_s2sql", "physical_sql"):
        if isinstance(response.get(key), str):
            response[key] = _redact_string(response[key], limit=4_000)
    compact = artifact.model_copy(
        update={
            "trace": compact_trace,
            "trace_truncated": True,
            "request": _bounded_detail(artifact.request, max_bytes=8 * 1024),
            "response": _bounded_detail(response, max_bytes=64 * 1024),
        }
    )
    if _json_size(compact.model_dump(mode="json")) <= QUERY_DIAGNOSTIC_MAX_ARTIFACT_BYTES:
        return compact
    minimal_response = {
        key: compact.response.get(key)
        for key in (
            "query_id",
            "state",
            "release_id",
            "spec_hash",
            "index_snapshot_id",
            "diagnostics",
            "error",
            "data",
            "semantic_query",
            "parsed_s2sql",
            "corrected_s2sql",
            "physical_sql",
        )
        if key in compact.response
    }
    return compact.model_copy(
        update={
            "request": {"project_id": compact.project_id, "question": compact.question},
            "response": _bounded_detail(minimal_response, max_bytes=48 * 1024),
        }
    )


def _build_summary(
    artifact: QueryDiagnosticArtifact,
    *,
    context: Mapping[str, Any],
    version_status: str,
) -> dict[str, Any]:
    diagnosis = artifact.response.get("diagnostics")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    error = artifact.response.get("error")
    error = error if isinstance(error, dict) else {}
    first_terminal = next(
        (step for step in artifact.trace if step.status in {"failed", "clarification"}),
        None,
    )
    state = str(artifact.response.get("state", "UNKNOWN"))
    if state == "COMPLETED":
        message = "系统链路完成，需核对治理口径、S2SQL、物理 SQL 与结果。"
    else:
        message = str(
            error.get("message")
            or diagnosis.get("summary")
            or (first_terminal.detail.get("code") if first_terminal else "未知")
            or "未知"
        )
    stage = str(
        error.get("stage")
        or diagnosis.get("stage")
        or (first_terminal.stage.value if first_terminal else QueryStage.FINISHED.value)
    )
    category = str(
        diagnosis.get("category")
        or ("internal" if error and artifact.mode != "structured" else "unknown")
    )
    return _sanitize_value(
        {
            "query_id": artifact.query_id,
            "state": state,
            "mode": artifact.mode,
            "question": artifact.question,
            "diagnostic_stage": stage,
            "category": category,
            "diagnostic_category": category,
            "message": message,
            "version_status": version_status,
            "release_id": artifact.release_id or None,
            "revision_id": artifact.revision_id,
            "spec_hash": artifact.spec_hash or None,
            "index_snapshot_id": artifact.index_snapshot_id,
            "created_at": artifact.created_at.isoformat(),
            "expires_at": artifact.expires_at.isoformat(),
            "aggregation_comparison": _aggregation_comparison(artifact, context),
        },
        string_limit=4_000,
        collection_limit=100,
    )


def _aggregation_comparison(
    artifact: QueryDiagnosticArtifact,
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    semantic_query = artifact.response.get("semantic_query")
    if not isinstance(semantic_query, dict):
        return []
    metric_ids = semantic_query.get("metric_ids")
    if not isinstance(metric_ids, list):
        return []
    metric_items: Any = [
        {
            "id": item.metric_id,
            "name": item.metric_name,
            "model_id": item.model_id,
            "aggregation": item.aggregation,
        }
        for item in artifact.metric_aggregation_snapshot
    ]
    if not metric_items:
        metric_items = context.get("metric_catalog")
    if not isinstance(metric_items, list):
        release_context = context.get("release")
        if isinstance(release_context, dict):
            release_payload = release_context.get("release", release_context)
            if isinstance(release_payload, dict):
                metric_items = release_payload.get("metrics")
    if not isinstance(metric_items, list):
        catalog_context = context.get("catalog")
        if isinstance(catalog_context, dict):
            metric_items = catalog_context.get("metrics")
    metrics = {
        str(item.get("id")): item
        for item in metric_items or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    overrides = {
        str(item.get("metric_id")): item.get("aggregation")
        for item in semantic_query.get("aggregation_overrides", [])
        if isinstance(item, dict) and item.get("metric_id") is not None
    }
    query_type = semantic_query.get("query_type")
    comparisons: list[dict[str, Any]] = []
    for metric_id_value in metric_ids:
        metric_id = str(metric_id_value)
        metric = metrics.get(metric_id, {})
        catalog_aggregation = metric.get("aggregation")
        if query_type == "detail":
            query_aggregation = "none(detail)"
            source = "SemanticQuery query_type"
        elif metric_id in overrides:
            query_aggregation = overrides[metric_id]
            source = "SemanticQuery override"
        else:
            query_aggregation = catalog_aggregation
            source = "Catalog default" if catalog_aggregation is not None else "unknown"
        comparisons.append(
            {
                "metric_id": metric_id,
                "metric_name": metric.get("name") or metric.get("biz_name") or metric_id,
                "catalog_aggregation": catalog_aggregation or "unknown",
                "query_aggregation": query_aggregation or "unknown",
                "source": source,
                "matches_default": (
                    catalog_aggregation == query_aggregation
                    if catalog_aggregation is not None and query_aggregation is not None
                    else None
                ),
            }
        )
    return comparisons


def _build_timeline(
    artifact: QueryDiagnosticArtifact,
    *,
    context: Mapping[str, Any],
    version_status: str,
) -> tuple[QueryDiagnosticTimelineItem, ...]:
    modeling_artifacts = {
        key: context.get(key)
        for key in (
            "schema_snapshot",
            "modeling_diagnostics",
            "modeling_job",
            "modeling_proposal",
            "modeling_run",
        )
    }
    available_modeling = [key for key, value in modeling_artifacts.items() if value is not None]
    modeling = QueryDiagnosticTimelineItem(
        key="MODELING_CONTEXT",
        label="数据源与 AI 建模",
        group="context",
        status="completed" if available_modeling else "not_recorded",
        summary=(
            f"已补充 {len(available_modeling)} 类建模产物；缺失项按不可用降级。"
            if available_modeling
            else "未找到可绑定的建模产物。"
        ),
        artifacts=modeling_artifacts,
    )
    revision_artifacts = {
        key: context.get(key)
        for key in (
            "release",
            "revision",
            "catalog",
            "metric_catalog",
            "index_snapshot",
        )
    }
    revision_artifacts["version_status"] = version_status
    revision_available = any(
        value is not None for key, value in revision_artifacts.items() if key != "version_status"
    )
    revision = QueryDiagnosticTimelineItem(
        key="REVISION_CONTEXT",
        label="Revision / Release 冻结版本",
        group="context",
        status=(
            "failed"
            if version_status == "VERSION_STALE"
            else "completed"
            if revision_available
            else "not_recorded"
        ),
        summary=(
            "VERSION_STALE：当前 Draft 与当次查询绑定版本不一致。"
            if version_status == "VERSION_STALE"
            else "已读取当次查询绑定的版本上下文。"
            if revision_available
            else "绑定 Revision / Release 当前不可用。"
        ),
        artifacts=revision_artifacts,
    )

    grouped: dict[QueryStage, list[QueryTraceStep]] = {stage: [] for stage in QueryStage}
    positions = {stage: index for index, stage in enumerate(QueryStage)}
    for step in artifact.trace:
        grouped[step.stage].append(step)
    terminal_positions = [
        positions[step.stage]
        for step in artifact.trace
        if step.status in {"failed", "clarification"}
    ]
    first_terminal_position = min(terminal_positions) if terminal_positions else None
    recorded_positions = {positions[stage] for stage, events in grouped.items() if events}
    query_items: list[QueryDiagnosticTimelineItem] = []
    for stage in QueryStage:
        events = grouped[stage]
        if events:
            status = _aggregate_recorded_status(events)
            summary = _recorded_stage_summary(stage, status, events)
            timeline_events = tuple(
                QueryDiagnosticTimelineEvent(status=event.status, detail=event.detail)
                for event in events
            )
        else:
            status = _missing_stage_status(
                stage=stage,
                mode=artifact.mode,
                position=positions[stage],
                first_terminal_position=first_terminal_position,
                recorded_positions=recorded_positions,
                completed=str(artifact.response.get("state")) == "COMPLETED",
            )
            summary = (
                "该管线或前序终止决定本阶段未运行。"
                if status == "not_run"
                else "后续阶段已有记录，但本阶段没有可观察事件。"
            )
            timeline_events = ()
        query_items.append(
            QueryDiagnosticTimelineItem(
                key=stage.value,
                label=_STAGE_LABELS[stage],
                group="query",
                status=status,
                summary=summary,
                events=timeline_events,
                artifacts=_stage_artifacts(stage, artifact),
            )
        )
    return (modeling, revision, *query_items)


def _aggregate_recorded_status(events: list[QueryTraceStep]) -> RecordedStatus:
    statuses = {event.status for event in events}
    if "failed" in statuses:
        return "failed"
    if "clarification" in statuses:
        return "clarification"
    return events[-1].status


def _recorded_stage_summary(
    stage: QueryStage,
    status: RecordedStatus,
    events: list[QueryTraceStep],
) -> str:
    code = next(
        (
            event.detail.get("code")
            for event in events
            if event.status in {"failed", "clarification"} and event.detail.get("code")
        ),
        None,
    )
    suffix = f"，代码 {code}" if code else ""
    repeated = f"，保留 {len(events)} 条事件" if len(events) > 1 else ""
    return f"{_STAGE_LABELS[stage]}{_STATUS_LABELS[status]}{suffix}{repeated}。"


def _missing_stage_status(
    *,
    stage: QueryStage,
    mode: Literal["natural", "structured"],
    position: int,
    first_terminal_position: int | None,
    recorded_positions: set[int],
    completed: bool,
) -> Literal["not_run", "not_recorded"]:
    if mode == "structured" and stage in _STRUCTURED_SKIPPED_STAGES:
        return "not_run"
    if first_terminal_position is not None and position > first_terminal_position:
        return "not_run"
    if any(later > position for later in recorded_positions) or completed:
        return "not_recorded"
    return "not_run"


def _stage_artifacts(
    stage: QueryStage,
    artifact: QueryDiagnosticArtifact,
) -> dict[str, Any]:
    response = artifact.response
    if stage is QueryStage.PRECHECK:
        return {
            "release_id": artifact.release_id or None,
            "revision_id": artifact.revision_id,
            "spec_hash": artifact.spec_hash or None,
            "index_snapshot_id": artifact.index_snapshot_id,
        }
    if stage is QueryStage.FINAL_PARSING:
        return {"parsed_s2sql": response.get("parsed_s2sql")}
    if stage is QueryStage.S2SQL_CORRECTING:
        return {"corrected_s2sql": response.get("corrected_s2sql")}
    if stage is QueryStage.TRANSLATING:
        physical_sql = response.get("physical_sql")
        return {
            "semantic_query": response.get("semantic_query"),
            "physical_sql": (
                physical_sql if physical_sql else "部署未授权导出（仅使用原响应已授权值）"
            ),
        }
    if stage is QueryStage.EXECUTING:
        return {"result": response.get("data")}
    if stage is QueryStage.FINISHED:
        return {
            "state": response.get("state"),
            "diagnostics": response.get("diagnostics"),
            "error": response.get("error"),
        }
    return {}


def _render_markdown(
    artifact: QueryDiagnosticArtifact,
    *,
    summary: Mapping[str, Any],
    timeline: tuple[QueryDiagnosticTimelineItem, ...],
) -> str:
    chunks: list[str] = [
        "# KnowFlow 问数诊断\n\n",
        "> ⚠️ 本报告可能包含业务问题、语义目录、SQL 与维度值。分享前请检查业务敏感信息。\n\n",
        "## 快速定位\n\n",
        f"- Query ID：`{_md_inline(artifact.query_id)}`\n",
        f"- 状态：`{_md_inline(str(summary['state']))}`\n",
        f"- 管线：`{_md_inline(artifact.mode)}`\n",
        f"- 首要阶段：`{_md_inline(str(summary['diagnostic_stage']))}`\n",
        f"- 诊断分类：`{_md_inline(str(summary['diagnostic_category']))}`\n",
        f"- 版本状态：`{_md_inline(str(summary['version_status']))}`\n",
        f"- 判断：{_md_inline(str(summary['message']))}\n\n",
    ]
    comparisons = summary.get("aggregation_comparison")
    if isinstance(comparisons, list) and comparisons:
        chunks.extend(
            (
                "## 指标聚合口径\n\n",
                "| 指标 | Catalog 默认聚合 | SemanticQuery 实际聚合 | 来源 |\n",
                "|---|---|---|---|\n",
            )
        )
        for comparison in comparisons:
            metric_label = comparison.get("metric_name") or comparison.get("metric_id") or "未知"
            chunks.append(
                f"| {_md_inline(str(metric_label))} "
                f"| `{_md_inline(str(comparison.get('catalog_aggregation') or 'unknown'))}` "
                f"| `{_md_inline(str(comparison.get('query_aggregation') or 'unknown'))}` "
                f"| {_md_inline(str(comparison.get('source') or 'unknown'))} |\n"
            )
        chunks.append("\n")
    chunks.extend(
        (
            "## 固定流程时间轴\n\n",
            "| 顺序 | 分组 | 阶段 | 状态 | 摘要 |\n",
            "|---:|---|---|---|---|\n",
        )
    )
    for index, item in enumerate(timeline):
        chunks.append(
            f"| {index} | {'上下文' if item.group == 'context' else '本次查询'} "
            f"| {_md_inline(item.label)} | `{item.status}` | {_md_inline(item.summary)} |\n"
        )
    chunks.append("\n## 阶段产物\n\n")
    omitted: list[str] = []

    def append_complete_section(key: str, section_text: str) -> None:
        # Reserve space for a syntactically complete truncation footer. A
        # section is either included whole (including its closing code fence)
        # or omitted whole; byte slicing can never leave broken Markdown.
        if _byte_length("".join(chunks)) + _byte_length(section_text) > (
            QUERY_DIAGNOSTIC_MAX_MARKDOWN_BYTES - 8_192
        ):
            omitted.append(key)
            return
        chunks.append(section_text)

    for index, item in enumerate(timeline):
        section = [
            f"### {index}. {_md_inline(item.label)} · `{item.status}`\n\n",
            f"{_md_inline(item.summary)}\n\n",
        ]
        if item.events:
            section.append("事件（按原始顺序）：\n\n")
            for event_index, event in enumerate(item.events, start=1):
                section.append(f"{event_index}. `{event.status}`\n\n")
                section.append(_json_fence(event.detail))
        section.append("产物：\n\n")
        section.append(_json_fence(item.artifacts))
        section_text = "".join(section)
        append_complete_section(item.key, section_text)
    append_complete_section(
        "REQUEST_PROJECTION",
        "## 请求安全投影\n\n" + _json_fence(artifact.request),
    )
    append_complete_section(
        "RESPONSE_PROJECTION",
        "## 响应安全投影\n\n" + _json_fence(artifact.response),
    )
    if artifact.trace_truncated:
        omitted.append("TRACE_EVENTS_AFTER_LIMIT")
    if omitted:
        chunks.extend(
            (
                "## 截断说明\n\n",
                "以下内容因报告硬上限未完整展开：",
                ", ".join(f"`{_md_inline(item)}`" for item in omitted),
                "。\n",
            )
        )
    markdown = "".join(chunks)
    if _byte_length(markdown) > QUERY_DIAGNOSTIC_MAX_MARKDOWN_BYTES:
        # Defensive fallback for an unexpectedly huge summary/table. It is a
        # complete Markdown document and contains no partially cut code fence.
        markdown = (
            "# KnowFlow 问数诊断\n\n"
            "> ⚠️ 报告达到 Markdown 硬上限，详细阶段产物未展开。\n\n"
            "## 快速定位\n\n"
            f"- Query ID：`{_md_inline(artifact.query_id)}`\n"
            f"- 状态：`{_md_inline(str(summary['state']))}`\n"
            f"- 判断：{_md_inline(str(summary['message']))}\n"
        )
    return markdown


def _bounded_context(value: Any, *, allow_debug_sql: bool) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not allow_debug_sql:
        value = _redact_executable_sql_fields(value)
    return _bounded_detail(value, max_bytes=64 * 1024)


def _redact_executable_sql_fields(value: Any, *, depth: int = 0) -> Any:
    if depth >= 10:
        return "[TRUNCATED:MAX_DEPTH]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if _executable_sql_key(str(key))
                else _redact_executable_sql_fields(child, depth=depth + 1)
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_executable_sql_fields(child, depth=depth + 1) for child in value]
    return value


def _executable_sql_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
    return normalized in {
        "sql",
        "sqlquery",
        "filtersql",
        "rawfiltersql",
        "physicalsql",
        "originalsql",
        "correctedsql",
        "originalphysicalsql",
        "correctedphysicalsql",
        "querysql",
        "sourcesql",
        "viewsql",
        "sqltemplate",
    }


def _bounded_detail(value: Any, *, max_bytes: int = 8 * 1024) -> Any:
    safe = _sanitize_value(value, string_limit=20_000, collection_limit=100)
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    if len(encoded) <= max_bytes:
        return safe
    preview_budget = max(0, max_bytes - 256)
    preview = encoded[:preview_budget].decode("utf-8", errors="ignore")
    return {
        "_truncated": True,
        "original_bytes": len(encoded),
        "preview": _redact_string(preview, limit=max(0, preview_budget)),
    }


def _sanitize_value(
    value: Any,
    *,
    string_limit: int,
    collection_limit: int,
    depth: int = 0,
) -> Any:
    if depth >= 10:
        return "[TRUNCATED:MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value, limit=string_limit)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        items = list(value.items())
        projected: dict[str, Any] = {}
        redact_sibling_values = any(
            re.sub(r"[^a-z0-9]+", "", str(key).casefold()) in _SENSITIVE_IDENTIFIER_KEYS
            and isinstance(child, str)
            and _sensitive_key(child)
            for key, child in items
        )
        for key, child in items[:collection_limit]:
            safe_key = _redact_string(str(key), limit=256)
            normalized_key = re.sub(r"[^a-z0-9]+", "", str(key).casefold())
            if _sensitive_key(str(key)) or (
                redact_sibling_values and normalized_key in _SIBLING_VALUE_KEYS
            ):
                projected[safe_key] = "[REDACTED]"
            else:
                projected[safe_key] = _sanitize_value(
                    child,
                    string_limit=string_limit,
                    collection_limit=collection_limit,
                    depth=depth + 1,
                )
        if len(items) > collection_limit:
            projected["_truncated_entries"] = len(items) - collection_limit
        return projected
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        projected = [
            _sanitize_value(
                child,
                string_limit=string_limit,
                collection_limit=collection_limit,
                depth=depth + 1,
            )
            for child in items[:collection_limit]
        ]
        if len(items) > collection_limit:
            projected.append(f"[TRUNCATED:{len(items) - collection_limit}_ITEMS]")
        return projected
    return _redact_string(str(value), limit=string_limit)


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
    return any(part.replace(" ", "") in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_string(value: str, *, limit: int) -> str:
    redacted = _PEM_PRIVATE_KEY_RE.sub("[REDACTED]", value)
    redacted = _URI_QUERY_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    redacted = _CONNECTION_URI_RE.sub("[REDACTED_CONNECTION_URI]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}[REDACTED]",
        redacted,
    )
    redacted = _SENSITIVE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}: [REDACTED]",
        redacted,
    )
    redacted = _SIGNED_SELECTION_RE.sub("[REDACTED]", redacted)
    redacted = _OPENAI_STYLE_KEY_RE.sub("[REDACTED]", redacted)
    redacted = _JWT_RE.sub("[REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _SLACK_TOKEN_RE.sub("[REDACTED]", redacted)
    redacted = _AWS_ACCESS_KEY_RE.sub("[REDACTED]", redacted)
    redacted = "".join(
        character for character in redacted if character >= " " or character in "\n\t"
    )
    if len(redacted) <= limit:
        return redacted
    omitted = len(redacted) - limit
    return f"{redacted[: max(0, limit - 32)]}…[TRUNCATED:{omitted}_CHARS]"


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    )


def _json_fence(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    longest = max((len(run) for run in re.findall(r"`+", rendered)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}json\n{rendered}\n{fence}\n\n"


def _md_inline(value: str) -> str:
    escaped = html.escape(value, quote=False).replace("\r", " ").replace("\n", " ")
    # Escape every CommonMark punctuation character that can start a link,
    # image, heading, emphasis span, quote or HTML-like construct. Encoding the
    # URL colon also prevents GFM autolinking of attacker-controlled text.
    escaped = escaped.replace(":", "&#58;")
    return re.sub(r"([\\`*{}\[\]()#+.!_|>~-])", r"\\\1", escaped)


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))
