from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic, perf_counter, sleep
from typing import Any, Literal, Protocol

import httpx
import sqlglot
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, text

_DEFAULT_MODEL_ID = "nvidia/nemotron-3-super-120b-a12b___OpenAI-API@OpenAI-API-Compatible"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelingInput(_FrozenModel):
    project_name: str = Field(min_length=1, max_length=256)
    schemas: tuple[str, ...] = Field(min_length=1, max_length=20)
    selected_tables: dict[str, tuple[str, ...]] = Field(min_length=1, max_length=20)
    include_views: bool = False

    @model_validator(mode="after")
    def scope_is_explicit(self) -> ModelingInput:
        if set(self.schemas) != set(self.selected_tables):
            raise ValueError("selected_tables must match schemas exactly")
        if any(not tables for tables in self.selected_tables.values()):
            raise ValueError("every schema must contain selected tables")
        return self


class HiddenCase(_FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4_000)
    root_table: str = Field(min_length=1, max_length=256)
    root_schema: str | None = Field(default=None, min_length=1, max_length=256)
    # Fixture-only business truth for an expected clarification. Ordinary API
    # options intentionally expose no Dataset/Scope identifier.
    expected_clarification_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
    )
    expected_state: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    reference_sql: str | None = Field(default=None, max_length=100_000)
    row_order_matters: bool = False
    allowed_error_codes: tuple[str, ...] = Field(default=(), max_length=50)
    allowed_error_stages: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def completed_case_has_reference_sql(self) -> HiddenCase:
        if self.expected_state == "COMPLETED" and not (self.reference_sql or "").strip():
            raise ValueError("completed hidden cases require reference_sql")
        if self.expected_state == "FAILED" and self.reference_sql is not None:
            raise ValueError("failed hidden cases must not include reference_sql")
        if self.expected_state == "FAILED" and not self.allowed_error_codes:
            raise ValueError("failed hidden cases require allowed_error_codes")
        if self.expected_state == "COMPLETED" and (
            self.allowed_error_codes or self.allowed_error_stages
        ):
            raise ValueError("completed hidden cases cannot allow refusal errors")
        return self


class HiddenSuite(_FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    cases: tuple[HiddenCase, ...] = Field(min_length=1, max_length=1_000)

    @field_validator("cases")
    @classmethod
    def case_ids_are_unique(cls, cases: tuple[HiddenCase, ...]) -> tuple[HiddenCase, ...]:
        identifiers = [item.id for item in cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("hidden case identifiers must be unique")
        return cases


class ProductQuestionResult(_FrozenModel):
    case_id: str
    expected_state: str
    actual_state: str
    correct: bool
    silent_wrong: bool = False
    false_refusal: bool = False
    correct_refusal: bool = False
    first_turn_clarification: bool = False
    manual_selection_count: int = Field(default=0, ge=0)
    auto_adopted_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_stage: str | None = None
    parser_source: str | None = None
    rule_fallback: bool = False
    corrected_s2sql: str | None = None
    physical_sql: str | None = None
    expected_row_count: int | None = None
    actual_row_count: int | None = None
    expected_rows_preview: tuple[tuple[Any, ...], ...] = ()
    actual_rows_preview: tuple[tuple[Any, ...], ...] = ()
    expected_result_hash: str | None = None
    actual_result_hash: str | None = None


class ProductRelationRejection(_FrozenModel):
    relation_id: str
    reason_code: str


class ProductAccuracyReport(_FrozenModel):
    contract_version: Literal["knowflow-product-accuracy-v1"] = "knowflow-product-accuracy-v1"
    modeling_completed: bool
    implementation_revision: str
    model_id: str
    modeling_error: str | None = None
    project_id: str | None = None
    revision_id: str | None = None
    revision_etag: int | None = None
    schema_snapshot_hash: str | None = None
    modeling_output_hash: str | None = None
    modeling_input_hash: str
    hidden_suite_hash: str
    modeling_started_at: datetime
    modeling_frozen_at: datetime
    hidden_suite_loaded_at: datetime
    modeling_duration_ms: float
    auto_confirmed_relation_count: int = 0
    rejected_relation_count: int = 0
    relation_rejections: tuple[ProductRelationRejection, ...] = ()
    ai_suggestion_count: int = 0
    ai_default_accepted_count: int = 0
    ai_override_count: int = 0
    adopted_topic_count: int = 0
    total_questions: int
    correct_answers: int
    accuracy: float
    silent_wrong_count: int
    false_refusal_count: int
    correct_refusal_count: int
    first_turn_clarification_count: int = 0
    manual_selection_count: int = 0
    auto_adopted_count: int = 0
    results: tuple[ProductQuestionResult, ...]


class ProductApi(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any: ...


class ReferenceExecutor(Protocol):
    def execute(self, sql: str) -> tuple[tuple[Any, ...], ...]: ...


def run_product_accuracy_campaign(
    *,
    api: ProductApi,
    modeling_input: ModelingInput,
    load_hidden_suite: Callable[[], HiddenSuite],
    reference_executor: ReferenceExecutor,
    implementation_revision: str = "test",
    model_id: str = _DEFAULT_MODEL_ID,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    include_sensitive_diagnostics: bool = False,
) -> ProductAccuracyReport:
    """Run the browser product journey before opening the hidden question suite."""

    started_at = now()
    started_clock = perf_counter()
    project_id: str | None = None
    revision: dict[str, Any] | None = None
    modeling_error: str | None = None
    auto_confirmed_relations = 0
    rejected_relations = 0
    relation_rejections: list[ProductRelationRejection] = []
    suggestion_count = 0
    accepted_count = 0
    override_count = 0
    adopted_topics = 0
    try:
        project = api.request(
            "POST", "/v1/analytics/core/projects", {"name": modeling_input.project_name}
        )
        project_id = _required_string(project, "id")
        base = f"/v1/analytics/core/projects/{project_id}"
        snapshot = api.request(
            "POST",
            f"{base}/schema-snapshots",
            {
                "schemas": list(modeling_input.schemas),
                "selected_tables": {
                    schema: list(tables)
                    for schema, tables in modeling_input.selected_tables.items()
                },
                "include_views": modeling_input.include_views,
            },
        )
        revision = api.request(
            "POST",
            f"{base}/revisions",
            {"schema_snapshot_id": _required_string(snapshot, "id")},
        )
        revision_id = _required_string(revision, "id")
        revision_base = f"{base}/revisions/{revision_id}"
        for table in _required_sequence(snapshot, "tables"):
            revision = api.request(
                "POST",
                f"{revision_base}/models:from-table",
                {
                    **_version(revision),
                    "schema_name": _required_string(table, "schema_name"),
                    "table_name": _required_string(table, "name"),
                },
            )

        for relation in _catalog_relations(revision):
            rejection_reason = _relation_rejection_reason(relation)
            if rejection_reason is not None:
                rejected_relations += 1
                relation_rejections.append(
                    ProductRelationRejection(
                        relation_id=str(relation.get("id") or "unknown"),
                        reason_code=rejection_reason,
                    )
                )
                continue
            confirmed_relation = _confirmed_database_relation(relation)
            revision = api.request(
                "PUT",
                f"{revision_base}/catalog/relations/{relation['id']}",
                {**_version(revision), "relation": confirmed_relation},
            )
            auto_confirmed_relations += 1

        job = api.request(
            "POST",
            f"{revision_base}/modeling-jobs",
            {"expected_etag": revision["etag"]},
        )
        proposal = _wait_for_modeling_proposal(
            api=api,
            project_base=base,
            revision_base=revision_base,
            initial_job=job,
        )
        suggestions = _required_sequence(proposal, "suggestions")
        decisions = _required_sequence(proposal, "decisions")
        suggestion_count = len(suggestions)
        accepted_count = sum(bool(item.get("accept")) for item in decisions)
        override_count = sum(bool(item.get("overrides")) for item in decisions)
        if accepted_count != suggestion_count or override_count:
            raise ValueError("AI proposal defaults are not zero-edit accept-all decisions")
        saved = api.request(
            "PUT",
            f"{revision_base}/modeling-proposals/{proposal['id']}",
            {
                "expected_proposal_etag": proposal["etag"],
                "expected_proposal_hash": proposal["proposal_hash"],
                "decisions": list(decisions),
                "alias_reviews": [
                    {
                        "resource_type": item["resource_type"],
                        "resource_id": item["resource_id"],
                        "aliases": list(item.get("aliases") or ()),
                        "display_name": item.get("display_name"),
                    }
                    for item in _required_sequence(
                        _required_mapping(proposal, "artifact"),
                        "alias_drafts",
                    )
                ],
            },
        )
        applied = api.request(
            "POST",
            f"{revision_base}/modeling-proposals/{saved['id']}:apply",
            {
                **_version(revision),
                "expected_proposal_etag": saved["etag"],
                "expected_proposal_hash": saved["proposal_hash"],
                "confirmation": "apply",
            },
        )
        revision = _required_mapping(applied, "revision")
        artifact = _required_mapping(saved, "artifact")
        if artifact.get("query_scope_compiler_version") != "knowflow-query-scope-v1":
            raise ValueError("AI one-click modeling did not freeze the query-scope compiler")
        if not str(artifact.get("query_scope_compilation_hash") or "").startswith("sha256:"):
            raise ValueError("AI one-click modeling did not freeze its query-scope manifest")
        artifact_routes = _required_sequence(artifact, "analysis_topic_routes")
        scope_diagnostics = _required_sequence(artifact, "query_scope_diagnostics")
        if len(scope_diagnostics) != len(artifact_routes):
            raise ValueError("AI one-click modeling returned incomplete query-scope diagnostics")
        artifact_context = _required_sequence(artifact, "semantic_context")
        if not artifact_context:
            raise ValueError("AI one-click modeling returned no reviewable SemanticContext")
        revision_spec = _required_mapping(revision, "semantic_spec")
        applied_routes = _required_sequence(revision_spec, "analysis_topic_routes")
        expected_topic_ids = {str(item.get("dataset_id") or "") for item in artifact_routes}
        applied_topic_ids = {str(item.get("dataset_id")) for item in applied_routes}
        if "" in expected_topic_ids:
            raise ValueError("AI one-click modeling returned a topic without dataset_id")
        if expected_topic_ids != applied_topic_ids:
            raise ValueError("AI one-click modeling did not atomically apply its analysis topics")
        adopted_topics = len(applied_routes)
        revision = api.request("POST", f"{revision_base}/validate", {})
        if revision.get("state") != "validated":
            raise ValueError("revision did not reach validated state")
    except Exception as exc:
        modeling_error = f"{type(exc).__name__}: {exc}"

    frozen_at = now()
    modeling_duration_ms = round((perf_counter() - started_clock) * 1_000, 3)
    modeling_output_hash = _content_hash(revision) if revision is not None else None

    # This is intentionally the first point at which hidden questions are opened.
    hidden_suite = load_hidden_suite()
    hidden_loaded_at = now()
    if hidden_loaded_at <= frozen_at:
        raise ValueError("hidden suite load time must follow modeling freeze time")

    if modeling_error is not None or revision is None or project_id is None:
        results = tuple(
            ProductQuestionResult(
                case_id=item.id,
                expected_state=item.expected_state,
                actual_state="MODELING_FAILED",
                correct=False,
                false_refusal=item.expected_state == "COMPLETED",
                error_code="MODELING_FAILED",
            )
            for item in hidden_suite.cases
        )
    else:
        results = _evaluate_hidden_suite(
            api=api,
            project_id=project_id,
            revision=revision,
            hidden_suite=hidden_suite,
            reference_executor=reference_executor,
            include_sensitive_diagnostics=include_sensitive_diagnostics,
        )
    correct_answers = sum(item.correct for item in results)
    total = len(results)
    return ProductAccuracyReport(
        modeling_completed=modeling_error is None,
        implementation_revision=implementation_revision,
        model_id=model_id,
        modeling_error=modeling_error,
        project_id=project_id,
        revision_id=str(revision.get("id")) if revision is not None else None,
        revision_etag=int(revision["etag"]) if revision is not None else None,
        schema_snapshot_hash=(
            str(revision["schema_snapshot_hash"]) if revision is not None else None
        ),
        modeling_output_hash=modeling_output_hash,
        modeling_input_hash=_content_hash(modeling_input.model_dump(mode="json")),
        hidden_suite_hash=_content_hash(hidden_suite.model_dump(mode="json")),
        modeling_started_at=started_at,
        modeling_frozen_at=frozen_at,
        hidden_suite_loaded_at=hidden_loaded_at,
        modeling_duration_ms=modeling_duration_ms,
        auto_confirmed_relation_count=auto_confirmed_relations,
        rejected_relation_count=rejected_relations,
        ai_suggestion_count=suggestion_count,
        ai_default_accepted_count=accepted_count,
        ai_override_count=override_count,
        adopted_topic_count=adopted_topics,
        relation_rejections=tuple(relation_rejections),
        total_questions=total,
        correct_answers=correct_answers,
        accuracy=correct_answers / total,
        silent_wrong_count=sum(item.silent_wrong for item in results),
        false_refusal_count=sum(item.false_refusal for item in results),
        correct_refusal_count=sum(item.correct_refusal for item in results),
        first_turn_clarification_count=sum(item.first_turn_clarification for item in results),
        manual_selection_count=sum(item.manual_selection_count for item in results),
        auto_adopted_count=sum(item.auto_adopted_count for item in results),
        results=results,
    )


def _evaluate_hidden_suite(
    *,
    api: ProductApi,
    project_id: str,
    revision: Mapping[str, Any],
    hidden_suite: HiddenSuite,
    reference_executor: ReferenceExecutor,
    include_sensitive_diagnostics: bool,
) -> tuple[ProductQuestionResult, ...]:
    revision_id = str(revision["id"])
    base = f"/v1/analytics/core/projects/{project_id}/revisions/{revision_id}"
    dataset_by_root_table = _dataset_by_root_table(revision)
    all_dataset_ids = _all_routed_dataset_ids(revision)
    results: list[ProductQuestionResult] = []
    for case in hidden_suite.cases:
        first_turn_clarification = False
        manual_selection_count = 0
        auto_adopted_count = 0
        root_key = (
            f"{case.root_schema}.{case.root_table}"
            if case.root_schema is not None
            else case.root_table
        )
        dataset_id = dataset_by_root_table.get(root_key)
        if dataset_id is None:
            results.append(
                ProductQuestionResult(
                    case_id=case.id,
                    expected_state=case.expected_state,
                    actual_state="FAILED",
                    correct=False,
                    false_refusal=case.expected_state == "COMPLETED",
                    error_code="ANALYSIS_TOPIC_MISSING",
                )
            )
            continue
        try:
            request_body = {
                **_version(revision),
                "question": case.question,
                "dataset_ids": list(all_dataset_ids),
                **(
                    {"include_diagnostics": True, "include_debug_sql": True}
                    if include_sensitive_diagnostics
                    else {}
                ),
            }
            response = api.request(
                "POST",
                f"{base}/query-preview",
                request_body,
            )
            first_turn_clarification = str(response.get("state") or "") == "CLARIFICATION_REQUIRED"
            if str(response.get("state") or "") == "CLARIFICATION_REQUIRED":
                expected_label = case.expected_clarification_label
                if expected_label is None:
                    raise ValueError(
                        "hidden case clarification requires fixture-side business label"
                    )
                selected = next(
                    (
                        item
                        for item in response.get("options", ())
                        if isinstance(item, dict) and str(item.get("label") or "") == expected_label
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("clarification omitted the adjudicated business option")
                manual_selection_count = 1
                response = api.request(
                    "POST",
                    f"{base}/query-preview",
                    {
                        **request_body,
                        "selected_candidate_id": _required_string(selected, "candidate_id"),
                        "expected_release_id": _required_string(response, "release_id"),
                        "expected_spec_hash": _required_string(response, "spec_hash"),
                        "expected_index_snapshot_id": _required_string(
                            response, "index_snapshot_id"
                        ),
                    },
                )
            auto_adopted_count = sum(
                1
                for item in response.get("semantic_decisions", ())
                if isinstance(item, dict) and str(item.get("source") or "") == "ai"
            )
        except Exception as exc:
            response = {
                "state": "FAILED",
                "error": {"code": "PRODUCT_QUERY_REQUEST_FAILED", "message": str(exc)},
                "trace": [],
            }
        actual_state = str(response.get("state") or "FAILED")
        parser_source = _parser_source(response.get("trace") or ())
        journey_metrics = {
            "first_turn_clarification": first_turn_clarification,
            "manual_selection_count": manual_selection_count,
            "auto_adopted_count": auto_adopted_count,
        }
        if case.expected_state == "FAILED":
            error_code = _error_code(response)
            error_stage = _error_stage(response)
            correct = bool(
                actual_state == "FAILED"
                and error_code in case.allowed_error_codes
                and (not case.allowed_error_stages or error_stage in case.allowed_error_stages)
                and error_code
                not in {
                    "PRODUCT_QUERY_REQUEST_FAILED",
                    "INTERNAL_ERROR",
                    "AUTHENTICATION_FAILED",
                    "AUTHORIZATION_FAILED",
                }
            )
            results.append(
                ProductQuestionResult(
                    case_id=case.id,
                    expected_state=case.expected_state,
                    actual_state=actual_state,
                    correct=correct,
                    **journey_metrics,
                    correct_refusal=correct,
                    silent_wrong=not correct,
                    error_code=error_code,
                    error_stage=error_stage,
                    parser_source=parser_source,
                    rule_fallback=parser_source == "rule",
                    corrected_s2sql=(
                        _optional_string(response, "corrected_s2sql")
                        if include_sensitive_diagnostics
                        else None
                    ),
                    physical_sql=(
                        _optional_string(response, "physical_sql")
                        if include_sensitive_diagnostics
                        else None
                    ),
                )
            )
            continue
        if actual_state != "COMPLETED":
            results.append(
                ProductQuestionResult(
                    case_id=case.id,
                    expected_state=case.expected_state,
                    actual_state=actual_state,
                    correct=False,
                    **journey_metrics,
                    false_refusal=True,
                    error_code=_error_code(response),
                    error_stage=_error_stage(response),
                    parser_source=parser_source,
                    rule_fallback=parser_source == "rule",
                    corrected_s2sql=(
                        _optional_string(response, "corrected_s2sql")
                        if include_sensitive_diagnostics
                        else None
                    ),
                    physical_sql=(
                        _optional_string(response, "physical_sql")
                        if include_sensitive_diagnostics
                        else None
                    ),
                )
            )
            continue
        expected_rows = reference_executor.execute(case.reference_sql or "")
        response_data = response.get("data", {})
        actual_rows = tuple(tuple(item) for item in response_data.get("rows", ()))
        numeric_columns = _numeric_column_indexes(revision, response_data)
        expected_signature = _row_signature(
            expected_rows,
            ordered=case.row_order_matters,
            numeric_columns=numeric_columns,
        )
        actual_signature = _row_signature(
            actual_rows,
            ordered=case.row_order_matters,
            numeric_columns=numeric_columns,
        )
        actual_dataset_id = _response_dataset_id(response)
        correct = actual_signature == expected_signature and actual_dataset_id in {
            None,
            dataset_id,
        }
        results.append(
            ProductQuestionResult(
                case_id=case.id,
                expected_state=case.expected_state,
                actual_state=actual_state,
                correct=correct,
                **journey_metrics,
                silent_wrong=not correct,
                error_code=_error_code(response),
                error_stage=_error_stage(response),
                parser_source=parser_source,
                rule_fallback=parser_source == "rule",
                corrected_s2sql=(
                    _optional_string(response, "corrected_s2sql")
                    if include_sensitive_diagnostics
                    else None
                ),
                physical_sql=(
                    _optional_string(response, "physical_sql")
                    if include_sensitive_diagnostics
                    else None
                ),
                expected_row_count=len(expected_rows),
                actual_row_count=len(actual_rows),
                expected_rows_preview=(
                    tuple(expected_rows[:5]) if include_sensitive_diagnostics else ()
                ),
                actual_rows_preview=(
                    tuple(actual_rows[:5]) if include_sensitive_diagnostics else ()
                ),
                expected_result_hash=_content_hash(expected_signature),
                actual_result_hash=_content_hash(actual_signature),
            )
        )
    return tuple(results)


def _wait_for_modeling_proposal(
    *,
    api: ProductApi,
    project_base: str,
    revision_base: str,
    initial_job: Mapping[str, Any],
    timeout_seconds: float = 1_200,
) -> Mapping[str, Any]:
    """Follow the same persisted async job contract as the active browser."""

    job = initial_job
    job_id = _required_string(job, "id")
    deadline = monotonic() + timeout_seconds
    while str(job.get("status") or "") in {"queued", "running"}:
        if monotonic() >= deadline:
            raise TimeoutError("AI modeling job did not complete before the product timeout")
        job = api.request("GET", f"{project_base}/modeling-jobs/{job_id}")
        if str(job.get("status") or "") in {"queued", "running"}:
            sleep(1)
    status = str(job.get("status") or "")
    if status != "completed":
        raise RuntimeError(
            f"AI modeling job ended as {status or 'unknown'}: {job.get('error') or ''}"
        )
    proposal_id = _required_string(job, "proposal_id")
    return api.request("GET", f"{revision_base}/modeling-proposals/{proposal_id}")


class HttpProductApi:
    def __init__(
        self,
        *,
        base_url: str,
        authorization: str | None,
        cookie: str | None,
        client: httpx.Client | None = None,
    ) -> None:
        if not authorization and not cookie:
            raise ValueError("product BFF authentication is required")
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        if cookie:
            headers["Cookie"] = cookie
        # One-click modeling fans out per table and per business entity, so the
        # campaign must outlast the BFF's own AI-suggestion budget rather than
        # cutting a run short and scoring it as a modeling failure.
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=1200, trust_env=False
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = None
        for attempt, delay in enumerate((5.0, 15.0, 45.0, 0.0)):
            response = self._client.request(method, path, json=body)
            if response.status_code != 429 or attempt == 3:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = min(60.0, max(1.0, float(retry_after)))
            except (TypeError, ValueError):
                wait_seconds = delay
            sleep(wait_seconds)
        assert response is not None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("product BFF returned a non-object response")
        # The active embedded microfrontend uses the transparent /core gate and
        # receives the core object directly. Legacy /modeling routes use the
        # RAGFlow {code,data} envelope; accepting both keeps old reports readable
        # while the campaign itself exercises the active browser dialect.
        if "code" not in payload:
            return payload
        if payload.get("code") != 0:
            raise RuntimeError(str(payload.get("message") or "product BFF request failed"))
        return payload.get("data")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class PostgresReferenceExecutor:
    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url, pool_pre_ping=True)

    def execute(self, sql: str) -> tuple[tuple[Any, ...], ...]:
        statements = sqlglot.parse(sql, read="postgres")
        allowed = {"select", "union", "intersect", "except"}
        if len(statements) != 1 or statements[0].key not in allowed:
            raise ValueError("reference SQL must be one read-only query")
        with self._engine.connect() as connection, connection.begin():
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout = 30000")
            result = connection.execute(text(sql))
            return tuple(tuple(row) for row in result.fetchall())

    def close(self) -> None:
        self._engine.dispose()


def _without_sensitive_diagnostics(report: ProductAccuracyReport) -> ProductAccuracyReport:
    """Keep the score artifact useful without persisting row values or SQL."""

    return report.model_copy(
        update={
            "results": tuple(
                item.model_copy(
                    update={
                        "corrected_s2sql": None,
                        "physical_sql": None,
                        "expected_rows_preview": (),
                        "actual_rows_preview": (),
                    }
                )
                for item in report.results
            )
        }
    )


def _report_console_summary(report: ProductAccuracyReport) -> dict[str, Any]:
    """Return only aggregate, non-row diagnostics suitable for CI stdout."""

    return {
        "modeling_completed": report.modeling_completed,
        "total_questions": report.total_questions,
        "correct_answers": report.correct_answers,
        "accuracy": report.accuracy,
        "silent_wrong_count": report.silent_wrong_count,
        "false_refusal_count": report.false_refusal_count,
        "correct_refusal_count": report.correct_refusal_count,
    }


def main() -> None:
    arguments = _arguments()
    modeling_path = Path(arguments.modeling_input).expanduser().resolve()
    hidden_path = Path(arguments.hidden_suite).expanduser().resolve()
    modeling_input = ModelingInput.model_validate_json(modeling_path.read_text(encoding="utf-8"))
    authorization = os.getenv(arguments.authorization_env)
    cookie = os.getenv(arguments.cookie_env) if arguments.cookie_env else None
    database_url = os.getenv(arguments.reference_database_url_env)
    if not database_url:
        raise RuntimeError("reference database URL environment variable is missing")
    api = HttpProductApi(
        base_url=arguments.ragflow_base_url,
        authorization=authorization,
        cookie=cookie,
    )
    reference = PostgresReferenceExecutor(database_url)
    try:
        report = run_product_accuracy_campaign(
            api=api,
            modeling_input=modeling_input,
            load_hidden_suite=lambda: HiddenSuite.model_validate_json(
                hidden_path.read_text(encoding="utf-8")
            ),
            reference_executor=reference,
            implementation_revision=arguments.implementation_revision,
            model_id=arguments.model_id,
            include_sensitive_diagnostics=bool(arguments.diagnostics_output),
        )
    finally:
        reference.close()
        api.close()
    output = Path(arguments.output).expanduser().resolve()
    if arguments.diagnostics_output:
        diagnostics_output = Path(arguments.diagnostics_output).expanduser().resolve()
        _write_report(diagnostics_output, report.model_dump_json(indent=2))
        report = _without_sensitive_diagnostics(report)
    _write_report(output, report.model_dump_json(indent=2))
    print(json.dumps(_report_console_summary(report), ensure_ascii=False, sort_keys=True))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the sole KnowFlow product accuracy campaign")
    parser.add_argument("--modeling-input", required=True)
    parser.add_argument("--hidden-suite", required=True)
    parser.add_argument("--output", default="product_accuracy_report.json")
    parser.add_argument(
        "--diagnostics-output",
        help="optional private report containing SQL and the first five raw rows per case",
    )
    parser.add_argument("--implementation-revision", required=True)
    parser.add_argument("--model-id", default=_DEFAULT_MODEL_ID)
    parser.add_argument("--ragflow-base-url", default="http://127.0.0.1:9380")
    parser.add_argument(
        "--authorization-env",
        default="KNOWFLOW_PRODUCT_ACCURACY_AUTHORIZATION",
        help="environment variable containing the browser Authorization header",
    )
    parser.add_argument(
        "--cookie-env",
        default="KNOWFLOW_PRODUCT_ACCURACY_COOKIE",
        help="environment variable containing the browser Cookie header",
    )
    parser.add_argument(
        "--reference-database-url-env",
        default="KNOWFLOW_PRODUCT_ACCURACY_DATABASE_URL",
    )
    return parser.parse_args()


def _relation_rejection_reason(relation: Mapping[str, Any]) -> str | None:
    evidence = relation.get("knowflowEvidence") or relation.get("knowflow_evidence")
    if evidence != "database_foreign_key":
        return "RELATION_NOT_DATABASE_FOREIGN_KEY"
    conditions = relation.get("joinConditions")
    if not bool(
        relation.get("id")
        and relation.get("fromModelId")
        and relation.get("toModelId")
        and isinstance(conditions, list)
        and conditions
        and all(item.get("operator") == "=" for item in conditions if isinstance(item, dict))
        and len([item for item in conditions if isinstance(item, dict)]) == len(conditions)
    ):
        return "RELATION_UNSAFE_SHAPE"
    return None


def _safe_database_relation(relation: Mapping[str, Any]) -> bool:
    return _relation_rejection_reason(relation) is None


def _confirmed_database_relation(relation: Mapping[str, Any]) -> dict[str, Any]:
    cardinality = relation.get("knowflowCardinality")
    if cardinality not in {"one_to_one", "one_to_many", "many_to_one"}:
        # Database FK candidates are emitted from the constrained (many) table
        # toward the referenced (one) table. A unique FK may be one-to-one, but
        # many-to-one is the conservative non-fanout query contract.
        cardinality = "many_to_one"
    return {**relation, "knowflowCardinality": cardinality}


def _catalog_relations(revision: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    catalog = revision.get("semantic_catalog")
    relations = catalog.get("modelRelations") if isinstance(catalog, dict) else None
    if not isinstance(relations, list):
        return ()
    return tuple(dict(item) for item in relations if isinstance(item, dict))


def _dataset_by_root_table(revision: Mapping[str, Any]) -> dict[str, str]:
    spec = _required_mapping(revision, "semantic_spec")
    models = {
        str(item["id"]): (
            str(item.get("schema_name") or ""),
            str(item.get("table") or ""),
        )
        for item in spec.get("models", ())
    }
    routes = spec.get("analysis_topic_routes") or spec.get("analysisTopicRoutes") or ()
    grouped: dict[str, list[tuple[str, str]]] = {}
    for route in routes:
        if not isinstance(route, dict):
            continue
        model = models.get(str(route.get("root_model_id") or route.get("rootModelId") or ""))
        schema, table = model or ("", "")
        dataset_id = str(route.get("dataset_id") or route.get("datasetId") or "")
        if table and dataset_id:
            grouped.setdefault(table, []).append((schema, dataset_id))
    result: dict[str, str] = {}
    for table, candidates in grouped.items():
        if len(candidates) == 1:
            result[table] = candidates[0][1]
        for schema, dataset_id in candidates:
            if schema:
                result[f"{schema}.{table}"] = dataset_id
    return result


def _all_routed_dataset_ids(revision: Mapping[str, Any]) -> tuple[str, ...]:
    spec = _required_mapping(revision, "semantic_spec")
    routes = spec.get("analysis_topic_routes") or spec.get("analysisTopicRoutes") or ()
    return tuple(
        sorted(
            {
                str(item.get("dataset_id") or item.get("datasetId") or "")
                for item in routes
                if isinstance(item, dict) and (item.get("dataset_id") or item.get("datasetId"))
            }
        )
    )


def _version(revision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "expected_etag": int(revision["etag"]),
        "schema_snapshot_hash": str(revision["schema_snapshot_hash"]),
    }


def _parser_source(trace: Sequence[Any]) -> str | None:
    for step in reversed(trace):
        if not isinstance(step, dict) or step.get("stage") != "FINAL_PARSING":
            continue
        detail = step.get("detail")
        if isinstance(detail, dict) and detail.get("parser"):
            return str(detail["parser"])
    return None


def _clarification_code(response: Mapping[str, Any]) -> str | None:
    for step in reversed(tuple(response.get("trace") or ())):
        if not isinstance(step, dict) or step.get("status") != "clarification":
            continue
        detail = step.get("detail")
        if not isinstance(detail, dict):
            continue
        if detail.get("code"):
            return str(detail["code"])
        scope_resolution = detail.get("scope_resolution")
        if isinstance(scope_resolution, dict) and scope_resolution.get("code"):
            return str(scope_resolution["code"])
    return None


def _response_dataset_id(response: Mapping[str, Any]) -> str | None:
    for key in ("semantic_query", "interpretation"):
        value = response.get(key)
        if not isinstance(value, dict):
            continue
        dataset_id = value.get("dataset_id")
        if dataset_id:
            return str(dataset_id)
    return None


def _error_code(response: Mapping[str, Any]) -> str | None:
    error = response.get("error")
    return str(error.get("code")) if isinstance(error, dict) and error.get("code") else None


def _error_stage(response: Mapping[str, Any]) -> str | None:
    error = response.get("error")
    return str(error.get("stage")) if isinstance(error, dict) and error.get("stage") else None


def _optional_string(response: Mapping[str, Any], key: str) -> str | None:
    value = response.get(key)
    return str(value) if value is not None and str(value).strip() else None


def _numeric_column_indexes(
    revision: Mapping[str, Any],
    response_data: Mapping[str, Any],
) -> frozenset[int]:
    spec = _required_mapping(revision, "semantic_spec")
    metrics = _required_sequence(spec, "metrics")
    numeric_ids = {str(item.get("id") or "") for item in metrics}
    # NUMERIC 类型的维度（年份等）经 API 序列化成字符串，而参考执行器返回
    # 数字——按受治理列类型归一，字符串形状（"6200" 可能是业务代码）不可信。
    numeric_types = ("int", "numeric", "decimal", "real", "double", "float")
    dimensions = spec.get("dimensions")
    for item in dimensions if isinstance(dimensions, list) else ():
        if not isinstance(item, dict):
            continue
        data_type = str(item.get("data_type") or "").casefold()
        if any(marker in data_type for marker in numeric_types):
            numeric_ids.add(str(item.get("id") or ""))
    columns = response_data.get("columns")
    if not isinstance(columns, list):
        return frozenset()
    return frozenset(
        index for index, element_id in enumerate(columns) if str(element_id) in numeric_ids
    )


def _row_signature(
    rows: Sequence[Sequence[Any]],
    *,
    ordered: bool,
    numeric_columns: frozenset[int] = frozenset(),
) -> tuple[Any, ...]:
    normalized = tuple(
        tuple(
            _normalize_cell(value, numeric=index in numeric_columns)
            for index, value in enumerate(row)
        )
        for row in rows
    )
    return normalized if ordered else tuple(sorted(normalized, key=repr))


def _normalize_cell(value: Any, *, numeric: bool = False) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        try:
            numeric = Decimal(str(value)).normalize()
        except InvalidOperation:
            return ("text", str(value))
        return ("number", format(numeric, "f"))
    if numeric and isinstance(value, str):
        try:
            return ("number", format(Decimal(value).normalize(), "f"))
        except InvalidOperation:
            pass
    if isinstance(value, (date, datetime)):
        return ("date", value.isoformat())
    return ("text", str(value))


def _required_mapping(container: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_sequence(container: Mapping[str, Any], key: str) -> tuple[dict[str, Any], ...]:
    value = container.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{key} must be an object list")
    return tuple(value)


def _required_string(container: Mapping[str, Any], key: str) -> str:
    value = str(container.get(key) or "")
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _content_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _write_report(path: Path, content: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content.rstrip("\n"))
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()
