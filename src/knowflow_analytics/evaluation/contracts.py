from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from knowflow_analytics.contracts import (
    FilterOperator,
    FrozenModel,
    QueryAggregationOverride,
    QueryMeasureFilter,
    QueryMetricFilter,
    QueryOrder,
    SemanticQueryType,
)
from knowflow_analytics.query.contracts import (
    MemoryReviewResult,
    MemoryStatus,
    QueryState,
    QueryTraceStep,
)


class ExpectedFilter(FrozenModel):
    dimension_id: str
    operator: FilterOperator
    value: Any = None


class GoldenCase(FrozenModel):
    id: str
    question: str = Field(min_length=1, max_length=4_000)
    dataset_ids: tuple[str, ...]
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    memory_status: MemoryStatus = MemoryStatus.DISABLED
    memory_review_result: MemoryReviewResult | None = None
    memory_review_comment: str = Field(default="", max_length=2_000)
    expected_state: QueryState
    expected_dataset_id: str | None = None
    expected_query_type: SemanticQueryType = SemanticQueryType.AGGREGATE
    expected_metric_ids: tuple[str, ...] = ()
    expected_aggregation_overrides: tuple[QueryAggregationOverride, ...] = ()
    expected_dimension_ids: tuple[str, ...] = ()
    expected_filters: tuple[ExpectedFilter, ...] = ()
    expected_measure_filters: tuple[QueryMeasureFilter, ...] = ()
    expected_metric_filters: tuple[QueryMetricFilter, ...] = ()
    expected_order_by: tuple[QueryOrder, ...] | None = None
    expected_limit: int | None = Field(default=None, ge=1, le=100_000)
    expected_s2sql: str | None = Field(default=None, min_length=1, max_length=100_000)
    expected_rows: tuple[tuple[Any, ...], ...] | None = None
    row_order_matters: bool = True
    numeric_tolerance: Decimal = Decimal("0.000001")
    expected_error_code: str | None = None

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(item).strip().lower() for item in value)
        if any(not item or len(item) > 64 for item in normalized):
            raise ValueError("golden case tags must be non-empty and at most 64 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("golden case tags must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_expected_outcome(self) -> GoldenCase:
        if self.expected_state is QueryState.COMPLETED and not (
            self.expected_metric_ids or self.expected_dimension_ids
        ):
            raise ValueError("completed golden case requires expected semantic projection")
        if self.expected_state is QueryState.COMPLETED and self.expected_rows is None:
            raise ValueError("completed golden case requires expected result rows")
        if self.expected_state is QueryState.FAILED and not self.expected_error_code:
            raise ValueError("failed golden case requires expected_error_code")
        if self.expected_state is QueryState.FAILED and self.expected_s2sql is not None:
            raise ValueError("failed golden case cannot contain expected S2SQL")
        if (
            self.expected_dataset_id is not None
            and self.expected_dataset_id not in self.dataset_ids
        ):
            raise ValueError("expected dataset must be included in the query dataset scope")
        if not {item.metric_id for item in self.expected_aggregation_overrides}.issubset(
            self.expected_metric_ids
        ):
            raise ValueError("expected aggregation overrides require expected metrics")
        if self.memory_status is MemoryStatus.ENABLED and (
            self.expected_state is not QueryState.COMPLETED
            or self.memory_review_result is not MemoryReviewResult.POSITIVE
            or {"holdout", "calibration"}.intersection(self.tags)
        ):
            raise ValueError(
                "enabled memory requires a positive reviewed completed non-evaluation case"
            )
        if self.memory_status is MemoryStatus.PENDING and self.memory_review_result is not None:
            raise ValueError("pending memory cannot contain a review result")
        projected = set(self.expected_metric_ids) | set(self.expected_dimension_ids)
        if self.expected_order_by is not None and any(
            item.element_id not in projected for item in self.expected_order_by
        ):
            raise ValueError("expected order elements require expected projection")
        return self


class GoldenSuite(FrozenModel):
    id: str
    name: str
    project_id: str
    fixed_now: datetime | None = None
    cases: tuple[GoldenCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_cases(self) -> GoldenSuite:
        case_ids = [item.id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("golden case ids must be unique")
        questions = [" ".join(item.question.split()).casefold() for item in self.cases]
        if len(questions) != len(set(questions)):
            raise ValueError("golden case questions must be unique")
        return self


class GoldenSuiteRecord(FrozenModel):
    """Audited question suite bound to one immutable modeling input version."""

    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    semantic_spec_hash: str = Field(min_length=1, max_length=128)
    suite: GoldenSuite
    saved_by: str = Field(min_length=1, max_length=128)
    updated_at: datetime

    @model_validator(mode="after")
    def suite_scope_matches_record(self) -> GoldenSuiteRecord:
        if self.suite.id != self.id or self.suite.project_id != self.project_id:
            raise ValueError("golden suite record scope is inconsistent")
        return self


class EvaluationCaseResult(FrozenModel):
    case_id: str
    passed: bool
    tags: tuple[str, ...] = ()
    expected_state: QueryState | None = None
    expected_dataset_id: str | None = None
    expected_query_type: SemanticQueryType = SemanticQueryType.AGGREGATE
    expected_metric_ids: tuple[str, ...] = ()
    expected_aggregation_overrides: tuple[QueryAggregationOverride, ...] = ()
    expected_dimension_ids: tuple[str, ...] = ()
    expected_filters: tuple[ExpectedFilter, ...] = ()
    expected_measure_filters: tuple[QueryMeasureFilter, ...] = ()
    expected_metric_filters: tuple[QueryMetricFilter, ...] = ()
    expected_order_by: tuple[QueryOrder, ...] | None = None
    expected_limit: int | None = None
    failure_stage: Literal["state", "mapping", "semantic", "result", "error"] | None = None
    message: str = ""
    actual_state: QueryState
    actual_dataset_id: str | None = None
    actual_query_type: SemanticQueryType | None = None
    actual_metric_ids: tuple[str, ...] = ()
    actual_aggregation_overrides: tuple[QueryAggregationOverride, ...] = ()
    actual_dimension_ids: tuple[str, ...] = ()
    actual_filters: tuple[ExpectedFilter, ...] = ()
    actual_measure_filters: tuple[QueryMeasureFilter, ...] = ()
    actual_metric_filters: tuple[QueryMetricFilter, ...] = ()
    actual_order_by: tuple[QueryOrder, ...] = ()
    actual_limit: int | None = None
    actual_columns: tuple[str, ...] = ()
    result_matches_expected: bool | None = None
    actual_error_code: str | None = None
    actual_clarification_options: tuple[str, ...] = ()
    actual_trace: tuple[QueryTraceStep, ...] = ()


class EvaluationSlice(FrozenModel):
    """Accuracy for one stable benchmark tag."""

    tag: str
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    accuracy: float = Field(ge=0.0, le=1.0)


class EvaluationReport(FrozenModel):
    id: str
    suite_id: str
    project_id: str
    release_id: str
    spec_hash: str
    index_snapshot_id: str
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    accuracy: float = Field(ge=0.0, le=1.0)
    required_accuracy: float = Field(ge=0.0, le=1.0)
    gate_passed: bool
    results: tuple[EvaluationCaseResult, ...]
    state_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    semantic_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    result_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    answer_correct: int = Field(default=0, ge=0)
    answer_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    rejection_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_completed: int = Field(default=0, ge=0)
    expected_non_completed: int = Field(default=0, ge=0)
    silent_wrong_count: int = Field(default=0, ge=0)
    semantic_drift_count: int = Field(default=0, ge=0)
    false_accept_count: int = Field(default=0, ge=0)
    false_refusal_count: int = Field(default=0, ge=0)
    unexpected_clarification_count: int = Field(default=0, ge=0)
    failure_stage_counts: dict[str, int] = Field(default_factory=dict)
    slices: tuple[EvaluationSlice, ...] = ()
