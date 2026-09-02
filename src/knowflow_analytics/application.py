from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from knowflow_analytics.catalog.release import ReleasePublisher
from knowflow_analytics.catalog.store import (
    CatalogError,
    CatalogStore,
    ProjectRecord,
    PublishedRelease,
)
from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionValueSpec,
    QueryRuleSpec,
    SemanticQuery,
    TermSpec,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.evaluation.contracts import (
    EvaluationReport,
    GoldenSuite,
    GoldenSuiteRecord,
)
from knowflow_analytics.evaluation.evaluator import GoldenEvaluator
from knowflow_analytics.execution.postgres import PostgresExecutor
from knowflow_analytics.hashing import content_hash, semantic_evidence_hash
from knowflow_analytics.modeling.ai_artifacts import (
    OneClickModelingArtifactService,
    reconcile_query_scopes,
)
from knowflow_analytics.modeling.ai_modeller import (
    AiSemanticModeller,
    ModelingCancelled,
    TableProgressCallback,
)
from knowflow_analytics.modeling.analysis_topics import (
    AnalysisTopicProposalSet,
    AnalysisTopicProposer,
    validate_analysis_topic_route,
)
from knowflow_analytics.modeling.catalog_compiler import (
    catalog_dataset_from_topic_command,
    catalog_table_model,
    compile_semantic_catalog,
    replace_catalog_item,
    replace_model_detail_item,
)
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetContract,
    DimensionContract,
    HierarchyContract,
    IdentifierContract,
    IdentifierType,
    MeasureContract,
    MetricContract,
    ModelContract,
    ModelDefineType,
    ModelDetailContract,
    ModelDimensionContract,
    ModelFieldContract,
    ModelRelationContract,
    QueryRuleContract,
    SemanticCatalog,
    SqlVariableContract,
)
from knowflow_analytics.modeling.catalog_editor import upsert_model_aggregate
from knowflow_analytics.modeling.contracts import (
    AiModelingArtifact,
    DimensionDictionaryApplyResult,
    DimensionDictionaryEligibilityStatus,
    DimensionDictionaryPolicy,
    DimensionDictionaryPreview,
    DimensionDictionaryStatus,
    DimensionValueDecision,
    DimensionValueListState,
    ModelingProposal,
    ModelingProposalApplyResult,
    ModelingProposalStatus,
    ModelingRevision,
    ModelingRunSource,
    ModelingRunStatus,
    ModelingSuggestionRun,
    RevisionState,
    SchemaSnapshot,
    SemanticAliasReview,
    SuggestionDecision,
    SuggestionSource,
    SuggestionState,
    TableCatalogEntry,
    TableSnapshot,
    semantic_context_content_hash,
)
from knowflow_analytics.modeling.deletion import (
    CatalogDeletionPlanner,
    DeletionImpact,
    ResourceKind,
)
from knowflow_analytics.modeling.diagnostics import (
    ModelingDiagnosticsAnalyzer,
    ModelingDiagnosticsReport,
)
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester
from knowflow_analytics.modeling.dimension_dictionary import (
    DimensionDictionaryBuilder,
    due_dictionary_refresh_groups,
    merge_complete_profiled_dimension_values,
)
from knowflow_analytics.modeling.dimension_dictionary_eligibility import (
    assess_dimension_dictionary_eligibility,
)
from knowflow_analytics.modeling.domain import DomainGovernance, DomainLifecycle
from knowflow_analytics.modeling.drift import SchemaDriftAnalyzer, SchemaDriftReport
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.modeling.jobs import (
    ModelingJob,
    ModelingJobProgress,
    ModelingJobStage,
    ModelingJobStatus,
    ModelingJobTable,
)
from knowflow_analytics.modeling.layout import (
    GraphNodePosition,
    GraphViewport,
    ModelGraphLayout,
    normalize_model_graph_layout,
    project_stored_layout,
)
from knowflow_analytics.modeling.product import (
    DecisionChoice,
    ModelingDecision,
    ModelingPlan,
    ModelingPlanApplyResult,
    ModelingPlanBuilder,
    ModelingPlanPhase,
    ModelingPlanStatus,
    ModelingResourceCounts,
    ModelingSummary,
    ScopeRecommendationBuilder,
    ScopeRecommendationSet,
)
from knowflow_analytics.modeling.profile import ColumnProfiler, TableProfile
from knowflow_analytics.modeling.profiler import PostgreSqlSemanticProfiler
from knowflow_analytics.modeling.proposal_defaults import (
    default_decisions,
    physical_comments_for,
)
from knowflow_analytics.modeling.quality import (
    MetricPreviewDecision,
    ModelingQualityReport,
    PostgreSqlModelingQualityProfiler,
    modeling_quality_report_is_stale,
)
from knowflow_analytics.modeling.relation_candidates import (
    synchronize_database_relation_candidates,
)
from knowflow_analytics.modeling.revision import (
    RevisionConflictError,
    RevisionEditor,
    unprocessed_suggestions,
)
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller
from knowflow_analytics.modeling.sql_model import validate_sql_model
from knowflow_analytics.query.contracts import (
    CompletedQueryResponse,
    QueryRequest,
    QueryResponse,
    QueryRowFilter,
    QueryState,
    QueryTraceStep,
    StructuredQueryRequest,
)
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector
from knowflow_analytics.query.diagnostics import (
    QUERY_DIAGNOSTIC_DEFAULT_TTL_SECONDS,
    QUERY_DIAGNOSTIC_MAX_RESULT_ROWS,
    QUERY_DIAGNOSTIC_MAX_TTL_SECONDS,
    QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE,
    QueryDiagnosticExport,
    QueryDiagnosticMetricAggregation,
    build_query_diagnostic_artifact,
    render_query_diagnostic_export,
)
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.intent_adjudicator import IntentAdjudicator
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.multi_turn import MultiTurnRewriter
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector
from knowflow_analytics.query.service import AnalyticsQueryService, ReleaseProvider
from knowflow_analytics.query.weak_metric_adjudicator import (
    WeakMetricAdjudicationMode,
    WeakMetricAdjudicator,
)
from knowflow_analytics.semantic.index import EmbeddingGateway, SemanticIndexBuilder
from knowflow_analytics.semantic.translator import SemanticTranslator

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _QueryDiagnosticRecord:
    request: QueryRequest | StructuredQueryRequest
    response: QueryResponse
    actor_id: str
    permission_scope_hash: str
    mode: Literal["natural", "structured"]
    revision: ModelingRevision | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.actor_id,
            self.request.project_id,
            self.permission_scope_hash,
            self.response.query_id,
        )


@dataclass(frozen=True)
class _QueuedQueryDiagnosticRecord:
    record: _QueryDiagnosticRecord
    generation: int
    completed: threading.Event


class _BoundedQueryDiagnosticRecorder:
    """One bounded best-effort side channel outside query response latency."""

    def __init__(
        self,
        *,
        persist: Callable[[_QueryDiagnosticRecord], None],
        purge: Callable[[], None],
        max_pending: int,
        purge_interval_seconds: float,
    ) -> None:
        self._persist = persist
        self._purge = purge
        self._purge_interval_seconds = purge_interval_seconds
        self._queue: queue.Queue[_QueuedQueryDiagnosticRecord | None] = queue.Queue(
            maxsize=max_pending
        )
        self._pending: dict[
            tuple[str, str, str, str],
            tuple[int, threading.Event],
        ] = {}
        self._next_generation = 0
        self._pending_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stopped = threading.Event()
        self._ensure_started()

    def submit(self, record: _QueryDiagnosticRecord) -> bool:
        if self._stopped.is_set():
            return False
        completed = threading.Event()
        with self._pending_lock:
            self._next_generation += 1
            generation = self._next_generation
            previous = self._pending.get(record.key)
            self._pending[record.key] = (generation, completed)
        queued = _QueuedQueryDiagnosticRecord(
            record=record,
            generation=generation,
            completed=completed,
        )
        try:
            self._queue.put_nowait(queued)
        except queue.Full:
            with self._pending_lock:
                if self._pending.get(record.key) == (generation, completed):
                    if previous is None:
                        self._pending.pop(record.key, None)
                    else:
                        self._pending[record.key] = previous
            completed.set()
            return False
        return True

    def wait_for(self, key: tuple[str, str, str, str], *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._pending_lock:
                latest = self._pending.get(key)
            if latest is None:
                return
            generation, completed = latest
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not completed.wait(timeout=remaining):
                return
            with self._pending_lock:
                current = self._pending.get(key)
            if current is None or current == (generation, completed):
                return

    def close(self) -> None:
        self._stopped.set()
        with suppress(queue.Full):
            self._queue.put_nowait(None)
        worker = self._worker
        if worker is not None:
            worker.join(timeout=1)

    def _ensure_started(self) -> None:
        if self._worker is not None:
            return
        with self._start_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._run,
                name="analytics-query-diagnostic-recorder",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            if self._stopped.is_set() and self._queue.empty():
                return
            try:
                queued = self._queue.get(timeout=self._purge_interval_seconds)
            except queue.Empty:
                try:
                    self._purge()
                except Exception as exc:  # noqa: BLE001 - retention remains best effort
                    LOGGER.warning(
                        "query diagnostic expiry purge failed error_type=%s",
                        type(exc).__name__,
                    )
                continue
            if queued is None:
                self._queue.task_done()
                return
            record = queued.record
            try:
                self._persist(record)
            except Exception as exc:  # noqa: BLE001 - diagnostics never alter query responses
                LOGGER.warning(
                    "query diagnostic persistence failed error_type=%s",
                    type(exc).__name__,
                )
            finally:
                queued.completed.set()
                with self._pending_lock:
                    if self._pending.get(record.key) == (
                        queued.generation,
                        queued.completed,
                    ):
                        self._pending.pop(record.key, None)
                self._queue.task_done()


class _StagedReleaseProvider:
    def __init__(self, published: PublishedRelease) -> None:
        self._published = published

    def get_active_release(self, project_id: str) -> PublishedRelease:
        if self._published.release.project_id != project_id:
            raise ValueError("staged release belongs to another project")
        return self._published


def _table_structure(table: TableSnapshot) -> dict[str, object]:
    """Return only query-relevant schema facts for expansion drift detection."""

    return {
        "schema_name": table.schema_name,
        "name": table.name,
        "source_type": table.source_type,
        "columns": tuple(
            (
                column.name,
                column.data_type,
                column.nullable,
                column.ordinal_position,
                column.primary_key,
                column.unique,
            )
            for column in table.columns
        ),
        "foreign_keys": tuple(
            sorted(
                (
                    foreign_key.constrained_columns,
                    foreign_key.referred_schema,
                    foreign_key.referred_table,
                    foreign_key.referred_columns,
                )
                for foreign_key in table.foreign_keys
            )
        ),
    }


def _diagnostic_index_snapshot_projection(index_snapshot) -> dict[str, object]:
    """Expose index provenance/counts, never embedding vectors or entry text."""

    by_element_type: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for entry in index_snapshot.entries:
        element_type = entry.element_type.value
        by_element_type[element_type] = by_element_type.get(element_type, 0) + 1
        by_source[entry.source] = by_source.get(entry.source, 0) + 1
    return {
        "id": index_snapshot.id,
        "release_spec_hash": index_snapshot.release_spec_hash,
        "content_hash": index_snapshot.content_hash,
        "state": index_snapshot.state.value,
        "embedding_model_id": index_snapshot.embedding_model_id,
        "vector_dimension": index_snapshot.vector_dimension,
        "entry_count": len(index_snapshot.entries),
        "entry_counts_by_element_type": dict(sorted(by_element_type.items())),
        "entry_counts_by_source": dict(sorted(by_source.items())),
    }


def _diagnostic_published_release_projection(
    published: PublishedRelease,
) -> dict[str, object]:
    return {
        "status": published.status,
        "release": published.release.model_dump(mode="json"),
        "index_snapshot": _diagnostic_index_snapshot_projection(published.index_snapshot),
    }


def _with_frozen_dimension_values(
    revision: ModelingRevision, dimension_values: tuple[DimensionValueSpec, ...]
) -> ModelingRevision:
    """把产物里冻结的维度值放回预览 Catalog，得到与 create 时相同的 Candidate。

    只覆盖仍然存在的维度：用户若在草稿里取消了某个字段的维度分类，那份值
    就该消失，materialize 会照常判过期并重建 —— 那是真的改动。
    """

    catalog = revision.semantic_catalog
    if catalog is None or not dimension_values:
        return revision
    known = {item.id for item in catalog.dimensions}
    kept = tuple(item for item in dimension_values if item.dimension_id in known)
    if not kept:
        return revision
    updated = catalog.model_copy(update={"dimension_values": kept})
    return revision.model_copy(
        update={
            "semantic_catalog": updated,
            "semantic_spec": compile_semantic_catalog(updated),
        }
    )


class AnalyticsApplication:
    """Authoritative modeling/query use cases shared by every API client.

    A web UI, DeepAgent adapter, or acceptance CLI may call this boundary, but no
    client owns separate semantic-modeling rules or state.
    """

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        introspector: PostgreSqlIntrospector,
        executor: PostgresExecutor,
        embedding_gateway: EmbeddingGateway,
        ai_modeller: AiSemanticModeller | None = None,
        dimension_alias_suggester: DimensionValueAliasSuggester | None = None,
        semantic_profiler: PostgreSqlSemanticProfiler | None = None,
        column_profiler: ColumnProfiler | None = None,
        quality_profiler: PostgreSqlModelingQualityProfiler | None = None,
        llm_parser: LlmS2SqlParser | None = None,
        textual_corrector: TextualS2SqlCorrector | None = None,
        physical_sql_corrector: LlmPhysicalSqlCorrector | None = None,
        multi_turn_rewriter: MultiTurnRewriter | None = None,
        require_evaluation_for_publish: bool = True,
        minimum_evaluation_cases: int = 30,
        minimum_accuracy: float = 1.0,
        require_quality_report_for_publish: bool = True,
        dry_run_before_execute: bool = False,
        modeling_job_workers: int = 2,
        modeling_max_concurrency: int | None = None,
        selection_secret: str | bytes | None = None,
        weak_metric_adjudicator: WeakMetricAdjudicator | None = None,
        weak_metric_adjudication_mode: WeakMetricAdjudicationMode | str = (
            WeakMetricAdjudicationMode.OFF
        ),
        intent_adjudicator: IntentAdjudicator | None = None,
        semantic_intent_adjudication_mode: WeakMetricAdjudicationMode | str = (
            WeakMetricAdjudicationMode.SHADOW
        ),
        analysis_object_adjudication_mode: WeakMetricAdjudicationMode | str = (
            WeakMetricAdjudicationMode.OFF
        ),
        confirmation_memory_ttl_seconds: int = 2_592_000,
        query_diagnostic_ttl_seconds: int = QUERY_DIAGNOSTIC_DEFAULT_TTL_SECONDS,
        query_diagnostic_result_rows: int = 0,
        query_diagnostic_queue_size: int = 64,
        query_diagnostic_export_wait_seconds: float = 0.5,
        query_diagnostic_purge_interval_seconds: float = 60.0,
    ) -> None:
        self.catalog = catalog
        self._introspector = introspector
        self._executor = executor
        self._embedding_gateway = embedding_gateway
        self._ai_modeller = ai_modeller
        self._ai_artifact_service = OneClickModelingArtifactService(
            ai_modeller=ai_modeller,
            dimension_alias_suggester=dimension_alias_suggester,
            max_concurrency=modeling_max_concurrency,
        )
        self._semantic_profiler = semantic_profiler
        self._column_profiler = column_profiler
        self._quality_profiler = quality_profiler
        self._llm_parser = llm_parser
        self._textual_corrector = textual_corrector
        self._physical_sql_corrector = physical_sql_corrector
        self._multi_turn_rewriter = multi_turn_rewriter
        self._rule_modeller = RuleSemanticModeller()
        self._scope_recommendation_builder = ScopeRecommendationBuilder()
        self._modeling_plan_builder = ModelingPlanBuilder()
        self._revision_editor = RevisionEditor()
        self._dimension_dictionary_builder = DimensionDictionaryBuilder(
            alias_suggester=dimension_alias_suggester
        )
        self._diagnostics_analyzer = ModelingDiagnosticsAnalyzer(self._revision_editor)
        self._schema_drift_analyzer = SchemaDriftAnalyzer()
        self._deletion_planner = CatalogDeletionPlanner()
        self._index_builder = SemanticIndexBuilder(embedding_gateway)
        self._publisher = ReleasePublisher(
            catalog=catalog,
            revision_editor=self._revision_editor,
            index_builder=self._index_builder,
            require_evaluation=require_evaluation_for_publish,
            minimum_evaluation_cases=minimum_evaluation_cases,
            minimum_accuracy=minimum_accuracy,
            require_quality_report=require_quality_report_for_publish,
        )
        self._dry_run_before_execute = dry_run_before_execute
        self._selection_secret = selection_secret or secrets.token_bytes(32)
        self._weak_metric_adjudicator = weak_metric_adjudicator
        self._weak_metric_adjudication_mode = WeakMetricAdjudicationMode(
            weak_metric_adjudication_mode
        )
        self._intent_adjudicator = intent_adjudicator
        self._semantic_intent_adjudication_mode = WeakMetricAdjudicationMode(
            semantic_intent_adjudication_mode
        )
        self._analysis_object_adjudication_mode = WeakMetricAdjudicationMode(
            analysis_object_adjudication_mode
        )
        self._confirmation_memory_ttl_seconds = confirmation_memory_ttl_seconds
        if not 0 < query_diagnostic_ttl_seconds <= QUERY_DIAGNOSTIC_MAX_TTL_SECONDS:
            raise ValueError("query diagnostic ttl is outside its retention limit")
        if not 0 <= query_diagnostic_result_rows <= QUERY_DIAGNOSTIC_MAX_RESULT_ROWS:
            raise ValueError("query diagnostic result sample limit is invalid")
        if not 1 <= query_diagnostic_queue_size <= 1_000:
            raise ValueError("query diagnostic queue size is invalid")
        if not 0 <= query_diagnostic_export_wait_seconds <= 2:
            raise ValueError("query diagnostic export wait is invalid")
        if not 0.01 <= query_diagnostic_purge_interval_seconds <= 3_600:
            raise ValueError("query diagnostic purge interval is invalid")
        self._query_diagnostic_ttl_seconds = query_diagnostic_ttl_seconds
        self._query_diagnostic_result_rows = query_diagnostic_result_rows
        self._query_diagnostic_export_wait_seconds = query_diagnostic_export_wait_seconds
        self._query_diagnostic_recorder = _BoundedQueryDiagnosticRecorder(
            persist=self._persist_query_diagnostic,
            purge=lambda: self.catalog.purge_expired_query_diagnostics(
                batch_size=QUERY_DIAGNOSTIC_PURGE_BATCH_SIZE
            ),
            max_pending=query_diagnostic_queue_size,
            purge_interval_seconds=query_diagnostic_purge_interval_seconds,
        )
        # AI 建模 job 的执行池。单进程部署，与 Deep Agent "单 Worker 进程内 MVP"
        # 一致；多副本时换成基于 DB 的 claim 型 worker，job 表结构已为此留好。
        # 2 个 worker：一个项目同时跑两轮建模已经够用，更多只会把模型网关打满。
        self._job_executor = ThreadPoolExecutor(
            max_workers=modeling_job_workers, thread_name_prefix="analytics-modeling-job"
        )
        self._cancel_requests: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._query_service = self._build_query_service(catalog)

    def close(self) -> None:
        """Stop the bounded diagnostic side channel during service shutdown."""

        self._query_diagnostic_recorder.close()

    def create_project(self, *, name: str, project_id: str | None = None) -> ProjectRecord:
        return self.catalog.create_project(name=name, project_id=project_id)

    def get_domain_governance(self, project_id: str) -> DomainGovernance:
        return self.catalog.get_domain_governance(project_id)

    def update_domain_governance(
        self,
        *,
        project_id: str,
        expected_etag: int,
        classifications: tuple[str, ...],
        lifecycle: DomainLifecycle,
        updated_by: str,
        parent_project_id: str | None = None,
    ) -> DomainGovernance:
        return self.catalog.update_domain_governance(
            project_id=project_id,
            expected_etag=expected_etag,
            classifications=classifications,
            lifecycle=lifecycle,
            updated_by=updated_by,
            parent_project_id=parent_project_id,
        )

    def get_model_graph_layout(
        self,
        *,
        project_id: str,
        revision_id: str,
    ) -> ModelGraphLayout:
        revision = self.catalog.get_revision(revision_id)
        if revision.project_id != project_id:
            raise SemanticValidationError(
                "model graph layout belongs to another project",
                code="MODEL_GRAPH_LAYOUT_NOT_FOUND",
            )
        stored = self.catalog.get_model_graph_layout(
            project_id=project_id,
            revision_id=revision_id,
        )
        return project_stored_layout(
            stored=stored,
            model_ids=tuple(sorted(item.id for item in revision.semantic_spec.models)),
            project_id=project_id,
            revision_id=revision_id,
        )

    def update_model_graph_layout(
        self,
        *,
        project_id: str,
        revision_id: str,
        expected_etag: int,
        positions: tuple[GraphNodePosition, ...],
        viewport: GraphViewport,
        updated_by: str,
    ) -> ModelGraphLayout:
        revision = self.catalog.get_revision(revision_id)
        if revision.project_id != project_id:
            raise SemanticValidationError(
                "model graph layout belongs to another project",
                code="MODEL_GRAPH_LAYOUT_NOT_FOUND",
            )
        layout = normalize_model_graph_layout(
            layout=ModelGraphLayout(
                project_id=project_id,
                revision_id=revision_id,
                etag=expected_etag,
                positions=positions,
                viewport=viewport,
                updated_by=updated_by,
                updated_at=datetime.now(UTC),
            ),
            model_ids=tuple(item.id for item in revision.semantic_spec.models),
        )
        return self.catalog.save_model_graph_layout(layout, expected_etag=expected_etag)

    def list_datasource_schemas(self, *, project_id: str) -> tuple[str, ...]:
        self.catalog.get_project(project_id)
        return self._introspector.list_schemas()

    def list_datasource_tables(
        self,
        *,
        project_id: str,
        schema_name: str,
        include_views: bool = False,
    ) -> tuple[TableCatalogEntry, ...]:
        self.catalog.get_project(project_id)
        return self._introspector.list_tables(
            schema_name=schema_name,
            include_views=include_views,
        )

    def describe_datasource_table(
        self,
        *,
        project_id: str,
        schema_name: str,
        table_name: str,
        include_views: bool = False,
    ) -> TableSnapshot:
        self.catalog.get_project(project_id)
        return self._introspector.describe_table(
            schema_name=schema_name,
            table_name=table_name,
            include_views=include_views,
        )

    def get_scope_recommendations(
        self,
        *,
        project_id: str,
        datasource_id: str,
        schema_name: str,
        include_views: bool = False,
    ) -> ScopeRecommendationSet:
        self.catalog.get_project(project_id)
        entries = self._introspector.list_tables(
            schema_name=schema_name,
            include_views=include_views,
        )
        tables = tuple(
            self._introspector.describe_table(
                schema_name=item.schema_name,
                table_name=item.name,
                include_views=include_views,
            )
            for item in entries
        )
        return self._scope_recommendation_builder.build(
            project_id=project_id,
            datasource_id=datasource_id,
            schema_name=schema_name,
            tables=tables,
        )

    def create_schema_snapshot(
        self,
        *,
        project_id: str,
        schemas: Sequence[str],
        selected_tables: Mapping[str, Sequence[str]] | None = None,
        include_views: bool = False,
    ):
        self.catalog.get_project(project_id)
        snapshot = self._introspector.scan(
            schemas=schemas,
            selected_tables=selected_tables,
            include_views=include_views,
        )
        self.catalog.save_schema_snapshot(project_id=project_id, snapshot=snapshot)
        return snapshot

    def create_empty_revision(
        self,
        *,
        project_id: str,
        schema_snapshot_id: str,
    ) -> ModelingRevision:
        self.catalog.get_project(project_id)
        snapshot = self.catalog.get_schema_snapshot(schema_snapshot_id, project_id=project_id)
        revision_id = f"rev_{uuid.uuid4().hex}"
        semantic_catalog = SemanticCatalog(
            project_id=project_id,
            revision_id=revision_id,
        )
        revision = self._revision_editor.create(
            project_id=project_id,
            schema_snapshot_hash=snapshot.content_hash,
            semantic_catalog=semantic_catalog,
        )
        self.catalog.save_revision(revision)
        return revision

    def add_table_model(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        schema_name: str,
        table_name: str,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        snapshot_id = f"schema_{schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        snapshot = self.catalog.get_schema_snapshot(snapshot_id, project_id=revision.project_id)
        baseline = self._rule_modeller.build(
            project_id=revision.project_id,
            snapshot=snapshot,
            create_default_dataset=False,
            # 不在导入时画像：API-first 路径只取这里的 ModelSpec 和主外键，字段分类
            # 建议被丢掉。画像在 AI 建模时做一次（create_ai_suggestion_run）。
        )
        model = next(
            (
                item
                for item in baseline.semantic_spec.models
                if item.schema_name == schema_name and item.table == table_name
            ),
            None,
        )
        if model is None:
            raise SemanticValidationError(
                "selected table is not present in the schema snapshot",
                code="TABLE_NOT_IN_SNAPSHOT",
            )
        table = next(
            item
            for item in snapshot.tables
            if item.schema_name == schema_name and item.name == table_name
        )
        if any(item.id == model.id for item in revision.semantic_catalog.models):
            raise RevisionConflictError(f"model already exists: {model.id}")
        foreign_columns = {
            column
            for foreign_key in table.foreign_keys
            for column in foreign_key.constrained_columns
        }
        identifiers = tuple(
            IdentifierContract(
                name=column.comment or column.name,
                type=(IdentifierType.PRIMARY if column.primary_key else IdentifierType.FOREIGN),
                biz_name=column.name,
                is_create_dimension=0,
            )
            for column in table.columns
            if column.primary_key or column.name in foreign_columns
        )
        catalog_model = catalog_table_model(
            model_id=model.id,
            schema_name=schema_name,
            table_name=table_name,
            description=table.comment,
            fields=tuple(
                ModelFieldContract(field_name=column.name, data_type=column.data_type)
                for column in table.columns
            ),
            identifiers=identifiers,
        )
        semantic_catalog = revision.semantic_catalog.model_copy(
            update={"models": revision.semantic_catalog.models + (catalog_model,)}
        )
        semantic_catalog = SemanticCatalog.model_validate(
            semantic_catalog.model_dump(mode="python")
        )
        semantic_catalog = synchronize_database_relation_candidates(
            catalog=semantic_catalog,
            snapshot=snapshot,
            changed_model_ids=frozenset({catalog_model.id}),
        )
        updated = self._revision_editor.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=semantic_catalog,
            # The model create form saves the reviewed ModelDetail resource;
            # it does not attach a second DecisionQueue to the domain. Database
            # identifiers are physical facts above, while inferred classifications
            # remain form-prefill suggestions until a resource save explicitly
            # persists them. Existing reviewed suggestions remain version-bound.
            suggestions=revision.suggestions,
        )
        self.catalog.update_revision(updated, previous_etag=revision.etag)
        return updated

    def add_sql_model(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        name: str,
        biz_name: str,
        description: str,
        sql_query: str,
        sql_variables: tuple[SqlVariableContract, ...] = (),
        model_id: str | None = None,
    ) -> ModelingRevision:
        """Create a SQL-backed ModelDetail from the modelling form."""

        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        catalog = self._require_semantic_catalog(revision)
        rendered = validate_sql_model(
            sql_query,
            tuple(item.model_dump(mode="json", by_alias=True) for item in sql_variables),
        )
        columns = self._introspector.describe_query(rendered)
        if not columns:
            raise SemanticValidationError(
                "SQL model query exposes no columns",
                code="SQL_MODEL_EMPTY_PROJECTION",
            )
        identifier = model_id or f"sql_model_{uuid.uuid4().hex}"
        if any(item.id == identifier for item in catalog.models):
            raise RevisionConflictError(f"model already exists: {identifier}")
        model = ModelContract(
            id=identifier,
            name=name,
            biz_name=biz_name,
            description=description,
            source_type="sql",
            model_detail=ModelDetailContract(
                query_type=ModelDefineType.SQL_QUERY,
                db_type="POSTGRESQL",
                sql_query=sql_query.strip().removesuffix(";").rstrip(),
                fields=tuple(
                    ModelFieldContract(
                        field_name=item.name,
                        data_type=item.data_type,
                    )
                    for item in columns
                ),
                sql_variables=sql_variables,
            ),
        )
        updated_catalog = replace_catalog_item(
            catalog,
            collection="models",
            item=model,
        )
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
        )

    def rollback_active_release(self, *, project_id: str) -> str:
        """Switch production back to the previously published release."""

        return self.catalog.rollback_active_release(project_id=project_id)

    def derive_candidate_revision(self, *, revision_id: str) -> ModelingRevision:
        """Open a fresh editable candidate that inherits an existing revision.

        A frozen or published revision is immutable (`_require_editable`), and the
        UI previously offered no way forward: the validation gate only stated that
        the state forbids re-validation. Deriving keeps the source untouched and
        records lineage through ``parent_revision_id``, which is the same shape
        ``extend_revision_tables`` already uses for its child revision.
        """

        source = self.catalog.get_revision(revision_id)
        if source.semantic_catalog is None:
            raise SemanticValidationError(
                "revision has no governed catalog to derive from",
                code="REVISION_NOT_DERIVABLE",
            )
        child_revision_id = f"rev_{uuid.uuid4().hex}"
        child_catalog = SemanticCatalog.model_validate(
            source.semantic_catalog.model_copy(
                update={"revision_id": child_revision_id}
            ).model_dump(mode="python")
        )
        child = self._revision_editor.create(
            project_id=source.project_id,
            schema_snapshot_hash=source.schema_snapshot_hash,
            semantic_catalog=child_catalog,
            suggestions=source.suggestions,
            parent_revision_id=source.id,
        )
        self.catalog.save_revision(child)
        return child

    def extend_revision_tables(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        selected_tables: Mapping[str, Sequence[str]],
        include_views: bool = False,
    ) -> ModelingRevision:
        """Create a child revision whose snapshot and catalog include new tables.

        Saving the model create form adds another model inside the same domain.
        This keeps that product behavior while keeping released revisions
        immutable: added models are written to a child candidate, never into the source.
        """

        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before extending tables")
        if revision.schema_snapshot_hash != schema_snapshot_hash:
            raise RevisionConflictError("schema snapshot changed; reload before extending tables")
        source_snapshot_id = f"schema_{revision.schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        source_snapshot = self.catalog.get_schema_snapshot(
            source_snapshot_id,
            project_id=revision.project_id,
        )
        source_tables = {(table.schema_name, table.name): table for table in source_snapshot.tables}
        requested_tables = {
            (str(schema).strip(), str(table).strip())
            for schema, tables in selected_tables.items()
            for table in tables
        }
        new_table_keys = requested_tables.difference(source_tables)
        if not new_table_keys:
            raise SemanticValidationError(
                "selected tables already belong to the revision snapshot",
                code="NO_NEW_TABLES",
            )

        expanded_scope: dict[str, list[str]] = {}
        for schema_name, table_name in sorted(set(source_tables) | requested_tables):
            expanded_scope.setdefault(schema_name, []).append(table_name)
        expanded_snapshot = self._introspector.scan(
            schemas=tuple(expanded_scope),
            selected_tables={schema: tuple(tables) for schema, tables in expanded_scope.items()},
            include_views=include_views
            or any(table.source_type == "view" for table in source_snapshot.tables),
        )
        expanded_tables = {
            (table.schema_name, table.name): table for table in expanded_snapshot.tables
        }
        missing_tables = (set(source_tables) | requested_tables).difference(expanded_tables)
        if missing_tables:
            names = ", ".join(f"{schema}.{table}" for schema, table in sorted(missing_tables))
            raise SemanticValidationError(
                f"selected tables are no longer available: {names}",
                code="TABLE_NOT_IN_SNAPSHOT",
            )
        drifted_tables = [
            key
            for key, previous in source_tables.items()
            if _table_structure(previous) != _table_structure(expanded_tables[key])
        ]
        if drifted_tables:
            names = ", ".join(f"{schema}.{table}" for schema, table in sorted(drifted_tables))
            raise SemanticValidationError(
                f"existing table schema changed and requires explicit reconciliation: {names}",
                code="EXISTING_TABLE_SCHEMA_DRIFT",
            )

        baseline = self._rule_modeller.build(
            project_id=revision.project_id,
            snapshot=expanded_snapshot,
            create_default_dataset=False,
            # 不在导入时画像：API-first 路径只取这里的 ModelSpec 和主外键，字段分类
            # 建议被丢掉。画像在 AI 建模时做一次（create_ai_suggestion_run）。
        )
        baseline_models = {
            (model.schema_name, model.table): model for model in baseline.semantic_spec.models
        }
        added_catalog_models: list[ModelContract] = []
        for key in sorted(new_table_keys):
            model = baseline_models.get(key)
            if model is None:
                raise SemanticValidationError(
                    f"selected table is not present in the expanded snapshot: {key[0]}.{key[1]}",
                    code="TABLE_NOT_IN_SNAPSHOT",
                )
            table = expanded_tables[key]
            foreign_columns = {
                column
                for foreign_key in table.foreign_keys
                for column in foreign_key.constrained_columns
            }
            identifiers = tuple(
                IdentifierContract(
                    name=column.comment or column.name,
                    type=(IdentifierType.PRIMARY if column.primary_key else IdentifierType.FOREIGN),
                    biz_name=column.name,
                    is_create_dimension=0,
                )
                for column in table.columns
                if column.primary_key or column.name in foreign_columns
            )
            added_catalog_models.append(
                catalog_table_model(
                    model_id=model.id,
                    schema_name=key[0],
                    table_name=key[1],
                    description=table.comment,
                    fields=tuple(
                        ModelFieldContract(
                            field_name=column.name,
                            data_type=column.data_type,
                        )
                        for column in table.columns
                    ),
                    identifiers=identifiers,
                )
            )

        child_revision_id = f"rev_{uuid.uuid4().hex}"
        child_catalog = revision.semantic_catalog.model_copy(
            update={
                "revision_id": child_revision_id,
                "models": (
                    *revision.semantic_catalog.models,
                    *added_catalog_models,
                ),
            }
        )
        child_catalog = SemanticCatalog.model_validate(child_catalog.model_dump(mode="python"))
        child_catalog = synchronize_database_relation_candidates(
            catalog=child_catalog,
            snapshot=expanded_snapshot,
            changed_model_ids=frozenset(item.id for item in added_catalog_models),
        )
        child_revision = self._revision_editor.create(
            project_id=revision.project_id,
            schema_snapshot_hash=expanded_snapshot.content_hash,
            semantic_catalog=child_catalog,
            suggestions=revision.suggestions,
            parent_revision_id=revision.id,
        )
        self.catalog.save_schema_snapshot(
            project_id=revision.project_id,
            snapshot=expanded_snapshot,
        )
        self.catalog.save_revision(child_revision)
        return child_revision

    def create_ai_suggestion_run(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        manifest_hash: str | None = None,
        source: ModelingRunSource = ModelingRunSource.API,
        source_task_id: str | None = None,
        persist: bool = True,
        tenant_id: str = "",
        progress: TableProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ModelingSuggestionRun:
        """Generate auditable form-prefill patches without changing the revision."""

        if self._ai_modeller is None:
            raise ValueError("AI modeling gateway is not configured")
        if manifest_hash is not None and source_task_id is None:
            raise SemanticValidationError(
                "knowledge-backed AI suggestions require a server-bound knowledge scope",
                code="KNOWLEDGE_SCOPE_REQUIRED",
            )
        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError(
                "revision etag changed; reload before generating suggestions"
            )
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(
                f"{revision.state.value} revisions cannot generate editable suggestions"
            )
        snapshot_id = f"schema_{revision.schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        snapshot = self.catalog.get_schema_snapshot(snapshot_id, project_id=revision.project_id)
        run_id = f"modeling_run_{uuid.uuid4().hex}"
        suggestions = self._ai_modeller.suggest(
            modeling_job_id=source_task_id or run_id,
            revision=revision,
            snapshot=snapshot,
            manifest_hash=manifest_hash,
            tenant_id=tenant_id,
            progress=progress,
            should_stop=should_stop,
            # S1 画像：给 S3 护栏用。没配 profiler 时为空，护栏退回列名规则。
            profiles=self._profile_tables(snapshot),
        )
        # Suggestion ids are stable by design, so a re-run regenerates ids the user
        # already settled and apply_suggestion_run would reject the whole batch.
        # Offer only what is still open; a still-pending suggestion stays on offer.
        suggestions = unprocessed_suggestions(suggestions, revision.suggestions)
        run = ModelingSuggestionRun(
            id=run_id,
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            source=source,
            source_task_id=source_task_id,
            manifest_hash=manifest_hash,
            input_hash=content_hash(
                {
                    "revision_id": revision.id,
                    "revision_etag": revision.etag,
                    "schema_snapshot_hash": revision.schema_snapshot_hash,
                    "semantic_spec_hash": revision.semantic_spec.spec_hash,
                    "manifest_hash": manifest_hash,
                }
            ),
            suggestions=suggestions,
            created_at=datetime.now(UTC),
        )
        if persist:
            self.catalog.save_modeling_run(run)
        return run

    def suggest_resource_aliases(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        resource_type: Literal["dimension", "metric"],
        model_id: str,
        name: str,
        biz_name: str,
        description: str,
        existing_aliases: tuple[str, ...] = (),
        tenant_id: str = "",
    ) -> dict[str, object]:
        """Return form alias candidates without writing the revision."""

        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before generating aliases")
        model = next(
            (item for item in revision.semantic_spec.models if item.id == model_id),
            None,
        )
        if model is None:
            raise SemanticValidationError("model was not found", code="MODEL_NOT_FOUND")
        if self._ai_modeller is None:
            raise SemanticValidationError(
                "AI alias suggestion is not configured",
                code="AI_MODELLER_DISABLED",
            )
        output = self._ai_modeller.suggest_aliases(
            resource_type=resource_type,
            name=name,
            biz_name=biz_name,
            description=description,
            model_name=model.name,
            existing_aliases=existing_aliases,
            trace={
                "revision_id": revision.id,
                "model_id": model.id,
                "resource_type": resource_type,
                "revision_etag": str(revision.etag),
                "tenant_id": tenant_id,
            },
        )
        return {
            "aliases": output.aliases,
            "revision_id": revision.id,
            "revision_etag": revision.etag,
            "input_hash": content_hash(
                {
                    "resource_type": resource_type,
                    "model_id": model_id,
                    "name": name,
                    "biz_name": biz_name,
                    "description": description,
                    "existing_aliases": existing_aliases,
                }
            ),
        }

    def get_modeling_run(self, run_id: str) -> ModelingSuggestionRun:
        return self.catalog.get_modeling_run(run_id)

    # ---- AI 建模异步 job ---------------------------------------------------
    #
    # 此前整条链路同步：一次 HTTP 请求跑完两个 LLM 扇出阶段，唯一的 DB 写在最后。
    # 进程重启、客户端断开、BFF 超时都让已完成的 LLM 工作全部丢失。job 让执行与
    # 请求解耦：POST 立即返回 id，每张表完成即落盘进度，客户端断开不影响执行。

    def start_ai_modeling_job(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        created_by: str,
        manifest_hash: str | None = None,
        source: ModelingRunSource = ModelingRunSource.API,
        source_task_id: str | None = None,
        tenant_id: str = "",
    ) -> ModelingJob:
        if self._ai_modeller is None:
            raise ValueError("AI modeling gateway is not configured")
        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before modeling")
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(f"{revision.state.value} revisions are immutable")
        tables = tuple(
            ModelingJobTable(model_id=model.id, name=model.name)
            for model in revision.semantic_spec.models
            if model.query_type == "table_query"
        )
        now = datetime.now(UTC)
        job = ModelingJob(
            id=f"modeling_job_{uuid.uuid4().hex}",
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            progress=ModelingJobProgress(tables=tables),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.catalog.save_modeling_job(job)
        self._job_executor.submit(
            self._run_ai_modeling_job,
            job.id,
            revision_id=revision_id,
            expected_etag=expected_etag,
            created_by=created_by,
            manifest_hash=manifest_hash,
            source=source,
            source_task_id=source_task_id,
            tenant_id=tenant_id,
        )
        return job

    def get_modeling_job(self, job_id: str) -> ModelingJob:
        return self.catalog.get_modeling_job(job_id)

    def cancel_modeling_job(self, job_id: str) -> ModelingJob:
        """协作式取消：不会中断正在进行的模型调用，只保证不再发起新的表。"""

        job = self.catalog.get_modeling_job(job_id)
        if job.is_terminal:
            return job
        with self._cancel_lock:
            self._cancel_requests.add(job_id)
        if job.status is ModelingJobStatus.QUEUED:
            # 还没开跑，直接终态；runner 启动时也会再查一次。
            return self._finish_job(job, status=ModelingJobStatus.CANCELLED)
        return job

    def _should_stop(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel_requests

    def _run_ai_modeling_job(self, job_id: str, **kwargs) -> None:
        job = self.catalog.get_modeling_job(job_id)
        if self._should_stop(job_id):
            self._finish_job(job, status=ModelingJobStatus.CANCELLED)
            return
        job = self._touch_job(job, status=ModelingJobStatus.RUNNING)
        # progress 回调在模型构建线程里被并发调用；每次读-改-写整行，用锁串行化。
        progress_lock = threading.Lock()

        def on_table(model_id: str, _name: str, status: str, error: str | None) -> None:
            with progress_lock:
                current = self.catalog.get_modeling_job(job_id)
                table = next((t for t in current.progress.tables if t.model_id == model_id), None)
                attempts = (table.attempts if table else 0) + (1 if status == "running" else 0)
                self._touch_job(
                    current,
                    progress=current.progress.with_table(
                        model_id, status=status, error=error, attempts=attempts
                    ),
                )

        def on_stage(stage: str) -> None:
            with progress_lock:
                self._touch_job(
                    self.catalog.get_modeling_job(job_id), stage=ModelingJobStage(stage)
                )

        try:
            proposal = self.create_ai_modeling_proposal(
                progress=on_table,
                should_stop=lambda: self._should_stop(job_id),
                on_stage=on_stage,
                **kwargs,
            )
        except ModelingCancelled:
            self._finish_job(
                self.catalog.get_modeling_job(job_id), status=ModelingJobStatus.CANCELLED
            )
        except Exception as exc:  # 任何失败都要落盘，否则 job 永远 running
            logging.getLogger(__name__).exception("AI modeling job failed job_id=%s", job_id)
            self._finish_job(
                self.catalog.get_modeling_job(job_id),
                status=ModelingJobStatus.FAILED,
                error=f"{getattr(exc, 'code', exc.__class__.__name__)}: {exc}"[:2_000],
            )
        else:
            self._finish_job(
                self.catalog.get_modeling_job(job_id),
                status=ModelingJobStatus.COMPLETED,
                proposal_id=proposal.id,
            )
        finally:
            with self._cancel_lock:
                self._cancel_requests.discard(job_id)

    def _touch_job(self, job: ModelingJob, **changes) -> ModelingJob:
        updated = job.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
        self.catalog.update_modeling_job(updated)
        return updated

    def _finish_job(self, job: ModelingJob, *, status: ModelingJobStatus, **changes) -> ModelingJob:
        return self._touch_job(job, status=status, stage=ModelingJobStage.DONE, **changes)

    def create_ai_modeling_proposal(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        created_by: str,
        manifest_hash: str | None = None,
        source: ModelingRunSource = ModelingRunSource.API,
        source_task_id: str | None = None,
        tenant_id: str = "",
        progress: TableProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> ModelingProposal:
        """Generate one complete, editable AI draft without changing Catalog state.

        Upstream parity: ``ModelServiceImpl.buildModelSchema`` returns all table
        ``ModelSchema`` results before ``createModelBatch`` converts them.  The
        persisted proposal is a KnowFlow accuracy extension that restores the
        missing human review boundary without creating a second semantic Catalog.
        """

        revision = self.catalog.get_revision(revision_id)
        if on_stage is not None:
            on_stage("modeling")
        run = self.create_ai_suggestion_run(
            revision_id=revision_id,
            expected_etag=expected_etag,
            manifest_hash=manifest_hash,
            source=source,
            source_task_id=source_task_id,
            persist=False,
            tenant_id=tenant_id,
            progress=progress,
            should_stop=should_stop,
        )
        # 按与前端相同的默认规则预建产物。此前按"全接受"建，前端第一次保存
        # 就因决策不同触发产物重建（再调一次模型）并强制重新核对。
        # 快照注释用于识别"description 是导入抄来的注释"：抄来的不算人工内容。
        # 快照取不到时保守降级（当人工内容处理），不让默认决策失败整个提案。
        try:
            snapshot_id = f"schema_{revision.schema_snapshot_hash.removeprefix('sha256:')[:16]}"
            proposal_snapshot = self.catalog.get_schema_snapshot(
                snapshot_id, project_id=revision.project_id
            )
            comments = physical_comments_for(
                revision.semantic_spec.models,
                revision.semantic_spec.fields,
                proposal_snapshot,
            )
        except CatalogError:
            comments = None
        decisions = default_decisions(revision, run.suggestions, physical_comments=comments)
        preview_revision = self._preview_ai_modeling_proposal(
            revision=revision,
            run=run,
            decisions=decisions,
            profile_values=True,
        )
        if should_stop is not None and should_stop():
            raise ModelingCancelled("enriching")
        if on_stage is not None:
            on_stage("enriching")
        artifact = self._ai_artifact_service.build(preview_revision, tenant_id=tenant_id)
        now = datetime.now(UTC)
        proposal = ModelingProposal(
            id=f"modeling_proposal_{uuid.uuid4().hex}",
            project_id=revision.project_id,
            revision_id=revision.id,
            suggestion_run_id=run.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            semantic_spec_hash=revision.semantic_spec.spec_hash,
            etag=1,
            suggestions=run.suggestions,
            decisions=decisions,
            artifact=artifact,
            proposal_hash=self._modeling_proposal_hash(
                revision=revision,
                run=run,
                decisions=decisions,
                artifact=artifact,
            ),
            created_by=created_by,
            created_at=now,
            updated_by=created_by,
            updated_at=now,
        )
        self.catalog.save_modeling_run_and_proposal(run=run, proposal=proposal)
        return proposal

    def get_modeling_proposal(self, proposal_id: str) -> ModelingProposal:
        return self.catalog.get_modeling_proposal(proposal_id)

    def save_ai_modeling_proposal(
        self,
        *,
        revision_id: str,
        proposal_id: str,
        expected_proposal_etag: int,
        expected_proposal_hash: str,
        decisions: tuple[SuggestionDecision, ...],
        alias_reviews: tuple[SemanticAliasReview, ...],
        saved_by: str,
        tenant_id: str = "",
    ) -> ModelingProposal:
        revision = self.catalog.get_revision(revision_id)
        proposal = self.catalog.get_modeling_proposal(proposal_id)
        if (
            proposal.revision_id != revision.id
            or proposal.project_id != revision.project_id
            or proposal.status is not ModelingProposalStatus.DRAFT
            or proposal.etag != expected_proposal_etag
            or proposal.proposal_hash != expected_proposal_hash
            or revision.etag != proposal.revision_etag
            or revision.schema_snapshot_hash != proposal.schema_snapshot_hash
            or revision.semantic_spec.spec_hash != proposal.semantic_spec_hash
        ):
            raise RevisionConflictError("modeling proposal is stale; reload before saving")
        run = self.catalog.get_modeling_run(proposal.suggestion_run_id)
        if run.status is not ModelingRunStatus.COMPLETED:
            raise RevisionConflictError("modeling proposal is stale; its AI run was reviewed")

        # Validate the complete patch set through the same deterministic editor
        # used by the final commit.  No result is persisted here.
        # create 时预览是 profile 过的（AI 分类新建维度 → profile 补 dimension_values），
        # 这里必须得到同一个 Catalog，否则 spec_hash 不同 → materialize 判
        # AI_MODELING_ARTIFACT_STALE → 重建产物（再调一次别名模型）并清空已核对
        # 标记，用户什么都没改也会触发。
        #
        # 但不能在这里重新 profile：那是每次保存都打一次库，而 profile 的结果
        # 已经冻结在 artifact.dimension_values 里，正是 materialize 要覆盖回去的
        # 那份。直接用它，保存路径不碰数据库。
        preview_revision = self._preview_ai_modeling_proposal(
            revision=revision,
            run=run,
            decisions=decisions,
        )
        preview_revision = _with_frozen_dimension_values(
            preview_revision, proposal.artifact.dimension_values
        )
        try:
            artifact = proposal.artifact.with_alias_reviews(alias_reviews)
            self._ai_artifact_service.materialize(preview_revision, artifact)
            reviewed_artifact_hash: str | None = artifact.artifact_hash
        except ValueError as exc:
            raise SemanticValidationError(
                "every AI alias draft requires one complete human review",
                code="AI_ALIAS_REVIEW_INCOMPLETE",
            ) from exc
        except SemanticValidationError as exc:
            if exc.code != "AI_MODELING_ARTIFACT_STALE":
                raise
            profiled_revision = self._preset_new_dimension_values(
                revision,
                preview_revision,
            )
            artifact = self._ai_artifact_service.build(profiled_revision, tenant_id=tenant_id)
            reviewed_artifact_hash = None
        updated = proposal.model_copy(
            update={
                "etag": proposal.etag + 1,
                "decisions": decisions,
                "artifact": artifact,
                "reviewed_artifact_hash": reviewed_artifact_hash,
                "proposal_hash": self._modeling_proposal_hash(
                    revision=revision,
                    run=run,
                    decisions=decisions,
                    artifact=artifact,
                ),
                "updated_by": saved_by,
                "updated_at": datetime.now(UTC),
            }
        )
        updated = ModelingProposal.model_validate(updated.model_dump(mode="python"))
        self.catalog.update_modeling_proposal(
            updated,
            previous_etag=proposal.etag,
            previous_hash=proposal.proposal_hash,
        )
        return updated

    def apply_ai_modeling_proposal(
        self,
        *,
        revision_id: str,
        proposal_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        expected_proposal_etag: int,
        expected_proposal_hash: str,
        reviewed_by: str,
    ) -> ModelingProposalApplyResult:
        revision = self.catalog.get_revision(revision_id)
        proposal = self.catalog.get_modeling_proposal(proposal_id)
        if (
            proposal.revision_id != revision.id
            or proposal.project_id != revision.project_id
            or proposal.status is not ModelingProposalStatus.DRAFT
            or revision.etag != expected_etag
            or proposal.revision_etag != expected_etag
            or revision.schema_snapshot_hash != schema_snapshot_hash
            or proposal.schema_snapshot_hash != schema_snapshot_hash
            or proposal.semantic_spec_hash != revision.semantic_spec.spec_hash
            or proposal.etag != expected_proposal_etag
            or proposal.proposal_hash != expected_proposal_hash
            or proposal.reviewed_artifact_hash != proposal.artifact.artifact_hash
        ):
            raise RevisionConflictError("modeling proposal is stale; reload before applying")
        run = self.catalog.get_modeling_run(proposal.suggestion_run_id)
        if (
            run.status is not ModelingRunStatus.COMPLETED
            or run.project_id != revision.project_id
            or run.revision_id != revision.id
            or run.revision_etag != expected_etag
            or run.schema_snapshot_hash != schema_snapshot_hash
            or run.suggestions != proposal.suggestions
        ):
            raise RevisionConflictError("modeling proposal no longer matches its AI run")

        updated_revision = self._preview_ai_modeling_proposal(
            revision=revision,
            run=run,
            decisions=proposal.decisions,
            artifact=proposal.artifact,
        )
        now = datetime.now(UTC)
        if proposal.artifact.semantic_context:
            updated_revision = ModelingRevision.model_validate(
                updated_revision.model_copy(
                    update={
                        "semantic_context_review_hash": semantic_context_content_hash(
                            proposal.artifact.semantic_context
                        ),
                        "semantic_context_reviewed_by": reviewed_by,
                        "semantic_context_reviewed_at": now,
                    }
                ).model_dump(mode="python")
            )
        reviewed_run = ModelingSuggestionRun.model_validate(
            run.model_copy(
                update={
                    "status": ModelingRunStatus.APPLIED,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": now,
                    "decisions": proposal.decisions,
                    "resulting_revision_etag": updated_revision.etag,
                }
            ).model_dump(mode="python")
        )
        reviewed_proposal = ModelingProposal.model_validate(
            proposal.model_copy(
                update={
                    "etag": proposal.etag + 1,
                    "status": ModelingProposalStatus.APPLIED,
                    "updated_by": reviewed_by,
                    "updated_at": now,
                    "reviewed_by": reviewed_by,
                    "reviewed_at": now,
                    "resulting_revision_etag": updated_revision.etag,
                }
            ).model_dump(mode="python")
        )
        self.catalog.apply_modeling_proposal_review(
            revision=updated_revision,
            previous_revision_etag=revision.etag,
            run=reviewed_run,
            proposal=reviewed_proposal,
            previous_proposal_etag=proposal.etag,
            previous_proposal_hash=proposal.proposal_hash,
        )
        return ModelingProposalApplyResult(
            proposal=reviewed_proposal,
            revision=updated_revision,
        )

    def _preview_ai_modeling_proposal(
        self,
        *,
        revision: ModelingRevision,
        run: ModelingSuggestionRun,
        decisions: tuple[SuggestionDecision, ...],
        artifact: AiModelingArtifact | None = None,
        profile_values: bool = False,
    ) -> ModelingRevision:
        """Build the exact Candidate produced by one AI-modeling confirmation."""

        updated = self._revision_editor.apply_suggestion_run(
            revision,
            expected_etag=revision.etag,
            expected_schema_snapshot_hash=revision.schema_snapshot_hash,
            suggestions=run.suggestions,
            decisions=decisions,
        )
        if artifact is not None:
            return self._ai_artifact_service.materialize(updated, artifact)
        if profile_values:
            return self._preset_new_dimension_values(revision, updated)
        return updated

    @staticmethod
    def _modeling_proposal_hash(
        *,
        revision: ModelingRevision,
        run: ModelingSuggestionRun,
        decisions: tuple[SuggestionDecision, ...],
        artifact: AiModelingArtifact,
    ) -> str:
        return content_hash(
            {
                "revision_id": revision.id,
                "revision_etag": revision.etag,
                "schema_snapshot_hash": revision.schema_snapshot_hash,
                "semantic_spec_hash": revision.semantic_spec.spec_hash,
                "suggestion_run_id": run.id,
                "suggestion_input_hash": run.input_hash,
                "suggestions": [item.model_dump(mode="json") for item in run.suggestions],
                "decisions": [item.model_dump(mode="json") for item in decisions],
                "artifact": artifact.model_dump(mode="json"),
            }
        )

    def create_modeling_plan(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        suggestion_run_id: str | None = None,
    ) -> ModelingPlan:
        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision changed; reload before generating a plan")
        suggestion_run = (
            self.catalog.get_modeling_run(suggestion_run_id)
            if suggestion_run_id is not None
            else None
        )
        project = self.catalog.get_project(revision.project_id)
        plan = self._modeling_plan_builder.build(
            revision=revision,
            project_name=project.name,
            suggestion_run=suggestion_run,
        )
        return self.catalog.save_modeling_plan(plan)

    def get_modeling_plan(self, plan_id: str) -> ModelingPlan:
        return self.catalog.get_modeling_plan(plan_id)

    def apply_modeling_plan(
        self,
        *,
        revision_id: str,
        plan_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        choices: tuple[DecisionChoice, ...],
        reviewed_by: str,
    ) -> ModelingPlanApplyResult:
        revision = self.catalog.get_revision(revision_id)
        plan = self.catalog.get_modeling_plan(plan_id)
        if expected_etag != plan.revision_etag or schema_snapshot_hash != plan.schema_snapshot_hash:
            raise RevisionConflictError("modeling plan is stale; generate a new plan")
        choice_by_id = self._modeling_plan_builder.validate_choices(
            plan=plan,
            revision=revision,
            choices=choices,
        )

        updated = revision
        reviewed_run: ModelingSuggestionRun | None = None
        if plan.phase is ModelingPlanPhase.REVIEWING_SEMANTICS:
            run = (
                self.catalog.get_modeling_run(plan.suggestion_run_id)
                if plan.suggestion_run_id is not None
                else None
            )
            run_suggestions = run.suggestions if run is not None else ()
            suggestions = tuple(
                item
                for item in (*revision.suggestions, *run_suggestions)
                if item.state is SuggestionState.PENDING
            )
            decision_by_suggestion = {
                source_id: (item, choice_by_id[item.id])
                for item in plan.queue.decisions
                for source_id in item.source_suggestion_ids
            }
            suggestion_decisions = tuple(
                self._compile_plan_suggestion_decision(
                    suggestion=item,
                    decision_and_choice=decision_by_suggestion.get(item.id),
                )
                for item in suggestions
            )
            if run is not None:
                updated = self._revision_editor.apply_suggestion_run(
                    revision,
                    expected_etag=revision.etag,
                    expected_schema_snapshot_hash=revision.schema_snapshot_hash,
                    suggestions=run.suggestions,
                    decisions=suggestion_decisions,
                )
                reviewed_run = run.model_copy(
                    update={
                        "status": ModelingRunStatus.APPLIED,
                        "reviewed_by": reviewed_by,
                        "reviewed_at": datetime.now(UTC),
                        "decisions": suggestion_decisions,
                        "resulting_revision_etag": updated.etag,
                    }
                )
                reviewed_run = ModelingSuggestionRun.model_validate(
                    reviewed_run.model_dump(mode="python")
                )
            else:
                updated = self._revision_editor.apply_decisions(
                    revision,
                    expected_etag=revision.etag,
                    expected_schema_snapshot_hash=revision.schema_snapshot_hash,
                    decisions=suggestion_decisions,
                )
        elif plan.phase is ModelingPlanPhase.REVIEWING_DATASET:
            decision = plan.queue.decisions[0]
            choice = choice_by_id[decision.id]
            if choice.option_id == "accept":
                if decision.proposed_resource is None:
                    raise RevisionConflictError("modeling plan has no dataset proposal")
                dataset = DatasetSpec.model_validate(decision.proposed_resource)
                catalog = self._require_semantic_catalog(revision)
                previous = next(
                    (item for item in catalog.data_sets if item.id == dataset.id),
                    None,
                )
                catalog_dataset = catalog_dataset_from_topic_command(
                    dataset,
                    revision.semantic_spec,
                    previous,
                )
                catalog = replace_catalog_item(
                    catalog,
                    collection="data_sets",
                    item=catalog_dataset,
                )
                updated = self._revision_editor.replace_semantic_catalog(
                    revision,
                    expected_etag=revision.etag,
                    expected_schema_snapshot_hash=revision.schema_snapshot_hash,
                    semantic_catalog=catalog,
                )

        updated = self._preset_new_dimension_values(revision, updated)

        reviewed_plan = plan.model_copy(
            update={
                "status": ModelingPlanStatus.APPLIED,
                "choices": choices,
                "resulting_revision_etag": updated.etag,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(UTC),
            }
        )
        reviewed_plan = ModelingPlan.model_validate(reviewed_plan.model_dump(mode="python"))
        self.catalog.apply_modeling_plan_review(
            revision=updated,
            previous_etag=revision.etag,
            plan=reviewed_plan,
            run=reviewed_run,
        )
        return ModelingPlanApplyResult(plan=reviewed_plan, revision=updated)

    @staticmethod
    def _compile_plan_suggestion_decision(
        *,
        suggestion,
        decision_and_choice: tuple[ModelingDecision, DecisionChoice] | None,
    ) -> SuggestionDecision:
        if (
            suggestion.source is SuggestionSource.DATABASE_CONSTRAINT
            and suggestion.target_kind != "relation"
        ):
            return SuggestionDecision(suggestion_id=suggestion.id, accept=True)
        if decision_and_choice is None:
            raise RevisionConflictError(f"modeling plan omitted suggestion choice: {suggestion.id}")
        decision, choice = decision_and_choice
        if decision.option_accepts_suggestion_ids:
            accepted = decision.option_accepts_suggestion_ids.get(choice.option_id)
            if accepted is None:
                raise RevisionConflictError(f"modeling plan omitted option effects: {decision.id}")
            return SuggestionDecision(
                suggestion_id=suggestion.id,
                accept=suggestion.id in accepted,
            )
        if choice.option_id.startswith("aggregation:"):
            return SuggestionDecision(
                suggestion_id=suggestion.id,
                accept=True,
                overrides={"aggregation": choice.option_id.split(":", 1)[1]},
            )
        if choice.option_id.startswith("cardinality:"):
            return SuggestionDecision(
                suggestion_id=suggestion.id,
                accept=True,
                overrides={"cardinality": choice.option_id.split(":", 1)[1]},
            )
        return SuggestionDecision(
            suggestion_id=suggestion.id,
            accept=choice.option_id == "accept",
        )

    def get_modeling_summary(self, *, project_id: str) -> ModelingSummary:
        project = self.catalog.get_project(project_id)
        revision = self.catalog.get_latest_revision(project_id=project_id)
        if revision is None:
            return ModelingSummary(
                project_id=project.id,
                project_name=project.name,
                stage="selecting_data",
                active_release_id=project.active_release_id,
            )
        plan = self.catalog.get_latest_modeling_plan(
            project_id=project_id,
            revision_id=revision.id,
        )
        current_plan = (
            plan
            if plan is not None
            and plan.revision_etag == revision.etag
            and plan.schema_snapshot_hash == revision.schema_snapshot_hash
            else None
        )
        if revision.state is RevisionState.PUBLISHED:
            stage = "published"
        elif revision.state is RevisionState.VALIDATED:
            stage = "ready_to_publish"
        elif (
            current_plan is not None
            and current_plan.status is ModelingPlanStatus.READY
            and current_plan.phase is not ModelingPlanPhase.READY_FOR_VALIDATION
        ):
            stage = (
                "blocked"
                if current_plan.phase is ModelingPlanPhase.BLOCKED
                else "reviewing_decisions"
            )
        elif revision.semantic_spec.datasets:
            stage = "verifying"
        else:
            stage = "building_draft"
        pending = (
            current_plan.queue.summary.needs_confirmation
            if current_plan is not None and current_plan.status is ModelingPlanStatus.READY
            else 0
        )
        informational = (
            current_plan.queue.summary.informational
            if current_plan is not None and current_plan.status is ModelingPlanStatus.READY
            else 0
        )
        spec = revision.semantic_spec
        return ModelingSummary(
            project_id=project.id,
            project_name=project.name,
            stage=stage,
            active_release_id=project.active_release_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            revision_state=revision.state.value,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            plan_id=current_plan.id if current_plan is not None else None,
            pending_confirmations=pending,
            informational_items=informational,
            counts=ModelingResourceCounts(
                models=len(spec.models),
                fields=len(spec.fields),
                relations=len(spec.relations),
                metrics=len(spec.metrics),
                dimensions=len(spec.dimensions),
                datasets=len(spec.datasets),
            ),
        )

    def apply_ai_suggestion_run(
        self,
        *,
        revision_id: str,
        run_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        decisions: tuple[SuggestionDecision, ...],
        reviewed_by: str,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        run = self.catalog.get_modeling_run(run_id)
        if run.project_id != revision.project_id or run.revision_id != revision.id:
            raise RevisionConflictError("modeling run belongs to another revision")
        if run.status is not ModelingRunStatus.COMPLETED:
            raise RevisionConflictError("modeling run was already reviewed")
        if run.revision_etag != expected_etag or run.schema_snapshot_hash != schema_snapshot_hash:
            raise RevisionConflictError("modeling run is stale; generate suggestions again")
        updated = self._revision_editor.apply_suggestion_run(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            suggestions=run.suggestions,
            decisions=decisions,
        )
        updated = self._preset_new_dimension_values(revision, updated)
        reviewed_run = run.model_copy(
            update={
                "status": ModelingRunStatus.APPLIED,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(UTC),
                "decisions": decisions,
                "resulting_revision_etag": updated.etag,
            }
        )
        reviewed_run = ModelingSuggestionRun.model_validate(reviewed_run.model_dump(mode="python"))
        self.catalog.apply_modeling_run_review(
            revision=updated,
            previous_etag=revision.etag,
            run=reviewed_run,
        )
        return updated

    def get_revision(self, revision_id: str) -> ModelingRevision:
        return self.catalog.get_revision(revision_id)

    def get_semantic_catalog(self, revision_id: str) -> SemanticCatalog:
        revision = self.catalog.get_revision(revision_id)
        return revision.semantic_catalog

    def upsert_catalog_model(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        model: ModelContract,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        catalog = self._require_semantic_catalog(revision)
        self._validate_catalog_model_source(revision, model)
        updated_catalog = upsert_model_aggregate(catalog, model)
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
        )

    def upsert_catalog_identifier(
        self,
        *,
        revision_id: str,
        model_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        identifier: IdentifierContract,
    ) -> ModelingRevision:
        return self._upsert_model_detail_resource(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="identifiers",
            identity_field="biz_name",
            item=identifier,
        )

    def upsert_catalog_model_dimension(
        self,
        *,
        revision_id: str,
        model_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        dimension: ModelDimensionContract,
    ) -> ModelingRevision:
        return self._upsert_model_detail_resource(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="dimensions",
            identity_field="biz_name",
            item=dimension,
        )

    def upsert_catalog_measure(
        self,
        *,
        revision_id: str,
        model_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        measure: MeasureContract,
    ) -> ModelingRevision:
        return self._upsert_model_detail_resource(
            revision_id=revision_id,
            model_id=model_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="measures",
            identity_field="biz_name",
            item=measure,
        )

    def upsert_catalog_model_field(
        self,
        *,
        revision_id: str,
        model_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        field: ModelFieldContract,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before editing fields")
        if revision.schema_snapshot_hash != schema_snapshot_hash:
            raise RevisionConflictError("schema snapshot changed; fields cannot be edited")
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(f"{revision.state.value} revisions are immutable")
        catalog = self._require_semantic_catalog(revision)
        model = next((item for item in catalog.models if item.id == model_id), None)
        if model is None:
            raise SemanticValidationError("model was not found", code="MODEL_NOT_FOUND")
        current = next(
            (item for item in model.model_detail.fields if item.field_name == field.field_name),
            None,
        )
        if current is None or current != field:
            raise SemanticValidationError(
                "physical model fields are immutable inside a schema snapshot",
                code="MODEL_FIELDS_DIFFER_FROM_SNAPSHOT",
            )
        return revision

    def upsert_catalog_relation(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        relation: ModelRelationContract,
    ) -> ModelingRevision:
        return self._upsert_catalog_resource(
            revision_id=revision_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="model_relations",
            item=relation,
        )

    def upsert_catalog_dimension(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        dimension: DimensionContract,
    ) -> ModelingRevision:
        return self._upsert_catalog_resource(
            revision_id=revision_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="dimensions",
            item=dimension,
        )

    def upsert_catalog_hierarchy(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        hierarchy: HierarchyContract,
    ) -> ModelingRevision:
        return self._upsert_catalog_resource(
            revision_id=revision_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="hierarchies",
            item=hierarchy,
        )

    def upsert_catalog_metric(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        metric: MetricContract,
    ) -> ModelingRevision:
        return self._upsert_catalog_resource(
            revision_id=revision_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="metrics",
            item=metric,
        )

    def upsert_catalog_dataset(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        data_set: DataSetContract,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        raise SemanticValidationError(
            "compiler-owned QueryScope cannot be edited directly; edit its governed resources",
            code="DERIVED_QUERY_SCOPE_IMMUTABLE",
        )

    def upsert_query_rule(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        query_rule: QueryRuleSpec,
    ) -> ModelingRevision:
        return self._upsert_catalog_resource(
            revision_id=revision_id,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            collection="query_rules",
            item=QueryRuleContract.model_validate(query_rule.model_dump(mode="python")),
        )

    def preview_catalog_deletion(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> DeletionImpact:
        """Return the exact dependency-normalized deletion plan for review.

        Deleting a resource also deletes, for a model, its owned metrics and
        dimensions. The resulting dependency cleanup is shown before mutation
        because one Revision also
        contains datasets, terms, value dictionaries and drill-down contracts.
        """

        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        return self._deletion_planner.preview(
            self._require_semantic_catalog(revision),
            resource_kind=resource_kind,
            resource_id=resource_id,
        )

    def delete_catalog_resource(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        resource_kind: ResourceKind,
        resource_id: str,
        expected_impact_hash: str,
    ) -> ModelingRevision:
        """Apply a previously reviewed deletion plan as one Revision mutation."""

        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        updated_catalog = self._deletion_planner.apply(
            self._require_semantic_catalog(revision),
            resource_kind=resource_kind,
            resource_id=resource_id,
            expected_impact_hash=expected_impact_hash,
        )
        projection = compile_semantic_catalog(updated_catalog)
        model_ids = {item.id for item in projection.models}
        field_ids = {item.id for item in projection.fields}
        relation_ids = {item.id for item in projection.relations}
        suggestions = tuple(
            item
            for item in revision.suggestions
            if (
                (item.target_kind == "model" and item.target_id in model_ids)
                or (item.target_kind == "field" and item.target_id in field_ids)
                or (item.target_kind == "relation" and item.target_id in relation_ids)
            )
        )
        updated = self._revision_editor.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
            suggestions=suggestions,
        )
        self.catalog.update_revision(updated, previous_etag=revision.etag)
        return updated

    def _upsert_model_detail_resource(
        self,
        *,
        revision_id: str,
        model_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        collection: str,
        identity_field: str,
        item: object,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        catalog = self._require_semantic_catalog(revision)
        updated_catalog = replace_model_detail_item(
            catalog,
            model_id=model_id,
            collection=collection,
            item=item,
            identity_field=identity_field,
        )
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
        )

    def _upsert_catalog_resource(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        collection: str,
        item: object,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        catalog = self._require_semantic_catalog(revision)
        updated_catalog = replace_catalog_item(catalog, collection=collection, item=item)
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
        )

    def _save_catalog_update(
        self,
        revision: ModelingRevision,
        *,
        expected_etag: int,
        schema_snapshot_hash: str,
        semantic_catalog: SemanticCatalog,
    ) -> ModelingRevision:
        semantic_catalog = reconcile_query_scopes(semantic_catalog)
        updated = self._revision_editor.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=semantic_catalog,
        )
        updated = self._preset_new_dimension_values(revision, updated)
        self.catalog.update_revision(updated, previous_etag=revision.etag)
        return updated

    def _profile_tables(self, snapshot: SchemaSnapshot) -> dict[tuple[str, str], TableProfile]:
        """S1 画像。没配 profiler 或某张表失败时返回空/跳过：画像是证据不是门禁，
        规则会退回类型 + 列名。"""

        if self._column_profiler is None:
            return {}
        profiles: dict[tuple[str, str], TableProfile] = {}
        for table in snapshot.tables:
            try:
                profile = self._column_profiler.profile_table(table)
            except Exception:  # noqa: BLE001 — 画像失败不能阻断导入
                logging.getLogger(__name__).exception(
                    "column profile failed table=%s.%s", table.schema_name, table.name
                )
                continue
            if profile.error is None:
                profiles[(table.schema_name, table.name)] = profile
        return profiles

    def _preset_new_dimension_values(
        self,
        previous: ModelingRevision,
        updated: ModelingRevision,
    ) -> ModelingRevision:
        """Populate complete categorical dictionaries at Dimension creation.

        This hook lives below every UI/Agent/API modeling command so automatic
        values cannot depend on which client created the Dimension. Database
        profiling is deterministic; AI aliases remain a separate optional action.
        """

        if self._semantic_profiler is None or updated.semantic_catalog is None:
            return updated
        previous_ids = {
            item.id
            for item in (
                previous.semantic_catalog.dimensions
                if previous.semantic_catalog is not None
                else ()
            )
        }
        created_ids = tuple(
            item.id for item in updated.semantic_spec.dimensions if item.id not in previous_ids
        )
        if not created_ids:
            return updated
        eligibility = assess_dimension_dictionary_eligibility(
            revision=updated,
            dimension_ids=created_ids,
        )
        targets = tuple(
            item.dimension_id
            for item in eligibility
            if item.status is DimensionDictionaryEligibilityStatus.ELIGIBLE
        )
        if not targets:
            return updated
        snapshot_id = f"schema_{updated.schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        snapshot = self.catalog.get_schema_snapshot(
            snapshot_id,
            project_id=updated.project_id,
        )
        profile = self._semantic_profiler.profile(
            snapshot=snapshot,
            semantic_spec=updated.semantic_spec,
            dimension_ids=targets,
        )
        profiled_ids = {item.dimension_id for item in profile.dimensions}
        missing_ids = sorted(set(targets) - profiled_ids)
        if missing_ids:
            raise SemanticValidationError(
                "automatic dimension dictionary profiling did not complete for "
                f"all eligible dimensions: {missing_ids[:5]}",
                code="DIMENSION_DICTIONARY_PROFILE_INCOMPLETE",
            )
        values = merge_complete_profiled_dimension_values(
            current_values=updated.semantic_catalog.dimension_values,
            profile=profile,
            dimension_ids=targets,
        )
        if values == updated.semantic_catalog.dimension_values:
            return updated
        catalog = SemanticCatalog.model_validate(
            updated.semantic_catalog.model_copy(update={"dimension_values": values}).model_dump(
                mode="python"
            )
        )
        return ModelingRevision.model_validate(
            updated.model_copy(
                update={
                    "semantic_catalog": catalog,
                    "semantic_spec": compile_semantic_catalog(catalog),
                }
            ).model_dump(mode="python")
        )

    @staticmethod
    def _require_semantic_catalog(revision: ModelingRevision) -> SemanticCatalog:
        return revision.semantic_catalog

    @staticmethod
    def _require_editable_revision_version(
        revision: ModelingRevision,
        *,
        expected_etag: int,
        schema_snapshot_hash: str,
    ) -> None:
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before deleting")
        if revision.schema_snapshot_hash != schema_snapshot_hash:
            raise RevisionConflictError("schema snapshot changed; reload before deleting")
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(f"{revision.state.value} revisions are immutable")

    def _validate_catalog_model_source(
        self,
        revision: ModelingRevision,
        model: ModelContract,
    ) -> None:
        existing = next(
            (item for item in revision.semantic_catalog.models if item.id == model.id),
            None,
        )
        if (
            existing is not None
            and existing.model_detail.query_type != model.model_detail.query_type
        ):
            raise SemanticValidationError(
                "model source type cannot change inside one revision",
                code="MODEL_SOURCE_IMMUTABLE",
            )
        if model.model_detail.query_type.value == "sql_query":
            validate_sql_model(
                model.model_detail.sql_query or "",
                tuple(
                    item.model_dump(mode="json", by_alias=True)
                    for item in model.model_detail.sql_variables
                ),
            )
            return
        table_query = model.model_detail.table_query or ""
        parts = [item.strip().strip('"') for item in table_query.split(".") if item.strip()]
        if not parts:
            raise SemanticValidationError(
                "model tableQuery is empty",
                code="MODEL_TABLE_QUERY_INVALID",
            )
        if (
            existing is not None
            and existing.model_detail.table_query != model.model_detail.table_query
        ):
            raise SemanticValidationError(
                "model tableQuery cannot change inside one revision",
                code="MODEL_SOURCE_IMMUTABLE",
            )
        duplicate = next(
            (
                item.id
                for item in revision.semantic_catalog.models
                if item.id != model.id
                and item.model_detail.query_type == model.model_detail.query_type
                and item.model_detail.table_query == model.model_detail.table_query
            ),
            None,
        )
        if duplicate is not None:
            raise SemanticValidationError(
                "physical table is already bound to another model",
                code="MODEL_SOURCE_ALREADY_BOUND",
            )
        schema_name, table_name = (
            ("public", parts[0]) if len(parts) == 1 else (parts[-2], parts[-1])
        )
        snapshot_id = f"schema_{revision.schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        snapshot = self.catalog.get_schema_snapshot(snapshot_id, project_id=revision.project_id)
        table = next(
            (
                item
                for item in snapshot.tables
                if item.schema_name == schema_name and item.name == table_name
            ),
            None,
        )
        if table is None:
            raise SemanticValidationError(
                "model tableQuery is absent from the schema snapshot",
                code="TABLE_NOT_IN_SNAPSHOT",
            )
        expected_fields = {item.name: item.data_type for item in table.columns}
        actual_fields = {item.field_name: item.data_type for item in model.model_detail.fields}
        if actual_fields != expected_fields:
            raise SemanticValidationError(
                "model fields differ from the schema snapshot",
                code="MODEL_FIELDS_DIFFER_FROM_SNAPSHOT",
            )

    def apply_modeling_decisions(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        decisions: tuple[SuggestionDecision, ...],
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        updated = self._revision_editor.apply_decisions(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            decisions=decisions,
        )
        updated = self._preset_new_dimension_values(revision, updated)
        self.catalog.update_revision(updated, previous_etag=revision.etag)
        return updated

    def propose_analysis_topics(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
    ) -> AnalysisTopicProposalSet:
        """Return deterministic topic proposals without changing the Revision."""

        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        return AnalysisTopicProposalSet(
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            proposals=AnalysisTopicProposer().propose(revision.semantic_spec),
        )

    def upsert_analysis_topic(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        dataset: DatasetSpec,
        route: AnalysisTopicRouteSpec,
    ) -> ModelingRevision:
        """Atomically persist the DataSet and its frozen join route."""

        if dataset.id != route.dataset_id:
            raise SemanticValidationError(
                "analysis topic dataset and route identifiers differ",
                code="ANALYSIS_TOPIC_ID_MISMATCH",
            )
        revision = self.catalog.get_revision(revision_id)
        self._require_editable_revision_version(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
        )
        catalog = self._require_semantic_catalog(revision)
        previous = next((item for item in catalog.data_sets if item.id == dataset.id), None)
        data_set = catalog_dataset_from_topic_command(
            dataset,
            revision.semantic_spec,
            previous,
        )
        catalog = replace_catalog_item(
            catalog,
            collection="data_sets",
            item=data_set,
        )
        catalog = catalog.model_copy(
            update={
                "analysis_topic_routes": (
                    *(
                        item
                        for item in catalog.analysis_topic_routes
                        if item.dataset_id != route.dataset_id
                    ),
                    route,
                ),
            }
        )
        catalog = type(catalog).model_validate(catalog.model_dump(mode="python"))
        validate_analysis_topic_route(compile_semantic_catalog(catalog), route)
        updated = self._revision_editor.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=catalog,
        )
        self.catalog.update_revision(updated, previous_etag=revision.etag)
        return updated

    def upsert_term(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        term: TermSpec,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        # Reviewed 2026-08-27 modeling contract: a user-authored Term maps
        # business language to governed meaning.  Existing legacy Catalogs may
        # still be loaded, but every new atomic write must bind at least one
        # metric or dimension instead of creating an inert glossary row.
        if not term.metric_ids and not term.dimension_ids:
            raise SemanticValidationError(
                "术语必须至少关联一个受治理指标或维度",
                code="TERM_BINDING_REQUIRED",
            )
        current_catalog = self._require_semantic_catalog(revision)
        current = next((item for item in current_catalog.terms if item.id == term.id), None)
        if (current is None and term.dataset_ids) or (
            current is not None and term.dataset_ids != current.dataset_ids
        ):
            raise SemanticValidationError(
                "术语的查询作用域关联由目录编译器管理",
                code="TERM_SCOPE_LINKS_MANAGED",
            )
        catalog = replace_catalog_item(
            current_catalog,
            collection="terms",
            item=term,
        )
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=catalog,
        )

    def upsert_dimension_value(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        dimension_value: DimensionValueSpec,
    ) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        current = next(
            (
                item
                for item in self._require_semantic_catalog(revision).dimension_values
                if item.id == dimension_value.id
            ),
            None,
        )
        if current is None:
            raise SemanticValidationError(
                "dimension value was not found in the current revision",
                code="DIMENSION_VALUE_NOT_FOUND",
            )
        same_value = (
            type(current.value) is type(dimension_value.value)
            and current.value == dimension_value.value
        )
        if current.dimension_id != dimension_value.dimension_id or not same_value:
            raise SemanticValidationError(
                "dimension value identity cannot change inside one revision",
                code="DIMENSION_VALUE_IDENTITY_IMMUTABLE",
            )
        catalog = replace_catalog_item(
            self._require_semantic_catalog(revision),
            collection="dimension_values",
            item=dimension_value,
        )
        return self._save_catalog_update(
            revision,
            expected_etag=expected_etag,
            schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=catalog,
        )

    def generate_dimension_dictionary_preview(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        dimension_ids: tuple[str, ...],
        policies: tuple[DimensionDictionaryPolicy, ...] | None = None,
    ) -> DimensionDictionaryPreview:
        """Collect selected dimension values without mutating the revision.

        This is the product boundary for scheduling a dictionary task and
        fetching its item values.
        """

        if self._semantic_profiler is None:
            raise ValueError("semantic data profiler is not configured")
        if not dimension_ids or len(dimension_ids) > 100:
            raise SemanticValidationError(
                "select between 1 and 100 dimensions",
                code="INVALID_DICTIONARY_SCOPE",
            )
        if len(set(dimension_ids)) != len(dimension_ids):
            raise SemanticValidationError(
                "dimension dictionary scope contains duplicates",
                code="INVALID_DICTIONARY_SCOPE",
            )
        revision = self.catalog.get_revision(revision_id)
        if revision.etag != expected_etag:
            raise RevisionConflictError(
                "revision etag changed; reload before collecting dimension values"
            )
        if revision.schema_snapshot_hash != schema_snapshot_hash:
            raise RevisionConflictError(
                "schema snapshot changed; dimension values cannot be collected"
            )
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(
                f"{revision.state.value} revisions cannot generate dictionary previews"
            )
        unresolved = [
            item.id
            for item in revision.suggestions
            if item.state in {SuggestionState.PENDING, SuggestionState.CONFLICT}
        ]
        if unresolved:
            raise SemanticValidationError(
                f"review modeling suggestions before collecting values: {unresolved[:5]}",
                code="UNREVIEWED_SUGGESTIONS",
            )
        eligibility = assess_dimension_dictionary_eligibility(
            revision=revision,
            dimension_ids=dimension_ids,
        )
        ineligible = [
            item
            for item in eligibility
            if item.status is DimensionDictionaryEligibilityStatus.INELIGIBLE
        ]
        if ineligible:
            reasons = [f"{item.dimension_id}:{item.reason_code}" for item in ineligible]
            raise SemanticValidationError(
                f"dictionary dimensions are ineligible: {reasons[:5]}",
                code="DICTIONARY_DIMENSION_INELIGIBLE",
            )
        snapshot_id = f"schema_{schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        snapshot = self.catalog.get_schema_snapshot(snapshot_id, project_id=revision.project_id)
        profile = self._semantic_profiler.profile(
            snapshot=snapshot,
            semantic_spec=revision.semantic_spec,
            dimension_ids=dimension_ids,
        )
        preview = self._dimension_dictionary_builder.build(
            revision=revision,
            profile=profile,
            dimension_ids=dimension_ids,
            policies=policies,
        )
        self.catalog.save_dimension_dictionary_preview(preview)
        return preview

    def get_dimension_dictionary_preview(self, preview_id: str) -> DimensionDictionaryPreview:
        return self.catalog.get_dimension_dictionary_preview(preview_id)

    def run_due_dimension_dictionary_refreshes(
        self,
        *,
        project_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> tuple[DimensionDictionaryPreview, ...]:
        """Create review previews for due dictionaries; never auto-apply them."""

        if not 1 <= limit <= 100:
            raise ValueError("dictionary refresh limit must be between 1 and 100")
        self.catalog.get_project(project_id)
        groups = due_dictionary_refresh_groups(
            self.catalog.list_dimension_dictionary_previews(project_id=project_id),
            now=now or datetime.now(UTC),
        )
        previews: list[DimensionDictionaryPreview] = []
        for revision_id, policies in groups[:limit]:
            revision = self.catalog.get_revision(revision_id)
            if revision.project_id != project_id:
                continue
            previews.append(
                self.generate_dimension_dictionary_preview(
                    revision_id=revision.id,
                    expected_etag=revision.etag,
                    schema_snapshot_hash=revision.schema_snapshot_hash,
                    dimension_ids=tuple(item.dimension_id for item in policies),
                    policies=policies,
                )
            )
        return tuple(previews)

    def apply_dimension_dictionary_preview(
        self,
        *,
        preview_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
        decisions: tuple[DimensionValueDecision, ...],
        reviewed_by: str,
    ) -> DimensionDictionaryApplyResult:
        preview = self.catalog.get_dimension_dictionary_preview(preview_id)
        if preview.status is not DimensionDictionaryStatus.COMPLETED:
            raise RevisionConflictError("dimension dictionary preview was already reviewed")
        revision = self.catalog.get_revision(preview.revision_id)
        if revision.etag != expected_etag or preview.revision_etag != expected_etag:
            raise RevisionConflictError(
                "dimension dictionary preview is stale; collect values again"
            )
        if (
            revision.schema_snapshot_hash != schema_snapshot_hash
            or preview.schema_snapshot_hash != schema_snapshot_hash
        ):
            raise RevisionConflictError(
                "schema snapshot changed; dimension dictionary preview is stale"
            )
        if revision.semantic_spec.spec_hash != preview.semantic_spec_hash:
            raise RevisionConflictError(
                "semantic revision changed; dimension dictionary preview is stale"
            )

        candidate_by_id = {item.id: item for item in preview.candidates}
        decision_by_id = {item.candidate_id: item for item in decisions}
        if len(decision_by_id) != len(decisions):
            raise SemanticValidationError(
                "dimension value decisions contain duplicate candidates",
                code="INCOMPLETE_DICTIONARY_REVIEW",
            )
        if set(decision_by_id) != set(candidate_by_id):
            raise SemanticValidationError(
                "every dimension value candidate requires one review decision",
                code="INCOMPLETE_DICTIONARY_REVIEW",
            )

        selected = set(preview.selected_dimension_ids)
        final_states = {
            candidate.id: (decision_by_id[candidate.id].list_state or candidate.list_state)
            for candidate in preview.candidates
            if decision_by_id[candidate.id].accept
        }
        policy_by_dimension = {item.dimension_id: item for item in preview.policies}
        reviewed_policies = []
        for dimension_id in preview.selected_dimension_ids:
            candidates = [
                item
                for item in preview.candidates
                if item.dimension_id == dimension_id and decision_by_id[item.id].accept
            ]
            reviewed_policies.append(
                policy_by_dimension[dimension_id].model_copy(
                    update={
                        "black_list": tuple(
                            item.value
                            for item in candidates
                            if final_states[item.id] is DimensionValueListState.BLACK
                        ),
                        "white_list": tuple(
                            item.value
                            for item in candidates
                            if final_states[item.id] is DimensionValueListState.WHITE
                        ),
                    }
                )
            )
        reviewed_policy_by_dimension = {item.dimension_id: item for item in reviewed_policies}
        catalog = self._require_semantic_catalog(revision)
        values = [item for item in catalog.dimension_values if item.dimension_id not in selected]
        for candidate in preview.candidates:
            decision = decision_by_id[candidate.id]
            if not decision.accept:
                continue
            list_state = final_states[candidate.id]
            policy = reviewed_policy_by_dimension[candidate.dimension_id]
            policy_enabled = (
                list_state is DimensionValueListState.WHITE
                if policy.white_list
                else list_state is not DimensionValueListState.BLACK
            )
            values.append(
                DimensionValueSpec(
                    id=candidate.dimension_value_id,
                    dimension_id=candidate.dimension_id,
                    value=candidate.value,
                    display_name=decision.display_name or candidate.display_name,
                    aliases=(
                        decision.aliases if decision.aliases is not None else candidate.aliases
                    ),
                    enabled=(
                        policy_enabled
                        and (
                            decision.enabled if decision.enabled is not None else candidate.enabled
                        )
                    ),
                )
            )
        updated_catalog = SemanticCatalog.model_validate(
            catalog.model_copy(update={"dimension_values": tuple(values)}).model_dump(mode="python")
        )
        updated = self._revision_editor.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
            semantic_catalog=updated_catalog,
        )
        applied_preview = preview.model_copy(
            update={
                "status": DimensionDictionaryStatus.APPLIED,
                "reviewed_by": reviewed_by,
                "reviewed_at": datetime.now(UTC),
                "decisions": decisions,
                "policies": tuple(reviewed_policies),
                "resulting_revision_etag": updated.etag,
            }
        )
        applied_preview = DimensionDictionaryPreview.model_validate(
            applied_preview.model_dump(mode="python")
        )
        self.catalog.apply_dimension_dictionary_review(
            revision=updated,
            previous_etag=revision.etag,
            preview=applied_preview,
        )
        return DimensionDictionaryApplyResult(preview=applied_preview, revision=updated)

    def validate_revision(self, revision_id: str) -> ModelingRevision:
        revision = self.catalog.get_revision(revision_id)
        validated = self._revision_editor.validate_for_publish(revision)
        self.catalog.update_revision(validated, previous_etag=revision.etag)
        return validated

    def get_revision_diagnostics(self, revision_id: str) -> ModelingDiagnosticsReport:
        return self._diagnostics_analyzer.analyze(self.catalog.get_revision(revision_id))

    def create_schema_drift_report(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
    ) -> SchemaDriftReport:
        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
        )
        snapshot_id = f"schema_{schema_snapshot_hash.removeprefix('sha256:')[:16]}"
        baseline = self.catalog.get_schema_snapshot(
            snapshot_id,
            project_id=revision.project_id,
        )
        schemas = sorted({item.schema_name for item in baseline.tables})
        entries = tuple(
            entry
            for schema_name in schemas
            for entry in self._introspector.list_tables(
                schema_name=schema_name,
                include_views=True,
            )
        )
        entry_by_key = {(item.schema_name, item.name): item for item in entries}
        current_tables = tuple(
            self._introspector.describe_table(
                schema_name=item.schema_name,
                table_name=item.name,
                include_views=entry_by_key.get((item.schema_name, item.name), item).source_type
                == "view",
            )
            for item in baseline.tables
            if (item.schema_name, item.name) in entry_by_key
        )
        report = self._schema_drift_analyzer.analyze(
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            baseline=baseline,
            current_tables=current_tables,
            available_table_keys=tuple(entry_by_key),
            semantic_spec=revision.semantic_spec,
        )
        self.catalog.save_schema_drift_report(report)
        return report

    def get_schema_drift_report(self, report_id: str) -> SchemaDriftReport:
        return self.catalog.get_schema_drift_report(report_id)

    def create_modeling_quality_report(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        schema_snapshot_hash: str,
    ) -> ModelingQualityReport:
        if self._quality_profiler is None:
            raise ValueError("modeling quality profiler is not configured")
        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=schema_snapshot_hash,
        )
        report = self._quality_profiler.profile(revision)
        self.catalog.save_modeling_quality_report(report)
        return report

    def get_current_evaluation_report(self, revision_id: str) -> EvaluationReport | None:
        """取当前语义版本对应的最新黄金评测报告,没有则 None。

        与发布门禁用同一个键(spec_hash),前端因此能在按钮上如实反映评测这道门,
        而不是等用户点了发布才被 409 顶回来。
        """

        revision = self.catalog.get_revision(revision_id)
        return self.catalog.get_latest_evaluation(revision.semantic_spec.spec_hash)

    def get_current_modeling_quality_report(self, revision_id: str) -> ModelingQualityReport | None:
        """取当前语义内容对应的最新质量报告,没有则返回 None。

        报告按 semantic_evidence_hash 绑定,与发布门禁用同一个键:改了影响关系、
        聚合或主题范围的东西,旧证据自动失效,这里也就取不到——前端因此不会拿
        一份过期报告点亮发布按钮。
        """

        revision = self.catalog.get_revision(revision_id)
        return self.catalog.get_latest_modeling_quality_report(
            semantic_evidence_hash(revision.semantic_spec)
        )

    def get_modeling_quality_report(self, report_id: str) -> ModelingQualityReport:
        return self.catalog.get_modeling_quality_report(report_id)

    def review_modeling_quality_report(
        self,
        *,
        revision_id: str,
        report_id: str,
        expected_etag: int,
        expected_content_hash: str,
        decisions: tuple[MetricPreviewDecision, ...],
        reviewed_by: str,
    ) -> ModelingQualityReport:
        if self._quality_profiler is None:
            raise ValueError("modeling quality profiler is not configured")
        revision = self.catalog.get_revision(revision_id)
        report = self.catalog.get_modeling_quality_report(report_id)
        if modeling_quality_report_is_stale(
            report,
            revision,
            expected_etag=expected_etag,
            expected_content_hash=expected_content_hash,
        ):
            raise RevisionConflictError(
                "modeling quality report is stale; regenerate it before review"
            )
        reviewed = self._quality_profiler.review(
            report,
            decisions=decisions,
            reviewed_by=reviewed_by,
        )
        self.catalog.review_modeling_quality_report(
            reviewed,
            previous_etag=report.etag,
            previous_content_hash=report.content_hash,
        )
        return reviewed

    def save_golden_suite(
        self,
        *,
        revision_id: str,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        suite: GoldenSuite,
        saved_by: str,
    ) -> GoldenSuiteRecord:
        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
        )
        if suite.project_id != revision.project_id:
            raise ValueError("golden suite belongs to another project")
        record = GoldenSuiteRecord(
            id=suite.id,
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            semantic_spec_hash=revision.semantic_spec.spec_hash,
            suite=suite,
            saved_by=saved_by,
            updated_at=datetime.now(UTC),
        )
        self.catalog.save_golden_suite(record)
        return record

    def list_projects(self, *, id_prefix: str | None = None, limit: int = 200):
        return self.catalog.list_projects(id_prefix=id_prefix, limit=limit)

    def list_releases(self, project_id: str, *, limit: int = 50):
        return self.catalog.list_releases(project_id=project_id, limit=limit)

    def publish_gate(self) -> dict[str, int | float]:
        return self._publisher.gate_thresholds

    def list_query_failures(self, project_id: str, *, limit: int = 100):
        """最近被拒答的问题。只读，给建模者看"系统听不懂什么"。"""

        return self.catalog.list_failures(project_id=project_id, limit=limit)

    def list_confirmation_memories(self, *, project_id: str, actor_id: str):
        return self.catalog.list_confirmation_memories(
            actor_id=actor_id,
            project_id=project_id,
        )

    def revoke_confirmation_memory(
        self,
        *,
        project_id: str,
        actor_id: str,
        memory_id: str,
    ) -> bool:
        return self.catalog.revoke_confirmation_memory(
            memory_id=memory_id,
            actor_id=actor_id,
            project_id=project_id,
            revoked_at=datetime.now(UTC),
        )

    def list_confirmation_suggestions(self, *, project_id: str, actor_id: str):
        from knowflow_analytics.query.confirmation_memory import ConfirmationSuggestion

        grouped: dict[tuple[str, str, str | None, str | None], list] = {}
        for memory in self.catalog.list_confirmation_memories(
            actor_id=actor_id,
            project_id=project_id,
        ):
            key = (
                memory.normalized_phrase,
                memory.selection_kind,
                memory.semantic_element_id,
                memory.dataset_id,
            )
            grouped.setdefault(key, []).append(memory)
        return tuple(
            ConfirmationSuggestion(
                id=(
                    "csug_"
                    + content_hash(
                        {
                            "project_id": project_id,
                            "normalized_phrase": key[0],
                            "selection_kind": key[1],
                            "semantic_element_id": key[2],
                            "dataset_id": key[3],
                        }
                    ).removeprefix("sha256:")[:20]
                ),
                detected_text=max(items, key=lambda item: item.created_at).detected_text,
                selection_kind=key[1],
                semantic_element_id=key[2],
                dataset_id=key[3],
                confirmation_count=len(items),
                latest_confirmed_at=max(item.created_at for item in items),
            )
            for key, items in sorted(
                grouped.items(),
                key=lambda item: tuple("" if value is None else value for value in item[0]),
            )
        )

    def list_golden_suites(self, revision_id: str) -> tuple[GoldenSuiteRecord, ...]:
        revision = self.catalog.get_revision(revision_id)
        return tuple(
            item
            for item in self.catalog.list_golden_suites(
                project_id=revision.project_id,
                revision_id=revision.id,
            )
            if item.revision_etag == revision.etag
            and item.schema_snapshot_hash == revision.schema_snapshot_hash
            and item.semantic_spec_hash == revision.semantic_spec.spec_hash
        )

    def delete_golden_suite(self, *, revision_id: str, suite_id: str) -> bool:
        revision = self.catalog.get_revision(revision_id)
        return self.catalog.delete_golden_suite(
            suite_id=suite_id,
            project_id=revision.project_id,
            revision_id=revision.id,
        )

    def evaluate_revision(
        self,
        *,
        revision_id: str,
        suite: GoldenSuite,
        required_accuracy: float,
        expected_etag: int | None = None,
        expected_schema_snapshot_hash: str | None = None,
        saved_by: str = "evaluation",
        tenant_id: str = "",
    ) -> EvaluationReport:
        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
        )
        validated = self._revision_editor.validate_for_publish(revision)
        if suite.project_id != validated.project_id:
            raise ValueError("evaluation suite belongs to another project")
        self.save_golden_suite(
            revision_id=revision.id,
            expected_etag=revision.etag,
            expected_schema_snapshot_hash=revision.schema_snapshot_hash,
            suite=suite,
            saved_by=saved_by,
        )
        staged = self._stage_revision(validated, tenant_id=tenant_id)
        evaluator = GoldenEvaluator(
            self._build_query_service(_StagedReleaseProvider(staged)),
            actor_id=tenant_id or None,
        )
        report = evaluator.evaluate(suite, required_accuracy=required_accuracy)
        self.catalog.save_index_snapshot(
            project_id=validated.project_id,
            index_snapshot=staged.index_snapshot,
        )
        self.catalog.save_evaluation_report(report)
        return report

    def preview_revision_query(
        self,
        *,
        revision_id: str,
        request: QueryRequest,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        now: datetime | None = None,
        actor_id: str | None = None,
        permission_scope_hash: str | None = None,
    ) -> QueryResponse:
        """Execute one question against an explicitly reviewed draft version.

        A selected semantic interpretation goes through the same semantic-layer
        query path used in normal chat.  This preserves that
        path while substituting a version-bound staged release, so a reviewer can
        verify a candidate before it becomes the active release.
        """

        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
        )
        if revision.state is not RevisionState.VALIDATED:
            raise SemanticValidationError(
                "revision must pass structural validation before query preview",
                code="NOT_VALIDATED",
            )
        if request.project_id != revision.project_id:
            raise RevisionConflictError("query preview belongs to another project")
        staged = self._stage_revision(
            # A RAGFlow tenant id is the signed-in user's id.
            revision,
            tenant_id=str(actor_id or ""),
            index_snapshot_id=(
                request.expected_index_snapshot_id
                if request.selected_candidate_id is not None
                else None
            ),
        )
        response = self._build_query_service(_StagedReleaseProvider(staged)).query(
            request,
            now=now,
            actor_id=actor_id,
        )
        if (
            request.selected_candidate_id is None
            and response.state is QueryState.CLARIFICATION_REQUIRED
        ):
            # Clarification candidates are meaningful only against the exact index
            # that produced them. Real embedding services may return slightly
            # different floats for identical inputs, so rebuilding on selection can
            # manufacture a new snapshot ID for an unchanged Revision. The
            # chatParse contract resubmits queryId/parseId/selectedParse against the
            # originating parse; persist the equivalent first-stage index and reload
            # it through KnowFlow's signed selection contract.
            self.catalog.save_index_snapshot(
                project_id=revision.project_id,
                index_snapshot=staged.index_snapshot,
            )
        self._save_query_diagnostic_best_effort(
            request=request,
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode="natural",
            revision=revision,
        )
        return response

    def preview_revision_structured_query(
        self,
        *,
        revision_id: str,
        project_id: str,
        semantic_query: SemanticQuery,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        now: datetime | None = None,
        include_debug_sql: bool = False,
        actor_id: str | None = None,
        permission_scope_hash: str | None = None,
    ) -> QueryResponse:
        """Preview a QueryStructReq-equivalent against one validated Revision."""

        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
        )
        if revision.state is not RevisionState.VALIDATED:
            raise SemanticValidationError(
                "revision must pass structural validation before query preview",
                code="NOT_VALIDATED",
            )
        if project_id != revision.project_id:
            raise RevisionConflictError("query preview belongs to another project")
        # The structured-query path starts at StructQueryParser and has no
        # Mapper/index dependency. Bind the reviewed Candidate directly instead
        # of calling _stage_revision(), which intentionally builds embeddings for
        # natural-language parsing.
        staged_release = revision.semantic_spec.model_copy(
            update={
                "id": f"staged:{revision.id}",
                "revision_id": revision.id,
                "index_snapshot_id": None,
            }
        )
        request = StructuredQueryRequest(
            project_id=project_id,
            semantic_query=semantic_query,
            include_debug_sql=include_debug_sql,
        )
        response = self._query_service.query_structured(
            request,
            now=now,
            semantic_release=staged_release,
        )
        self._save_query_diagnostic_best_effort(
            request=request,
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode="structured",
            revision=revision,
        )
        return response

    def publish_revision(
        self,
        revision_id: str,
        *,
        expected_etag: int | None = None,
        expected_schema_snapshot_hash: str | None = None,
    ) -> PublishedRelease:
        revision = self.catalog.get_revision(revision_id)
        self._require_revision_version(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
        )
        return self._publisher.publish(revision)

    def get_release(self, release_id: str) -> PublishedRelease:
        return self.catalog.get_release(release_id)

    def query(
        self,
        request: QueryRequest,
        *,
        actor_id: str | None = None,
        permission_scope_hash: str | None = None,
        on_trace: Callable[[QueryTraceStep], None] | None = None,
    ) -> QueryResponse:
        response = self._query_service.query(request, actor_id=actor_id, on_trace=on_trace)
        self._save_query_diagnostic_best_effort(
            request=request,
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode="natural",
        )
        return response

    def structured_query(
        self,
        request: StructuredQueryRequest,
        *,
        actor_id: str | None = None,
        permission_scope_hash: str | None = None,
    ) -> QueryResponse:
        """Execute one governed QueryStructReq against the Active Release.

        Same authority chain as natural-language Ask from the Corrector on;
        no Mapper, no LLM, no semantic index.
        """

        response = self._query_service.query_structured(request, actor_id=actor_id)
        self._save_query_diagnostic_best_effort(
            request=request,
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode="structured",
        )
        return response

    def drilldown_query(
        self,
        *,
        project_id: str,
        query_id: str,
        token: str,
        actor_id: str,
        permission_scope_hash: str,
        value: str | None = None,
        allowed_element_ids: tuple[str, ...] | None = None,
        row_filters: tuple[QueryRowFilter, ...] | None = None,
    ) -> QueryResponse:
        """Continue a completed answer by one signed drilldown option.

        The base semantics are recovered from the persisted query artifact in
        the exact (actor, project, scope, query) slot — the client only returns
        the opaque token it was shown.
        """

        try:
            artifact = self.catalog.get_query_diagnostic(
                actor_id=actor_id,
                project_id=project_id,
                permission_scope_hash=permission_scope_hash,
                query_id=query_id,
            )
        except CatalogError as exc:
            raise SemanticParsingError(
                "下钻已过期或不属于当前查询，请重新提问",
                code="STALE_QUERY_SELECTION",
            ) from exc
        base_raw = artifact.response.get("semantic_query")
        if not isinstance(base_raw, dict):
            raise SemanticParsingError(
                "该回答不支持下钻，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        try:
            base_query = SemanticQuery.model_validate(base_raw)
        except ValidationError as exc:
            raise SemanticParsingError(
                "该回答不支持下钻，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            ) from exc
        response = self._query_service.query_drilldown(
            project_id=project_id,
            query_id=query_id,
            token=token,
            base_query=base_query,
            base_release_id=artifact.release_id,
            base_spec_hash=artifact.spec_hash,
            actor_id=actor_id,
            value=value,
            allowed_element_ids=allowed_element_ids,
            row_filters=row_filters,
        )
        # 诊断里的 request 记实际执行的 continuation；链式下钻的语义恢复
        # 走 artifact.response.semantic_query，不依赖这里。
        executed_query = (
            response.semantic_query
            if isinstance(response, CompletedQueryResponse)
            else base_query
        )
        self._save_query_diagnostic_best_effort(
            request=StructuredQueryRequest(
                project_id=project_id,
                semantic_query=executed_query,
            ),
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode="structured",
        )
        return response

    def export_query_diagnostic(
        self,
        *,
        project_id: str,
        query_id: str,
        actor_id: str,
        permission_scope_hash: str,
        allow_debug_sql: bool = False,
    ) -> QueryDiagnosticExport:
        diagnostic_key = (actor_id, project_id, permission_scope_hash, query_id)
        self._query_diagnostic_recorder.wait_for(
            diagnostic_key,
            timeout=self._query_diagnostic_export_wait_seconds,
        )
        artifact = self.catalog.get_query_diagnostic(
            actor_id=actor_id,
            project_id=project_id,
            permission_scope_hash=permission_scope_hash,
            query_id=query_id,
        )
        context: dict[str, object | None] = {
            "release": None,
            "revision": None,
            "catalog": None,
            "metric_catalog": [
                {
                    "id": item.metric_id,
                    "name": item.metric_name,
                    "model_id": item.model_id,
                    "aggregation": item.aggregation,
                }
                for item in artifact.metric_aggregation_snapshot
            ],
            "index_snapshot": None,
            "schema_snapshot": None,
            "modeling_diagnostics": None,
            "modeling_job": None,
            "modeling_proposal": None,
            "modeling_run": None,
        }
        version_status = "VERSION_UNAVAILABLE"
        revision = None
        if artifact.revision_id is not None:
            try:
                candidate = self.catalog.get_revision(artifact.revision_id)
                if candidate.project_id == project_id:
                    revision = candidate
            except Exception as exc:  # noqa: BLE001 - context sections degrade independently
                LOGGER.warning(
                    "query diagnostic revision context unavailable error_type=%s",
                    type(exc).__name__,
                )
        if revision is not None:
            version_matches = (
                artifact.revision_etag == revision.etag
                and artifact.revision_schema_snapshot_hash == revision.schema_snapshot_hash
                and artifact.revision_semantic_spec_hash == revision.semantic_spec.spec_hash
            )
            version_status = "CURRENT" if version_matches else "VERSION_STALE"
            if version_matches:
                context["revision"] = revision.model_dump(mode="json")
                context["catalog"] = (
                    revision.semantic_catalog.model_dump(mode="json")
                    if revision.semantic_catalog is not None
                    else None
                )
                if not context["metric_catalog"]:
                    context["metric_catalog"] = self._metric_catalog_projection(
                        revision.semantic_spec.metrics
                    )
                try:
                    context["modeling_diagnostics"] = self.get_revision_diagnostics(
                        revision.id
                    ).model_dump(mode="json")
                except Exception as exc:  # noqa: BLE001 - optional export enrichment
                    LOGGER.warning(
                        "query diagnostic modeling diagnostics unavailable error_type=%s",
                        type(exc).__name__,
                    )
            else:
                # The mutable Draft is deliberately absent. The small
                # query-time metric snapshot on the artifact is authoritative;
                # presenting today's Catalog under this timeline would make an
                # old SUM/AVG decision look as though it came from the query.
                context["revision"] = {
                    "_version_notice": (
                        "VERSION_STALE: current Draft content is intentionally omitted"
                    ),
                    "query_binding": {
                        "revision_id": artifact.revision_id,
                        "etag": artifact.revision_etag,
                        "schema_snapshot_hash": artifact.revision_schema_snapshot_hash,
                        "semantic_spec_hash": artifact.revision_semantic_spec_hash,
                    },
                }
            snapshot_hash = artifact.revision_schema_snapshot_hash
            snapshot_id = (
                f"schema_{snapshot_hash.removeprefix('sha256:')[:16]}" if snapshot_hash else None
            )
            try:
                if snapshot_id is not None:
                    context["schema_snapshot"] = self.catalog.get_schema_snapshot(
                        snapshot_id,
                        project_id=project_id,
                    ).model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 - optional export enrichment
                LOGGER.warning(
                    "query diagnostic schema snapshot unavailable error_type=%s",
                    type(exc).__name__,
                )
        if artifact.release_id and not artifact.release_id.startswith("staged:"):
            try:
                published = self.catalog.get_release(artifact.release_id)
                if (
                    published.release.project_id == project_id
                    and published.release.spec_hash == artifact.spec_hash
                ):
                    context["release"] = _diagnostic_published_release_projection(published)
                    if not context["metric_catalog"]:
                        context["metric_catalog"] = self._metric_catalog_projection(
                            published.release.metrics
                        )
            except Exception as exc:  # noqa: BLE001 - optional export enrichment
                LOGGER.warning(
                    "query diagnostic release context unavailable error_type=%s",
                    type(exc).__name__,
                )
        elif artifact.release_id:
            context["release"] = {
                "id": artifact.release_id,
                "revision_id": artifact.revision_id,
                "spec_hash": artifact.spec_hash,
                "index_snapshot_id": artifact.index_snapshot_id,
                "kind": "staged_revision_preview",
            }
        if artifact.index_snapshot_id is not None:
            try:
                index_snapshot = self.catalog.get_index_snapshot(
                    artifact.index_snapshot_id,
                    project_id=project_id,
                )
                context["index_snapshot"] = _diagnostic_index_snapshot_projection(index_snapshot)
            except Exception as exc:  # noqa: BLE001 - optional export enrichment
                LOGGER.warning(
                    "query diagnostic index snapshot unavailable error_type=%s",
                    type(exc).__name__,
                )
        self._load_query_diagnostic_modeling_artifacts(
            artifact=artifact,
            project_id=project_id,
            context=context,
        )
        return render_query_diagnostic_export(
            artifact,
            context=context,
            version_status=version_status,
            allow_debug_sql=allow_debug_sql,
        )

    def _save_query_diagnostic_best_effort(
        self,
        *,
        request: QueryRequest | StructuredQueryRequest,
        response: QueryResponse,
        actor_id: str | None,
        permission_scope_hash: str | None,
        mode: Literal["natural", "structured"],
        revision: ModelingRevision | None = None,
    ) -> None:
        """Enqueue after response formation without waiting for storage I/O."""

        if not actor_id or not permission_scope_hash:
            return
        record = _QueryDiagnosticRecord(
            request=request,
            response=response,
            actor_id=actor_id,
            permission_scope_hash=permission_scope_hash,
            mode=mode,
            revision=revision,
        )
        if not self._query_diagnostic_recorder.submit(record):
            LOGGER.warning("query diagnostic queue full; dropping newest artifact")

    def _persist_query_diagnostic(self, record: _QueryDiagnosticRecord) -> None:
        bound_revision = record.revision
        metrics = bound_revision.semantic_spec.metrics if bound_revision is not None else ()
        if (
            bound_revision is None
            and record.response.release_id
            and not record.response.release_id.startswith("staged:")
        ):
            published = self.catalog.get_release(record.response.release_id)
            if published.release.project_id == record.request.project_id:
                metrics = published.release.metrics
                if published.release.revision_id is not None:
                    candidate = self.catalog.get_revision(published.release.revision_id)
                    if candidate.project_id == record.request.project_id:
                        bound_revision = candidate
        artifact = build_query_diagnostic_artifact(
            request=record.request,
            response=record.response,
            actor_id=record.actor_id,
            permission_scope_hash=record.permission_scope_hash,
            mode=record.mode,
            revision_id=bound_revision.id if bound_revision is not None else None,
            revision_etag=bound_revision.etag if bound_revision is not None else None,
            revision_schema_snapshot_hash=(
                bound_revision.schema_snapshot_hash if bound_revision is not None else None
            ),
            revision_semantic_spec_hash=(
                bound_revision.semantic_spec.spec_hash if bound_revision is not None else None
            ),
            modeling_job_id=(
                bound_revision.modeling_job_id if bound_revision is not None else None
            ),
            metric_aggregation_snapshot=self._metric_aggregation_snapshot(
                record.response,
                metrics,
            ),
            ttl_seconds=self._query_diagnostic_ttl_seconds,
            max_result_rows=self._query_diagnostic_result_rows,
        )
        self.catalog.save_query_diagnostic(artifact)

    @staticmethod
    def _metric_catalog_projection(metrics: Sequence) -> list[dict[str, object | None]]:
        return [
            {
                "id": metric.id,
                "name": metric.name,
                "model_id": metric.model_id,
                "aggregation": (
                    metric.aggregation.value if metric.aggregation is not None else None
                ),
            }
            for metric in metrics
        ]

    @staticmethod
    def _metric_aggregation_snapshot(
        response: QueryResponse,
        metrics: Sequence,
    ) -> tuple[QueryDiagnosticMetricAggregation, ...]:
        if not isinstance(response, CompletedQueryResponse):
            return ()
        by_id = {metric.id: metric for metric in metrics}
        snapshot: list[QueryDiagnosticMetricAggregation] = []
        for metric_id in response.semantic_query.metric_ids:
            metric = by_id.get(metric_id)
            if metric is None:
                continue
            snapshot.append(
                QueryDiagnosticMetricAggregation(
                    metric_id=metric.id,
                    metric_name=metric.name,
                    model_id=metric.model_id,
                    aggregation=(
                        metric.aggregation.value if metric.aggregation is not None else None
                    ),
                )
            )
        return tuple(snapshot)

    def _load_query_diagnostic_modeling_artifacts(
        self,
        *,
        artifact,
        project_id: str,
        context: dict[str, object | None],
    ) -> None:
        if artifact.modeling_job_id is None:
            return
        try:
            job = self.catalog.get_modeling_job(artifact.modeling_job_id)
            if job.project_id != project_id or job.revision_id != artifact.revision_id:
                return
            context["modeling_job"] = job.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - optional export enrichment
            LOGGER.warning(
                "query diagnostic modeling job unavailable error_type=%s",
                type(exc).__name__,
            )
            return
        if job.proposal_id is None:
            return
        try:
            proposal = self.catalog.get_modeling_proposal(job.proposal_id)
            if proposal.project_id != project_id or proposal.revision_id != artifact.revision_id:
                return
            context["modeling_proposal"] = proposal.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - optional export enrichment
            LOGGER.warning(
                "query diagnostic modeling proposal unavailable error_type=%s",
                type(exc).__name__,
            )
            return
        try:
            run = self.catalog.get_modeling_run(proposal.suggestion_run_id)
            if run.project_id == project_id and run.revision_id == artifact.revision_id:
                context["modeling_run"] = run.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - optional export enrichment
            LOGGER.warning(
                "query diagnostic modeling run unavailable error_type=%s",
                type(exc).__name__,
            )

    def _stage_revision(
        self,
        revision: ModelingRevision,
        *,
        index_snapshot_id: str | None = None,
        tenant_id: str = "",
    ) -> PublishedRelease:
        index = None
        if index_snapshot_id is not None:
            try:
                candidate = self.catalog.get_index_snapshot(
                    index_snapshot_id,
                    project_id=revision.project_id,
                )
            except CatalogError:
                candidate = None
            if (
                candidate is not None
                and candidate.release_spec_hash == revision.semantic_spec.spec_hash
            ):
                index = candidate
        if index is None:
            # Building an index embeds every governed phrase, so it must run as
            # the tenant that owns the request's embedding model.
            gateway = (
                self._embedding_gateway.for_tenant(tenant_id)
                if tenant_id
                else self._embedding_gateway
            )
            index = SemanticIndexBuilder(gateway).build(revision.semantic_spec)
        staged_release = revision.semantic_spec.model_copy(
            update={
                "id": f"staged:{revision.id}",
                "revision_id": revision.id,
                "index_snapshot_id": index.id,
            }
        )
        return PublishedRelease(
            release=staged_release,
            index_snapshot=index,
            status="active",
        )

    @staticmethod
    def _require_revision_version(
        revision: ModelingRevision,
        *,
        expected_etag: int | None,
        expected_schema_snapshot_hash: str | None,
    ) -> None:
        if expected_etag is not None and revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before continuing")
        if (
            expected_schema_snapshot_hash is not None
            and revision.schema_snapshot_hash != expected_schema_snapshot_hash
        ):
            raise RevisionConflictError("schema snapshot changed; reload before continuing")

    def _build_query_service(self, release_provider: ReleaseProvider) -> AnalyticsQueryService:
        return AnalyticsQueryService(
            releases=release_provider,
            orchestrator=CandidateOrchestrator(
                mapper=SemanticMapper(
                    embedding_gateway=self._embedding_gateway,
                    llm_enabled=self._llm_parser is not None,
                ),
                llm_parser=self._llm_parser,
                textual_corrector=self._textual_corrector,
            ),
            translator=SemanticTranslator(),
            physical_sql_corrector=self._physical_sql_corrector,
            executor=self._executor,
            multi_turn_rewriter=self._multi_turn_rewriter,
            query_history=self.catalog,
            query_failures=self.catalog,
            dry_run_before_execute=self._dry_run_before_execute,
            selection_secret=self._selection_secret,
            weak_metric_adjudicator=self._weak_metric_adjudicator,
            weak_metric_adjudication_mode=self._weak_metric_adjudication_mode,
            intent_adjudicator=self._intent_adjudicator,
            semantic_intent_adjudication_mode=self._semantic_intent_adjudication_mode,
            analysis_object_adjudication_mode=self._analysis_object_adjudication_mode,
            confirmation_memories=self.catalog,
            confirmation_memory_ttl_seconds=self._confirmation_memory_ttl_seconds,
        )
