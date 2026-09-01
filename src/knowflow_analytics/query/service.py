from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import product
from typing import Protocol

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    FilterOperator,
    QueryFilter,
    QueryResult,
    SemanticQuery,
    SemanticQueryType,
    SemanticRelease,
)
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.query.ambiguity import (
    SemanticDecisionObligation,
    SemanticValueBinding,
    same_name_ambiguities,
    same_name_ambiguity,
    settle_after_parse,
)
from knowflow_analytics.query.confirmation_memory import (
    ConfirmationMemory,
    ConfirmationMemoryStore,
    confirmation_candidate_set_hash,
)
from knowflow_analytics.query.contracts import (
    ClarificationOption,
    ClarificationQueryResponse,
    CompletedQueryResponse,
    DrilldownOption,
    FailedQueryResponse,
    MapMode,
    MappingEvidence,
    MappingEvidenceChannel,
    MappingResult,
    MatchMethod,
    ParsedSemanticCandidate,
    QueryDiagnosis,
    QueryDiagnosticCategory,
    QueryError,
    QueryFailureRecord,
    QueryInterpretation,
    QueryRequest,
    QueryResponse,
    QueryStage,
    QueryTraceStep,
    SchemaMatch,
    SemanticAmbiguityGroup,
    SemanticAmbiguityMember,
    SemanticDecision,
    SemanticDecisionSource,
    StructuredQueryRequest,
)
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector
from knowflow_analytics.query.errors import (
    ClarificationSignal,
    MappingError,
    SemanticParsingError,
)
from knowflow_analytics.query.failures import QueryFailureStore
from knowflow_analytics.query.intent import reject_unsupported_intent
from knowflow_analytics.query.intent_adjudicator import (
    IntentAdjudicationBatchResult,
    IntentAdjudicationCandidate,
    IntentAdjudicationDecision,
    IntentAdjudicationGroup,
    IntentAdjudicationResult,
    IntentAdjudicator,
)
from knowflow_analytics.query.multi_turn import (
    MultiTurnContext,
    MultiTurnRewriter,
    QueryHistoryStore,
    QueryHistoryTurn,
)
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.rules import QueryRuleEngine
from knowflow_analytics.query.scope_resolver import (
    QueryScopeResolutionStatus,
    QueryScopeResolver,
)
from knowflow_analytics.query.weak_metric_adjudicator import (
    WeakMetricAdjudicationDecision,
    WeakMetricAdjudicationMode,
    WeakMetricAdjudicationResult,
    WeakMetricAdjudicator,
)
from knowflow_analytics.semantic.index import SemanticElementType, normalize_text
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator
from knowflow_analytics.semantic.translator import SemanticTranslator

LOGGER = logging.getLogger(__name__)
_MAX_SEMANTIC_CONFIRMATION_OPTIONS = 20
_SELECTION_TOKEN_VERSION = "sel1"
# Drilldown continuations reuse the sel1 machinery: opaque HMAC refs bound to
# the exact actor/project/query/release context, recovered by re-enumeration.
_DRILLDOWN_TOKEN_VERSION = "drl1"
_MAX_DRILLDOWN_DIMENSIONS = 12
_MAX_DRILLDOWN_METRICS = 8


@dataclass(frozen=True)
class _SelectionTokenContext:
    project_id: str
    actor_id: str
    question: str
    dataset_ids: tuple[str, ...]
    release_id: str
    spec_hash: str
    index_snapshot_id: str
    conversation_id: str | None
    semantic_now: str | None


class ReleaseProvider(Protocol):
    def get_active_release(self, project_id: str) -> PublishedRelease: ...


class QueryExecutor(Protocol):
    def execute(self, *, query: object, release: SemanticRelease) -> QueryResult: ...


class AnalyticsQueryService:
    def __init__(
        self,
        *,
        releases: ReleaseProvider,
        orchestrator: CandidateOrchestrator,
        translator: SemanticTranslator,
        s2sql_translator: S2SqlSemanticTranslator | None = None,
        physical_sql_corrector: LlmPhysicalSqlCorrector | None = None,
        executor: QueryExecutor,
        multi_turn_rewriter: MultiTurnRewriter | None = None,
        query_history: QueryHistoryStore | None = None,
        query_failures: QueryFailureStore | None = None,
        query_rule_engine: QueryRuleEngine | None = None,
        dry_run_before_execute: bool = False,
        selection_secret: str | bytes | None = None,
        selection_token_ttl_seconds: int = 900,
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
        confirmation_memories: ConfirmationMemoryStore | None = None,
        confirmation_memory_ttl_seconds: int = 2_592_000,
    ) -> None:
        self._releases = releases
        self._orchestrator = orchestrator
        self._translator = translator
        self._s2sql_translator = s2sql_translator or S2SqlSemanticTranslator()
        self._physical_sql_corrector = physical_sql_corrector or LlmPhysicalSqlCorrector()
        self._executor = executor
        self._multi_turn_rewriter = multi_turn_rewriter
        self._query_history = query_history
        self._query_failures = query_failures
        self._query_rule_engine = query_rule_engine or QueryRuleEngine()
        self._dry_run_before_execute = dry_run_before_execute
        secret = (
            selection_secret.encode("utf-8")
            if isinstance(selection_secret, str)
            else selection_secret
            if selection_secret is not None
            else secrets.token_bytes(32)
        )
        if len(secret) < 32:
            raise ValueError("selection secret must contain at least 32 bytes")
        if selection_token_ttl_seconds <= 0:
            raise ValueError("selection token ttl must be positive")
        self._selection_secret = secret
        self._selection_token_ttl_seconds = selection_token_ttl_seconds
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
        if confirmation_memory_ttl_seconds <= 0:
            raise ValueError("confirmation memory ttl must be positive")
        self._confirmation_memories = confirmation_memories
        self._confirmation_memory_ttl_seconds = confirmation_memory_ttl_seconds

    def query(
        self,
        request: QueryRequest,
        *,
        now: datetime | None = None,
        actor_id: str | None = None,
    ) -> QueryResponse:
        query_id = request.query_id or f"q_{uuid.uuid4().hex}"
        tenant_id = str(actor_id or "").strip()
        decision_now = datetime.now(UTC)
        trace: list[QueryTraceStep] = [QueryTraceStep(stage=QueryStage.PRECHECK, status="started")]
        parse_events: list[dict[str, object]] = []
        diagnostic_context: dict[str, object] = {}
        published: PublishedRelease | None = None
        selection_context: _SelectionTokenContext | None = None
        selected_scope_dataset_id: str | None = None
        ai_adjudicated_metric_id: str | None = None
        ai_adjudicated_metric_ids: tuple[str, ...] = ()
        memory_confirmed_metric_id: str | None = None
        memory_confirmed_metric_ids: tuple[str, ...] = ()
        semantic_intent_adopted = False
        semantic_intent_adjudication_detail: dict[str, object] | None = None
        analysis_object_adjudication_detail: dict[str, object] | None = None
        decision_obligations: list[SemanticDecisionObligation] = []
        automatic_decisions: list[SemanticDecision] = []
        pending_confirmation_memories: list[ConfirmationMemory] = []
        confirmation_memory_detail: dict[str, object] | None = None
        confirmation_memory_unavailable = False
        weak_metric_adjudication_detail: dict[str, object] | None = None
        semantic_clarification_group: SemanticAmbiguityGroup | None = None
        option_dataset_ids: tuple[str, ...] = ()
        # 映射失败发生在多轮改写之前，那时 effective_question 还没赋值；失败记录
        # 要用到它，所以先按原问题初始化，改写成功后再覆盖。
        effective_question = request.question
        try:
            published = self._releases.get_active_release(request.project_id)
            release = published.release
            index = published.index_snapshot
            if (
                release.index_snapshot_id != index.id
                or release.spec_hash != index.release_spec_hash
            ):
                raise SemanticParsingError(
                    "语义模型与索引版本不一致", code="RELEASE_INDEX_MISMATCH"
                )
            available = {item.id for item in release.datasets}
            dataset_ids = request.dataset_ids or tuple(sorted(available))
            if not dataset_ids or not set(dataset_ids).issubset(available):
                raise SemanticParsingError(
                    "请求的数据集不属于当前发布版本", code="DATASET_SCOPE_VIOLATION"
                )
            option_dataset_ids = dataset_ids
            selection_context = self._selection_context(
                request=request,
                published=published,
                dataset_ids=dataset_ids,
                actor_id=tenant_id,
                semantic_now=now,
            )
            (
                candidate_selection_id,
                selected_element_id,
                selected_element_type,
                selected_time_dimension_id,
                selected_metric_bundle_ids,
            ) = self._selection(
                request,
                published,
                selection_context=selection_context,
            )
            effective_selected_element_id = selected_element_id
            effective_selected_element_type = selected_element_type
            selected_scope_element_id = selected_element_id or selected_time_dimension_id
            global_router_before_selection = self._supports_global_scope_routing(
                release,
                dataset_ids,
            )
            # A confirmed semantic choice must be revalidated against the same
            # global evidence that produced its card. Pre-narrow only the legacy
            # time continuation; otherwise a competing root/member can disappear
            # before the signed choice is reconstructed and settled.
            keep_global_selection_evidence = global_router_before_selection and (
                selected_element_id is not None or selected_metric_bundle_ids
            )
            if selected_scope_element_id is not None and not keep_global_selection_evidence:
                dataset_ids = self._scope_datasets_to_selected_element(
                    release,
                    dataset_ids,
                    selected_scope_element_id,
                    element_type=selected_element_type,
                    require_time=selected_time_dimension_id is not None,
                )
                option_dataset_ids = dataset_ids
            reject_unsupported_intent(request.question)
            trace[-1] = QueryTraceStep(
                stage=QueryStage.PRECHECK,
                status="completed",
                detail={
                    "release_id": release.id,
                    "spec_hash": release.spec_hash,
                    "index_snapshot_id": index.id,
                },
            )
            trace.append(QueryTraceStep(stage=QueryStage.CANDIDATE_DISCOVERY, status="started"))
            global_evidence = None
            scope_resolution = None
            use_global_scope_router = self._supports_global_scope_routing(
                release,
                dataset_ids,
            )
            if use_global_scope_router:
                global_evidence = self._orchestrator.collect_evidence(
                    question=request.question,
                    dataset_ids=dataset_ids,
                    index=index,
                    tenant_id=tenant_id,
                )
                if selected_metric_bundle_ids:
                    offered = self._weak_metric_confirmation(
                        release=release,
                        evidence=global_evidence,
                        dataset_ids=dataset_ids,
                        selection_context=selection_context,
                    )
                    chosen_bundle = next(
                        (
                            item
                            for item in (offered[3] if offered is not None else ())
                            if item.dataset_id == candidate_selection_id
                            and tuple(
                                selection_id.removeprefix("element:metric:")
                                for selection_id in item.semantic_selection_ids
                            )
                            == selected_metric_bundle_ids
                        ),
                        None,
                    )
                    if offered is None or offered[0] != "metric_multi" or chosen_bundle is None:
                        raise MappingError(
                            "组合确认项不属于当前问题实际展示的候选。",
                            code="CANDIDATE_NOT_FOUND",
                        )
                    phrase_groups = offered[5]
                    selected_set = set(selected_metric_bundle_ids)
                    selected_dataset = next(
                        item for item in release.datasets if item.id == candidate_selection_id
                    )
                    selected_dataset_metrics = set(selected_dataset.metric_ids)
                    selected_by_group: list[
                        tuple[str, ClarificationOption, tuple[ClarificationOption, ...]]
                    ] = []
                    for detected_text, group_options in phrase_groups:
                        choices = tuple(
                            item for item in group_options if item.element_id in selected_set
                        )
                        if len(choices) != 1:
                            raise MappingError(
                                "组合确认项没有为每个业务说法选择唯一指标。",
                                code="CANDIDATE_NOT_FOUND",
                            )
                        scoped_options = tuple(
                            item.model_copy(
                                update={
                                    "candidate_id": self._selection_token(
                                        release=release,
                                        context=selection_context,
                                        dataset_id=candidate_selection_id,
                                        semantic_selection_id=(f"element:metric:{item.element_id}"),
                                    ),
                                    "dataset_id": candidate_selection_id or item.dataset_id,
                                }
                            )
                            for item in group_options
                            if item.element_id in selected_dataset_metrics
                        )
                        chosen = next(
                            item
                            for item in scoped_options
                            if item.element_id == choices[0].element_id
                        )
                        selected_by_group.append((detected_text, chosen, scoped_options))
                    if {
                        item.element_id for _text, item, _options in selected_by_group
                    } != selected_set:
                        raise MappingError(
                            "组合确认项与当前业务说法不一致。",
                            code="CANDIDATE_NOT_FOUND",
                        )
                    for detected_text, chosen, group_options in selected_by_group:
                        obligation = self._decision_obligation(
                            release=release,
                            detected_text=detected_text,
                            source=SemanticDecisionSource.HUMAN,
                            chosen=chosen,
                            options=group_options,
                        )
                        if obligation is not None:
                            decision_obligations.append(obligation)
                        pending_memory = self._confirmation_memory_from_choice(
                            actor_id=tenant_id,
                            project_id=request.project_id,
                            release=release,
                            index_snapshot_id=index.id,
                            detected_text=detected_text,
                            chosen=chosen,
                            options=group_options,
                            exact_context_hash=self._exact_semantic_binding_hash(global_evidence),
                            now=decision_now,
                        )
                        if pending_memory is not None:
                            pending_confirmation_memories.append(pending_memory)
                elif selected_element_id is not None and selected_element_type is not None:
                    offered = self._semantic_confirmation_for_current_evidence(
                        release=release,
                        evidence=global_evidence,
                        dataset_ids=dataset_ids,
                        selection_context=selection_context,
                        question=request.question,
                        now=now,
                        selected_element_id=selected_element_id,
                        selected_element_type=selected_element_type,
                        selected_dataset_id=candidate_selection_id,
                    )
                    offered_detected_text = offered[0] if offered is not None else ""
                    offered_options = offered[1] if offered is not None else ()
                    chosen_option = next(
                        (
                            item
                            for item in offered_options
                            if item.element_type == selected_element_type.value
                            and item.element_id == selected_element_id
                            and (
                                candidate_selection_id is None
                                or item.dataset_id == candidate_selection_id
                            )
                        ),
                        None,
                    )
                    if chosen_option is None:
                        raise MappingError(
                            "确认的语义对象不属于当前问题实际展示的候选。",
                            code="CANDIDATE_NOT_FOUND",
                        )
                    obligation = self._decision_obligation(
                        release=release,
                        detected_text=offered_detected_text,
                        source=SemanticDecisionSource.HUMAN,
                        chosen=chosen_option,
                        options=offered_options,
                    )
                    if obligation is not None:
                        decision_obligations.append(obligation)
                    pending_memory = self._confirmation_memory_from_choice(
                        actor_id=tenant_id,
                        project_id=request.project_id,
                        release=release,
                        index_snapshot_id=index.id,
                        detected_text=offered_detected_text,
                        chosen=chosen_option,
                        options=offered_options,
                        exact_context_hash=self._exact_semantic_binding_hash(global_evidence),
                        now=decision_now,
                    )
                    if pending_memory is not None:
                        pending_confirmation_memories.append(pending_memory)
                confirmed_scope_id = candidate_selection_id
                scope_resolution = QueryScopeResolver.from_release(release).resolve(
                    global_evidence.matches,
                    allowed_dataset_ids=dataset_ids,
                    selected_dataset_id=confirmed_scope_id,
                    # A ``time:`` continuation is produced by the parser after
                    # Mapper/Scope selection.  It narrows eligible datasets and
                    # enters the time corrector below, but is not Mapper evidence
                    # and must never be validated as a selected semantic hit.
                    selected_element_id=selected_element_id,
                    selected_element_type=(
                        selected_element_type.value if selected_element_type is not None else None
                    ),
                    human_confirmed_metric_ids=selected_metric_bundle_ids,
                )
                if candidate_selection_id is not None:
                    unselected_scope = QueryScopeResolver.from_release(release).resolve(
                        global_evidence.matches,
                        allowed_dataset_ids=dataset_ids,
                        selected_element_id=selected_element_id,
                        selected_element_type=(
                            selected_element_type.value
                            if selected_element_type is not None
                            else None
                        ),
                        human_confirmed_metric_ids=selected_metric_bundle_ids,
                    )
                    if unselected_scope.status is QueryScopeResolutionStatus.CLARIFICATION:
                        object_options = self._query_scope_options_for_dataset_ids(
                            release,
                            unselected_scope.candidate_dataset_ids,
                            selection_context=selection_context,
                            carried_selection_id=self._semantic_selection_token(
                                selected_element_id=selected_element_id,
                                selected_element_type=selected_element_type,
                                selected_time_dimension_id=selected_time_dimension_id,
                            ),
                        )
                        chosen_object = next(
                            (
                                item
                                for item in object_options
                                if item.dataset_id == candidate_selection_id
                            ),
                            None,
                        )
                        if chosen_object is not None:
                            pending_memory = self._confirmation_memory_from_choice(
                                actor_id=tenant_id,
                                project_id=request.project_id,
                                release=release,
                                index_snapshot_id=index.id,
                                detected_text="业务记录粒度",
                                chosen=chosen_object,
                                options=object_options,
                                exact_context_hash=self._exact_semantic_binding_hash(
                                    global_evidence
                                ),
                                now=decision_now,
                            )
                            if pending_memory is not None:
                                pending_confirmation_memories.append(pending_memory)
                            automatic_decisions.append(
                                self._semantic_decision(
                                    detected_text="业务记录粒度",
                                    source=SemanticDecisionSource.HUMAN,
                                    chosen=chosen_object,
                                    options=object_options,
                                )
                            )
                weak_metric_confirmation = (
                    self._weak_metric_confirmation(
                        release=release,
                        evidence=global_evidence,
                        # Weak metric candidates must be projected across the
                        # caller's whole allowed set.  Reusing a dimension-only
                        # resolver result here would hide a metric owned by a
                        # different root before the user can confirm it.
                        dataset_ids=dataset_ids,
                        selection_context=selection_context,
                    )
                    if (
                        scope_resolution.status is not QueryScopeResolutionStatus.REFUSED
                        and not scope_resolution.exact_metric_ids
                        and selected_element_id is None
                        and candidate_selection_id is None
                    )
                    else None
                )
                if weak_metric_confirmation is not None:
                    (
                        confirmation_kind,
                        confirmation_detected_text,
                        confirmation_question,
                        confirmation_options,
                        projections,
                        confirmation_phrase_groups,
                    ) = weak_metric_confirmation
                    metric_ids = tuple(
                        dict.fromkeys(
                            item.element_id
                            for item in confirmation_options
                            if item.kind == "metric" and item.element_id is not None
                        )
                    )
                    exact_context_hash = self._exact_semantic_binding_hash(global_evidence)
                    remembered = None
                    remembered_option = None
                    if confirmation_kind == "metric_multi":
                        (
                            recalled_bundle,
                            confirmation_memory_unavailable,
                        ) = self._recall_metric_phrase_memories(
                            actor_id=tenant_id,
                            project_id=request.project_id,
                            release=release,
                            index_snapshot_id=index.id,
                            combination_options=confirmation_options,
                            phrase_groups=confirmation_phrase_groups,
                            selection_context=selection_context,
                            exact_context_hash=exact_context_hash,
                            now=decision_now,
                        )
                        if confirmation_memory_unavailable:
                            confirmation_memory_detail = {"status": "unavailable"}
                        if recalled_bundle is not None:
                            remembered_dataset_id, remembered_groups = recalled_bundle
                            remembered_metric_ids = tuple(
                                dict.fromkeys(
                                    chosen.element_id
                                    for _text, chosen, _options, _memory in remembered_groups
                                    if chosen.element_id is not None
                                )
                            )
                            remembered_scope = QueryScopeResolver.from_release(release).resolve(
                                global_evidence.matches,
                                allowed_dataset_ids=dataset_ids,
                                selected_dataset_id=remembered_dataset_id,
                                memory_confirmed_metric_ids=remembered_metric_ids,
                            )
                            if remembered_scope.status is QueryScopeResolutionStatus.SELECTED:
                                memory_confirmed_metric_ids = remembered_metric_ids
                                scope_resolution = remembered_scope
                                semantic_intent_adopted = True
                                for (
                                    detected_text,
                                    chosen,
                                    group_options,
                                    _memory,
                                ) in remembered_groups:
                                    obligation = self._decision_obligation(
                                        release=release,
                                        detected_text=detected_text,
                                        source=SemanticDecisionSource.MEMORY,
                                        chosen=chosen,
                                        options=group_options,
                                    )
                                    if obligation is not None:
                                        decision_obligations.append(obligation)
                                confirmation_memory_detail = {
                                    "status": "applied",
                                    "candidate_set_hash": content_hash(
                                        [
                                            memory.candidate_set_hash
                                            for (
                                                _text,
                                                _chosen,
                                                _options,
                                                memory,
                                            ) in remembered_groups
                                        ]
                                    ),
                                }
                    else:
                        (
                            remembered,
                            remembered_option,
                            confirmation_memory_unavailable,
                        ) = self._recall_confirmation_memory(
                            actor_id=tenant_id,
                            project_id=request.project_id,
                            release=release,
                            index_snapshot_id=index.id,
                            detected_text=confirmation_detected_text,
                            options=confirmation_options,
                            exact_context_hash=exact_context_hash,
                            now=decision_now,
                        )
                        if confirmation_memory_unavailable:
                            confirmation_memory_detail = {"status": "unavailable"}
                    if remembered is not None and remembered_option is not None:
                        remembered_dataset_id, semantic_selection_ids = (
                            self._decode_selection_token(
                                remembered_option.candidate_id,
                                release=release,
                                context=selection_context,
                            )
                        )
                        semantic_selection_id = (
                            semantic_selection_ids[0] if len(semantic_selection_ids) == 1 else None
                        )
                        (
                            remembered_element_id,
                            remembered_element_type,
                            remembered_time_id,
                        ) = self._parse_semantic_selection_id(semantic_selection_id)
                        remembered_scope = QueryScopeResolver.from_release(release).resolve(
                            global_evidence.matches,
                            allowed_dataset_ids=dataset_ids,
                            selected_dataset_id=remembered_dataset_id,
                            selected_element_id=(
                                remembered_element_id
                                if remembered_element_type is not SemanticElementType.METRIC
                                else None
                            ),
                            selected_element_type=(
                                remembered_element_type.value
                                if remembered_element_type is not None
                                and remembered_element_type is not SemanticElementType.METRIC
                                else None
                            ),
                            memory_confirmed_metric_id=(
                                remembered_element_id
                                if remembered_element_type is SemanticElementType.METRIC
                                else None
                            ),
                        )
                        if remembered_scope.status is not QueryScopeResolutionStatus.REFUSED:
                            effective_selected_element_id = remembered_element_id
                            effective_selected_element_type = remembered_element_type
                            selected_time_dimension_id = remembered_time_id
                            if remembered_element_type is SemanticElementType.METRIC:
                                memory_confirmed_metric_id = remembered_element_id
                            scope_resolution = remembered_scope
                            semantic_intent_adopted = True
                            obligation = self._decision_obligation(
                                release=release,
                                detected_text=confirmation_detected_text,
                                source=SemanticDecisionSource.MEMORY,
                                chosen=remembered_option,
                                options=confirmation_options,
                            )
                            if obligation is not None:
                                decision_obligations.append(obligation)
                            confirmation_memory_detail = {
                                "status": "applied",
                                "candidate_set_hash": remembered.candidate_set_hash,
                            }
                        else:
                            confirmation_memory_detail = {
                                "status": "ignored",
                                "governance_code": remembered_scope.code,
                            }
                    if (
                        not confirmation_memory_unavailable
                        and not semantic_intent_adopted
                        and self._should_adjudicate_weak_metric(
                            confirmation_kind=confirmation_kind,
                            metric_ids=metric_ids,
                        )
                    ):
                        adjudication = self._adjudicate_weak_metric(
                            question=request.question,
                            detected_text=confirmation_detected_text,
                            release=release,
                            metric_ids=metric_ids,
                            evidence=global_evidence,
                            query_id=query_id,
                            tenant_id=tenant_id,
                        )
                        weak_metric_adjudication_detail = self._adjudication_trace_detail(
                            adjudication,
                            mode=self._weak_metric_adjudication_mode,
                        )
                        if (
                            self._weak_metric_adjudication_mode is WeakMetricAdjudicationMode.AUTO
                            and adjudication.decision is WeakMetricAdjudicationDecision.MATCH
                            and adjudication.metric_id in metric_ids
                        ):
                            adjudicated_scope = QueryScopeResolver.from_release(release).resolve(
                                global_evidence.matches,
                                allowed_dataset_ids=dataset_ids,
                                ai_adjudicated_metric_id=adjudication.metric_id,
                            )
                            if adjudicated_scope.status is QueryScopeResolutionStatus.SELECTED:
                                ai_adjudicated_metric_id = adjudication.metric_id
                                ai_adjudicated_metric_ids = (adjudication.metric_id,)
                                effective_selected_element_id = adjudication.metric_id
                                effective_selected_element_type = SemanticElementType.METRIC
                                scope_resolution = adjudicated_scope
                                chosen_option = next(
                                    (
                                        item
                                        for item in confirmation_options
                                        if item.element_type == "metric"
                                        and item.element_id == adjudication.metric_id
                                    ),
                                    None,
                                )
                                if chosen_option is not None:
                                    obligation = self._decision_obligation(
                                        release=release,
                                        detected_text=confirmation_detected_text,
                                        source=SemanticDecisionSource.AI,
                                        chosen=chosen_option,
                                        options=confirmation_options,
                                    )
                                    if obligation is not None:
                                        decision_obligations.append(obligation)
                            else:
                                weak_metric_adjudication_detail.update(
                                    {
                                        "governance_code": adjudicated_scope.code,
                                        "fallback": "human_confirmation",
                                    }
                                )
                        if ai_adjudicated_metric_id is None:
                            weak_metric_adjudication_detail.setdefault(
                                "fallback", "human_confirmation"
                            )
                    elif (
                        not semantic_intent_adopted
                        and not confirmation_memory_unavailable
                        and confirmation_kind == "metric_multi"
                        and self._should_adjudicate_intent(
                            confirmation_kind=confirmation_kind,
                            options=confirmation_options,
                            mode=self._semantic_intent_adjudication_mode,
                        )
                    ):
                        selected_groups, semantic_intent_adjudication_detail = (
                            self._adjudicate_metric_option_groups(
                                question=request.question,
                                release=release,
                                phrase_groups=confirmation_phrase_groups,
                                exact_context=self._exact_semantic_context(
                                    release,
                                    global_evidence,
                                ),
                                query_id=query_id,
                                tenant_id=tenant_id,
                            )
                        )
                        if (
                            self._semantic_intent_adjudication_mode
                            is WeakMetricAdjudicationMode.AUTO
                            and selected_groups
                        ):
                            selected_metric_ids = tuple(
                                dict.fromkeys(
                                    option.element_id
                                    for _detected, option, _options in selected_groups
                                    if option.element_id is not None
                                )
                            )
                            adjudicated_scope = QueryScopeResolver.from_release(release).resolve(
                                global_evidence.matches,
                                allowed_dataset_ids=dataset_ids,
                                ai_adjudicated_metric_ids=selected_metric_ids,
                            )
                            if adjudicated_scope.status is QueryScopeResolutionStatus.REFUSED:
                                if adjudicated_scope.code == "CROSS_FACT_METRICS_UNSUPPORTED":
                                    raise MappingError(
                                        adjudicated_scope.message,
                                        code=adjudicated_scope.code,
                                        details={
                                            "scope_resolution": (
                                                adjudicated_scope.to_trace_detail()
                                            )
                                        },
                                    )
                                semantic_intent_adjudication_detail.update(
                                    {
                                        "governance_code": adjudicated_scope.code,
                                        "fallback": "human_confirmation",
                                    }
                                )
                            else:
                                ai_adjudicated_metric_ids = selected_metric_ids
                                scope_resolution = adjudicated_scope
                                semantic_intent_adopted = True
                                semantic_intent_adjudication_detail["adopted"] = True
                                for detected_text, chosen, group_options in selected_groups:
                                    obligation = self._decision_obligation(
                                        release=release,
                                        detected_text=detected_text,
                                        source=SemanticDecisionSource.AI,
                                        chosen=chosen,
                                        options=group_options,
                                    )
                                    if obligation is not None:
                                        decision_obligations.append(obligation)
                    elif (
                        not confirmation_memory_unavailable
                        and not semantic_intent_adopted
                        and self._should_adjudicate_intent(
                            confirmation_kind=confirmation_kind,
                            options=confirmation_options,
                            mode=self._semantic_intent_adjudication_mode,
                        )
                    ):
                        intent_candidates = self._intent_candidates_for_options(
                            release=release,
                            options=confirmation_options,
                            intent_kind="semantic_element",
                        )
                        intent_result = self._adjudicate_intent(
                            intent_kind="semantic_element",
                            question=request.question,
                            detected_text=confirmation_detected_text,
                            candidates=intent_candidates,
                            exact_context=self._exact_semantic_context(
                                release,
                                global_evidence,
                            ),
                            query_id=query_id,
                            tenant_id=tenant_id,
                            release=release,
                        )
                        semantic_intent_adjudication_detail = self._intent_trace_detail(
                            intent_result,
                            mode=self._semantic_intent_adjudication_mode,
                        )
                        if (
                            self._semantic_intent_adjudication_mode
                            is WeakMetricAdjudicationMode.AUTO
                            and intent_result.decision is IntentAdjudicationDecision.MATCH
                        ):
                            chosen_option = next(
                                (
                                    item
                                    for item in confirmation_options
                                    if item.candidate_id == intent_result.selection_id
                                ),
                                None,
                            )
                            if chosen_option is not None:
                                ai_dataset_id, semantic_selection_ids = (
                                    self._decode_selection_token(
                                        chosen_option.candidate_id,
                                        release=release,
                                        context=selection_context,
                                    )
                                )
                                semantic_selection_id = (
                                    semantic_selection_ids[0]
                                    if len(semantic_selection_ids) == 1
                                    else None
                                )
                                (
                                    ai_element_id,
                                    ai_element_type,
                                    ai_time_dimension_id,
                                ) = self._parse_semantic_selection_id(semantic_selection_id)
                                adjudicated_scope = QueryScopeResolver.from_release(
                                    release
                                ).resolve(
                                    global_evidence.matches,
                                    allowed_dataset_ids=dataset_ids,
                                    selected_dataset_id=ai_dataset_id,
                                    selected_element_id=(
                                        ai_element_id
                                        if ai_element_type is not SemanticElementType.METRIC
                                        else None
                                    ),
                                    selected_element_type=(
                                        ai_element_type.value
                                        if ai_element_type is not None
                                        and ai_element_type is not SemanticElementType.METRIC
                                        else None
                                    ),
                                    ai_adjudicated_metric_id=(
                                        ai_element_id
                                        if ai_element_type is SemanticElementType.METRIC
                                        else None
                                    ),
                                )
                                if (
                                    adjudicated_scope.status
                                    is not QueryScopeResolutionStatus.REFUSED
                                ):
                                    effective_selected_element_id = ai_element_id
                                    effective_selected_element_type = ai_element_type
                                    selected_time_dimension_id = ai_time_dimension_id
                                    if ai_element_type is SemanticElementType.METRIC:
                                        ai_adjudicated_metric_id = ai_element_id
                                        ai_adjudicated_metric_ids = (ai_element_id,)
                                    scope_resolution = adjudicated_scope
                                    semantic_intent_adopted = True
                                    semantic_intent_adjudication_detail["adopted"] = True
                                    obligation = self._decision_obligation(
                                        release=release,
                                        detected_text=confirmation_detected_text,
                                        source=SemanticDecisionSource.AI,
                                        chosen=chosen_option,
                                        options=confirmation_options,
                                    )
                                    if obligation is not None:
                                        decision_obligations.append(obligation)
                                else:
                                    semantic_intent_adjudication_detail.update(
                                        {
                                            "governance_code": adjudicated_scope.code,
                                            "fallback": "human_confirmation",
                                            "adopted": False,
                                        }
                                    )
                        if not semantic_intent_adopted:
                            semantic_intent_adjudication_detail.setdefault("adopted", False)
                            semantic_intent_adjudication_detail.setdefault(
                                "fallback", "human_confirmation"
                            )
                    if ai_adjudicated_metric_id is None and not semantic_intent_adopted:
                        trace[-1] = self._weak_metric_clarification_trace(
                            scope_resolution=scope_resolution,
                            confirmation_kind=confirmation_kind,
                            detected_text=confirmation_detected_text,
                            options=confirmation_options,
                            projections=projections,
                            adjudication_detail=(
                                semantic_intent_adjudication_detail
                                or weak_metric_adjudication_detail
                            ),
                            adjudication_key=(
                                "semantic_intent_adjudication"
                                if semantic_intent_adjudication_detail is not None
                                else "weak_metric_adjudication"
                            ),
                            include_diagnostics=request.include_diagnostics,
                        )
                        return self._weak_metric_clarification_response(
                            query_id=query_id,
                            release=release,
                            index_snapshot_id=index.id,
                            trace=trace,
                            confirmation_kind=confirmation_kind,
                            question=confirmation_question,
                            options=confirmation_options,
                        )
                if scope_resolution.status is QueryScopeResolutionStatus.CLARIFICATION:
                    ambiguous_metric_ids = tuple(
                        dict.fromkeys(
                            metric_id
                            for group in scope_resolution.ambiguous_metric_groups
                            for metric_id in group.metric_ids
                        )
                    )
                    bundled_scope_semantics = (
                        self._semantic_options_for_ambiguous_scopes(
                            release=release,
                            evidence=global_evidence,
                            dataset_ids=scope_resolution.candidate_dataset_ids,
                            selection_context=selection_context,
                            question=request.question,
                            now=now,
                        )
                        if not ambiguous_metric_ids
                        else None
                    )
                    options = (
                        self._semantic_options(
                            release,
                            ambiguous_metric_ids,
                            selection_context=selection_context,
                            require_time=False,
                            typed_members=tuple(
                                SemanticAmbiguityMember(
                                    element_type=SemanticElementType.METRIC,
                                    element_id=metric_id,
                                )
                                for metric_id in ambiguous_metric_ids
                            ),
                            allowed_dataset_ids=dataset_ids,
                        )
                        if ambiguous_metric_ids
                        else bundled_scope_semantics[1]
                        if bundled_scope_semantics is not None
                        else self._query_scope_options_for_dataset_ids(
                            release,
                            scope_resolution.candidate_dataset_ids,
                            selection_context=selection_context,
                            carried_selection_id=self._semantic_selection_token(
                                selected_element_id=effective_selected_element_id,
                                selected_element_type=effective_selected_element_type,
                                selected_time_dimension_id=selected_time_dimension_id,
                            ),
                        )
                    )
                    if (
                        scope_resolution.status is QueryScopeResolutionStatus.CLARIFICATION
                        and bundled_scope_semantics is not None
                    ):
                        detected_text, options = bundled_scope_semantics
                        trace[-1] = QueryTraceStep(
                            stage=QueryStage.CANDIDATE_DISCOVERY,
                            status="clarification",
                            detail=(
                                {
                                    "scope_resolution": scope_resolution.to_trace_detail(),
                                    "semantic_confirmation": {
                                        "kind": "semantic_element",
                                        "detected_text": detected_text,
                                        "bundles_business_object": True,
                                    },
                                }
                                if request.include_diagnostics
                                else {"clarification_kind": "semantic_element"}
                            ),
                        )
                        return ClarificationQueryResponse(
                            query_id=query_id,
                            release_id=release.id,
                            spec_hash=release.spec_hash,
                            index_snapshot_id=index.id,
                            trace=tuple(trace),
                            diagnostics=QueryDiagnosis(
                                category=QueryDiagnosticCategory.AMBIGUITY,
                                stage=QueryStage.CANDIDATE_DISCOVERY.value,
                                severity="warning",
                                summary="同一说法对应多个业务语义和分析粒度。",
                                recommendation="确认具体语义对象后重新执行。",
                                user_hint=(
                                    "请选择你实际表达的业务含义。"
                                    if options
                                    else (
                                        "当前版本无法用一次业务选择区分这些语义，请联系建模管理员。"
                                    )
                                ),
                            ),
                            question=(
                                f"你说的「{detected_text}」有多个业务含义，请确认具体对象。"
                                if options
                                else "当前发布版本存在多个独立语义歧义，请联系建模管理员重新发布。"
                            ),
                            options=options,
                        )
                    if not ambiguous_metric_ids and options and candidate_selection_id is None:
                        (
                            remembered,
                            remembered_option,
                            confirmation_memory_unavailable,
                        ) = self._recall_confirmation_memory(
                            actor_id=tenant_id,
                            project_id=request.project_id,
                            release=release,
                            index_snapshot_id=index.id,
                            detected_text="业务记录粒度",
                            options=options,
                            exact_context_hash=self._exact_semantic_binding_hash(global_evidence),
                            now=decision_now,
                        )
                        if confirmation_memory_unavailable:
                            confirmation_memory_detail = {"status": "unavailable"}
                        if remembered is not None and remembered_option is not None:
                            remembered_scope = QueryScopeResolver.from_release(release).resolve(
                                global_evidence.matches,
                                allowed_dataset_ids=dataset_ids,
                                selected_dataset_id=remembered_option.dataset_id,
                                selected_element_id=(
                                    effective_selected_element_id
                                    if effective_selected_element_type
                                    is not SemanticElementType.METRIC
                                    else None
                                ),
                                selected_element_type=(
                                    effective_selected_element_type.value
                                    if effective_selected_element_type is not None
                                    and effective_selected_element_type
                                    is not SemanticElementType.METRIC
                                    else None
                                ),
                                ai_adjudicated_metric_id=ai_adjudicated_metric_id,
                                memory_confirmed_metric_id=memory_confirmed_metric_id,
                                memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                            )
                            if remembered_scope.status is QueryScopeResolutionStatus.SELECTED:
                                scope_resolution = remembered_scope
                                automatic_decisions.append(
                                    self._semantic_decision(
                                        detected_text="业务记录粒度",
                                        source=SemanticDecisionSource.MEMORY,
                                        chosen=remembered_option,
                                        options=options,
                                    )
                                )
                                confirmation_memory_detail = {
                                    "status": "applied",
                                    "candidate_set_hash": remembered.candidate_set_hash,
                                }
                    if (
                        scope_resolution.status is QueryScopeResolutionStatus.CLARIFICATION
                        and not confirmation_memory_unavailable
                        and not ambiguous_metric_ids
                        and options
                        and candidate_selection_id is None
                        and self._should_adjudicate_intent(
                            confirmation_kind="analysis_object",
                            options=options,
                            mode=self._analysis_object_adjudication_mode,
                        )
                    ):
                        intent_result = self._adjudicate_intent(
                            intent_kind="analysis_object",
                            question=request.question,
                            detected_text="业务记录粒度",
                            candidates=self._intent_candidates_for_options(
                                release=release,
                                options=options,
                                intent_kind="analysis_object",
                            ),
                            exact_context=self._exact_semantic_context(
                                release,
                                global_evidence,
                            ),
                            query_id=query_id,
                            tenant_id=tenant_id,
                            release=release,
                        )
                        analysis_object_adjudication_detail = self._intent_trace_detail(
                            intent_result,
                            mode=self._analysis_object_adjudication_mode,
                        )
                        if (
                            self._analysis_object_adjudication_mode
                            is WeakMetricAdjudicationMode.AUTO
                            and intent_result.decision is IntentAdjudicationDecision.MATCH
                        ):
                            chosen_option = next(
                                (
                                    item
                                    for item in options
                                    if item.candidate_id == intent_result.selection_id
                                ),
                                None,
                            )
                            if chosen_option is not None:
                                adjudicated_scope = QueryScopeResolver.from_release(
                                    release
                                ).resolve(
                                    global_evidence.matches,
                                    allowed_dataset_ids=dataset_ids,
                                    selected_dataset_id=chosen_option.dataset_id,
                                    selected_element_id=(
                                        effective_selected_element_id
                                        if effective_selected_element_type
                                        is not SemanticElementType.METRIC
                                        else None
                                    ),
                                    selected_element_type=(
                                        effective_selected_element_type.value
                                        if effective_selected_element_type is not None
                                        and effective_selected_element_type
                                        is not SemanticElementType.METRIC
                                        else None
                                    ),
                                    ai_adjudicated_metric_id=ai_adjudicated_metric_id,
                                    ai_adjudicated_metric_ids=ai_adjudicated_metric_ids,
                                    memory_confirmed_metric_id=memory_confirmed_metric_id,
                                    memory_confirmed_metric_ids=memory_confirmed_metric_ids,
                                )
                                if adjudicated_scope.status is QueryScopeResolutionStatus.SELECTED:
                                    scope_resolution = adjudicated_scope
                                    analysis_object_adjudication_detail["adopted"] = True
                                    automatic_decisions.append(
                                        self._semantic_decision(
                                            detected_text="业务记录粒度",
                                            source=SemanticDecisionSource.AI,
                                            chosen=chosen_option,
                                            options=options,
                                        )
                                    )
                                else:
                                    analysis_object_adjudication_detail.update(
                                        {
                                            "governance_code": adjudicated_scope.code,
                                            "fallback": "human_confirmation",
                                            "adopted": False,
                                        }
                                    )
                        if scope_resolution.status is QueryScopeResolutionStatus.CLARIFICATION:
                            analysis_object_adjudication_detail.setdefault("adopted", False)
                            analysis_object_adjudication_detail.setdefault(
                                "fallback", "human_confirmation"
                            )
                    if scope_resolution.status is QueryScopeResolutionStatus.CLARIFICATION:
                        trace[-1] = QueryTraceStep(
                            stage=QueryStage.CANDIDATE_DISCOVERY,
                            status="clarification",
                            detail=(
                                {
                                    "scope_resolution": scope_resolution.to_trace_detail(),
                                    **(
                                        {
                                            "analysis_object_adjudication": (
                                                analysis_object_adjudication_detail
                                            )
                                        }
                                        if analysis_object_adjudication_detail is not None
                                        else {}
                                    ),
                                }
                                if request.include_diagnostics
                                else {"clarification_kind": "semantic_or_business_object"}
                            ),
                        )
                        clarification_question = (
                            "同一说法对应多个业务指标，请确认具体口径。"
                            if ambiguous_metric_ids
                            else "当前问题仍可落到多个业务分析对象，请确认你要分析什么。"
                            if options
                            else (
                                "当前发布版本存在无法区分的内部分析路径，请联系建模管理员重新发布。"
                            )
                        )
                        return ClarificationQueryResponse(
                            query_id=query_id,
                            release_id=release.id,
                            spec_hash=release.spec_hash,
                            index_snapshot_id=index.id,
                            trace=tuple(trace),
                            diagnostics=QueryDiagnosis(
                                category=QueryDiagnosticCategory.AMBIGUITY,
                                stage=QueryStage.CANDIDATE_DISCOVERY.value,
                                severity="warning",
                                summary=(
                                    "同一说法对应多个业务指标。"
                                    if ambiguous_metric_ids
                                    else "多个业务分析对象满足当前语义条件。"
                                ),
                                recommendation="确认具体语义对象或事实范围后重新执行。",
                                user_hint="请选择你要分析的业务对象。",
                            ),
                            question=clarification_question,
                            options=options,
                        )
                if scope_resolution.status is QueryScopeResolutionStatus.REFUSED:
                    raise MappingError(
                        scope_resolution.message,
                        code=scope_resolution.code,
                        details={"scope_resolution": scope_resolution.to_trace_detail()},
                    )
                assert scope_resolution.selected_dataset_id is not None
                candidate_set = self._orchestrator.discover_selected_scope(
                    question=request.question,
                    release=release,
                    evidence=global_evidence,
                    dataset_id=scope_resolution.selected_dataset_id,
                    now=now,
                    selected_element_id=effective_selected_element_id,
                    selected_element_type=effective_selected_element_type,
                    selected_time_dimension_id=selected_time_dimension_id,
                )
            else:
                candidate_set = self._orchestrator.discover(
                    question=request.question,
                    release=release,
                    index=index,
                    dataset_ids=dataset_ids,
                    now=now,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    selected_time_dimension_id=selected_time_dimension_id,
                    tenant_id=tenant_id,
                )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.CANDIDATE_DISCOVERY,
                status="completed",
                detail=(
                    {
                        "candidate_ids": [item.id for item in candidate_set.candidates],
                        "mapping_modes": [
                            f"{item.dataset_id}:{item.mode.value}"
                            for item in candidate_set.mapping_attempts
                        ],
                        "mapping_degraded_reasons": [
                            {
                                "dataset_id": attempt.dataset_id,
                                "mode": attempt.mode.value,
                                "reasons": list(attempt.degraded_reasons),
                            }
                            for attempt in candidate_set.mapping_attempts
                            if attempt.degraded_reasons
                        ],
                        **(
                            {"scope_resolution": scope_resolution.to_trace_detail()}
                            if scope_resolution is not None
                            else {}
                        ),
                        **(
                            {"weak_metric_adjudication": weak_metric_adjudication_detail}
                            if weak_metric_adjudication_detail is not None
                            else {}
                        ),
                        **(
                            {"semantic_intent_adjudication": (semantic_intent_adjudication_detail)}
                            if semantic_intent_adjudication_detail is not None
                            else {}
                        ),
                        **(
                            {"analysis_object_adjudication": (analysis_object_adjudication_detail)}
                            if analysis_object_adjudication_detail is not None
                            else {}
                        ),
                        **(
                            {"confirmation_memory": confirmation_memory_detail}
                            if confirmation_memory_detail is not None
                            else {}
                        ),
                        **{
                            "mapping_attempts": [
                                item.model_dump(mode="json")
                                for item in candidate_set.mapping_attempts
                            ],
                            "candidates": [
                                {
                                    "id": item.id,
                                    "dataset_id": item.dataset_id,
                                    "parser": item.parser,
                                    "score": item.score,
                                    "parsed_s2sql": item.parsed_s2sql,
                                    "mapping": item.mapping.model_dump(mode="json"),
                                }
                                for item in candidate_set.candidates
                            ],
                        },
                    }
                    if request.include_diagnostics
                    else {}
                ),
            )
            admitted_candidates = (
                candidate_set.candidates
                if use_global_scope_router
                else self._admit_query_scope_candidates(candidate_set.candidates)
            )
            if admitted_candidates != candidate_set.candidates and request.include_diagnostics:
                trace[-1] = trace[-1].model_copy(
                    update={
                        "detail": {
                            **trace[-1].detail,
                            "scope_admission": {
                                "rule": "unique_exact_metric_match",
                                "discovered_dataset_ids": [
                                    item.dataset_id for item in candidate_set.candidates
                                ],
                                "admitted_dataset_ids": [
                                    item.dataset_id for item in admitted_candidates
                                ],
                            },
                        }
                    }
                )
            offered_candidates = self._clarification_scope_candidates(admitted_candidates)
            scope_options = self._query_scope_options(
                release,
                offered_candidates,
                selection_context=selection_context,
            )
            offered_scope_ids = tuple(dict.fromkeys(item.dataset_id for item in offered_candidates))
            if (
                not use_global_scope_router
                and candidate_selection_id is None
                and len(offered_scope_ids) > 1
            ):
                clarification_detail: dict[str, object] = {
                    "code": "AMBIGUOUS_QUERY_SCOPE",
                    **(
                        {
                            "candidate_ids": [item.candidate_id for item in scope_options],
                            "dataset_ids": list(offered_scope_ids),
                        }
                        if request.include_diagnostics
                        else {}
                    ),
                }
                if offered_candidates != admitted_candidates and request.include_diagnostics:
                    clarification_detail["options_rule"] = "exact_metric_scopes_only"
                    clarification_detail["excluded_dataset_ids"] = sorted(
                        {item.dataset_id for item in admitted_candidates}
                        - {item.dataset_id for item in offered_candidates}
                    )
                trace.append(
                    QueryTraceStep(
                        stage=QueryStage.CANDIDATE_DISCOVERY,
                        status="clarification",
                        detail=clarification_detail,
                    )
                )
                return ClarificationQueryResponse(
                    query_id=query_id,
                    release_id=release.id,
                    spec_hash=release.spec_hash,
                    index_snapshot_id=index.id,
                    trace=tuple(trace),
                    diagnostics=QueryDiagnosis(
                        category=QueryDiagnosticCategory.AMBIGUITY,
                        stage=QueryStage.CANDIDATE_DISCOVERY.value,
                        severity="warning",
                        summary="同一问题匹配到多个业务分析对象",
                        recommendation="重新发布带有业务对象信息的语义模型后再确认。",
                        user_hint="当前版本无法安全地区分分析对象，请联系建模管理员。",
                    ),
                    question=(
                        "这个问题可以按多个业务对象分析，请确认你想分析哪一个。"
                        if scope_options
                        else "当前发布版本缺少可区分的业务对象信息，请联系建模管理员重新发布。"
                    ),
                    options=scope_options,
                )
            selected = (
                admitted_candidates[0]
                if use_global_scope_router
                else self._select_candidate(
                    admitted_candidates,
                    candidate_selection_id,
                )
            )
            selected_scope_dataset_id = selected.dataset_id
            unresolved = same_name_ambiguity(selected.mapping, release)
            if unresolved is not None:
                semantic_clarification_group = unresolved
                raise ClarificationSignal(
                    code="AMBIGUOUS_SEMANTIC_ELEMENT",
                    message=(
                        f"「{unresolved.detected_text}」在当前业务范围内匹配到多个同名指标或维度，"
                        "请确认你指的是哪一个。"
                    ),
                    element_ids=tuple(member.element_id for member in unresolved.members),
                    degraded_reasons=selected.mapping.degraded_reasons,
                )
            if request.conversation_id is not None:
                normalized_actor = tenant_id
                if not normalized_actor:
                    raise SemanticParsingError(
                        "多轮问数必须绑定当前用户",
                        code="MULTI_TURN_ACTOR_REQUIRED",
                    )
                if self._query_history is not None and self._multi_turn_rewriter is not None:
                    previous = self._query_history.last_success(
                        actor_id=normalized_actor,
                        project_id=request.project_id,
                        conversation_id=request.conversation_id,
                        release_id=release.id,
                        spec_hash=release.spec_hash,
                        index_snapshot_id=index.id,
                        dataset_id=selected.dataset_id,
                    )
                    if previous is not None:
                        effective_question = self._multi_turn_rewriter.rewrite(
                            MultiTurnContext(
                                tenant_id=tenant_id,
                                current_question=request.question,
                                current_mapping=selected.mapping,
                                previous_question=previous.effective_question,
                                previous_mapping=previous.mapping,
                                previous_corrected_s2sql=previous.corrected_s2sql,
                            )
                        )
                        if use_global_scope_router and effective_question != request.question:
                            # The rewrite is a new effective question produced
                            # after the root Scope is known. Retrieve once for
                            # that selected Scope rather than reusing evidence
                            # collected for the original wording.
                            global_evidence = self._orchestrator.collect_evidence(
                                question=effective_question,
                                dataset_ids=(selected.dataset_id,),
                                index=index,
                                tenant_id=tenant_id,
                            )
            trace.append(QueryTraceStep(stage=QueryStage.FINAL_PARSING, status="started"))
            translation_holder = {}

            def validate_and_translate(candidate: ParsedSemanticCandidate) -> None:
                if request.include_diagnostics:
                    diagnostic_context["last_candidate"] = candidate.model_dump(mode="json")
                translation_holder["value"] = self._s2sql_translator.translate(
                    release=release,
                    dataset_id=candidate.dataset_id,
                    corrected_s2sql=candidate.corrected_s2sql,
                )

            corrected = self._orchestrator.final_parse(
                question=effective_question,
                query_id=query_id,
                release=release,
                index=index,
                selected=selected,
                now=now,
                selected_element_id=effective_selected_element_id,
                selected_element_type=effective_selected_element_type,
                selected_time_dimension_id=selected_time_dimension_id,
                candidate_validator=validate_and_translate,
                diagnostic_sink=(
                    lambda event, detail: parse_events.append({"event": event, "detail": detail})
                )
                if request.include_diagnostics
                else None,
                tenant_id=tenant_id,
                mapping_evidence=global_evidence,
            )
            rule_application = self._query_rule_engine.apply(
                release=release,
                dataset_id=corrected.dataset_id,
                corrected_s2sql=corrected.corrected_s2sql,
                now=now,
            )
            if rule_application.applied_rule_ids:
                corrected = corrected.model_copy(
                    update={
                        "corrected_s2sql": rule_application.corrected_s2sql,
                        "applied_defaults": tuple(
                            dict.fromkeys(
                                (
                                    *corrected.applied_defaults,
                                    *(
                                        f"query_rule:{rule_id}"
                                        for rule_id in rule_application.applied_rule_ids
                                    ),
                                )
                            )
                        ),
                    }
                )
                translation_holder["value"] = self._s2sql_translator.translate(
                    release=release,
                    dataset_id=corrected.dataset_id,
                    corrected_s2sql=corrected.corrected_s2sql,
                )
            # 异名歧义（生还人数 / 遇难人数）在发现阶段放行给了 LLM；这里按最终
            # 回绑出的 ID 复核：恰好用了一个才算裁决成功，0 个或 2 个都问人。放在
            # QueryRule 之后，明示的选择才和返回的 semantic_query 一致。
            settlement = settle_after_parse(
                corrected.mapping,
                translation_holder["value"].audit_query,
                lambda group: self._semantic_options(
                    release,
                    tuple(member.element_id for member in group.members),
                    selection_context=selection_context,
                    require_time=False,
                    preferred_dataset_id=corrected.dataset_id,
                    typed_members=group.members,
                    allowed_dataset_ids=(corrected.dataset_id,),
                ),
                obligations=tuple(decision_obligations),
            )
            if settlement.unmet_obligation is not None:
                obligation = settlement.unmet_obligation
                if len(obligation.candidates) >= 2:
                    semantic_clarification_group = SemanticAmbiguityGroup(
                        detected_text=obligation.detected_text,
                        members=obligation.candidates,
                    )
                raise ClarificationSignal(
                    code="SELECTED_SEMANTIC_NOT_USED",
                    message=(
                        f"生成的查询没有保留已确认的「{obligation.detected_text}」业务含义，"
                        "请确认后重试。"
                    ),
                    element_ids=tuple(member.element_id for member in obligation.candidates),
                    stage=QueryStage.FINAL_PARSING.value,
                )
            if settlement.unresolved is not None:
                semantic_clarification_group = settlement.unresolved
                raise ClarificationSignal(
                    code="AMBIGUOUS_SEMANTIC_ELEMENT",
                    message=(
                        f"「{settlement.unresolved.detected_text}」同时匹配到多个指标或维度，"
                        "请确认你指的是哪一个。"
                    ),
                    element_ids=tuple(
                        member.element_id for member in settlement.unresolved.members
                    ),
                    stage=QueryStage.FINAL_PARSING.value,
                )
            resolved_by_llm = settlement.resolved
            semantic_decisions = tuple(
                (
                    *automatic_decisions,
                    *settlement.decisions,
                    *(
                        SemanticDecision(
                            source=SemanticDecisionSource.FINAL_LLM,
                            detected_text=item.detected_text,
                            chosen=item.chosen,
                            alternatives=item.alternatives,
                        )
                        for item in settlement.resolved
                    ),
                )
            )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.FINAL_PARSING,
                status="completed",
                detail={
                    "parser": corrected.parser,
                    "candidate_id": corrected.id,
                    "multi_turn_rewritten": effective_question != request.question,
                    **(
                        {
                            "original_question": request.question,
                            "effective_question": effective_question,
                            "parsed_s2sql": corrected.parsed_s2sql,
                            "corrected_s2sql": corrected.corrected_s2sql,
                            "rationale": corrected.rationale,
                            "final_mapping": corrected.mapping.model_dump(mode="json"),
                            "parse_events": parse_events,
                        }
                        if request.include_diagnostics
                        else {}
                    ),
                },
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.S2SQL_CORRECTING,
                    status="completed",
                    detail={
                        "registry": list(self._orchestrator.corrector_registry),
                        "enabled": list(self._orchestrator.enabled_correctors),
                        "query_rule_ids": list(rule_application.applied_rule_ids),
                    },
                )
            )
            translated = translation_holder["value"]
            physical = translated.physical_query
            # Structured queries are built from governed IDs, so their audit
            # projection is the request itself and is complete by construction.
            audit_complete = getattr(translated, "audit_complete", True)
            # Diagnostic projection of the TRANSLATING boundary.  The
            # translator remains the sole authority that chooses the relation
            # path; this trace step only exposes the already-selected route.
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.ROUTE_BINDING,
                    status="completed",
                    detail={"relation_ids": list(physical.relation_ids)},
                )
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.TRANSLATING,
                    status="completed",
                    detail={
                        "relation_ids": list(physical.relation_ids),
                        "registry": list(translated.parser_trace),
                        **(
                            {
                                "semantic_query": translated.audit_query.model_dump(mode="json"),
                                "parameters": physical.parameters,
                                "result_limit": physical.result_limit,
                                **(
                                    {"physical_sql": physical.sql}
                                    if request.include_debug_sql
                                    else {}
                                ),
                            }
                            if request.include_diagnostics
                            else {}
                        ),
                    },
                )
            )
            trace.append(QueryTraceStep(stage=QueryStage.PHYSICAL_SQL_CORRECTING, status="started"))
            original_physical_sql = physical.sql
            physical = self._physical_sql_corrector.correct(
                question=effective_question,
                query=physical,
                release=release,
                query_id=query_id,
                tenant_id=tenant_id,
            )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.PHYSICAL_SQL_CORRECTING,
                status="completed",
                detail={
                    "registry": list(self._physical_sql_corrector.registry),
                    "enabled": list(self._physical_sql_corrector.enabled_correctors),
                    "sql_changed": physical.sql != original_physical_sql,
                    **(
                        {
                            "parameters": physical.parameters,
                            **(
                                {
                                    "original_physical_sql": original_physical_sql,
                                    "corrected_physical_sql": physical.sql,
                                }
                                if request.include_debug_sql
                                else {}
                            ),
                        }
                        if request.include_diagnostics
                        else {}
                    ),
                },
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.PHYSICAL_SQL_VALIDATING,
                    status="completed",
                    detail={"guard": "executor_preflight"},
                )
            )
            self._dry_run(physical=physical, release=release, trace=trace)
            trace.append(QueryTraceStep(stage=QueryStage.EXECUTING, status="started"))
            result = self._executor.execute(query=physical, release=release)
            trace[-1] = QueryTraceStep(
                stage=QueryStage.EXECUTING,
                status="completed",
                detail={"row_count": result.row_count, "truncated": result.truncated},
            )
            trace.append(QueryTraceStep(stage=QueryStage.POST_PROCESSING, status="completed"))
            trace.append(QueryTraceStep(stage=QueryStage.FINISHED, status="completed"))
            defaults = tuple(
                dict.fromkeys((*corrected.applied_defaults, *physical.applied_defaults))
            )
            if pending_confirmation_memories and self._confirmation_memories is not None:
                try:
                    for pending_memory in pending_confirmation_memories:
                        self._confirmation_memories.save_confirmation_memory(pending_memory)
                except Exception:
                    LOGGER.exception(
                        "Failed to save confirmation memory project_id=%s query_id=%s",
                        request.project_id,
                        query_id,
                    )
            if request.conversation_id is not None and self._query_history is not None:
                self._query_history.save_success(
                    QueryHistoryTurn(
                        question=request.question,
                        effective_question=effective_question,
                        corrected_s2sql=corrected.corrected_s2sql,
                        mapping=corrected.mapping,
                        dataset_id=corrected.dataset_id,
                        release_id=release.id,
                        spec_hash=release.spec_hash,
                        index_snapshot_id=index.id,
                    ),
                    actor_id=str(actor_id or "").strip(),
                    project_id=request.project_id,
                    conversation_id=request.conversation_id,
                )
            if selected_element_id is not None:
                self._record_element_selection(
                    request,
                    published=published,
                    effective_question=effective_question,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    actor_id=actor_id,
                )
            return CompletedQueryResponse(
                query_id=query_id,
                release_id=release.id,
                spec_hash=release.spec_hash,
                index_snapshot_id=index.id,
                trace=tuple(trace),
                # Same rule as the failure path: a degraded answer is reported
                # once and is not replayable, so a warning always ships. Rule
                # fallback and a lossy interpretation both hand the caller a
                # plausible number that may be wrong; hiding that behind
                # include_diagnostics is the silent-degradation case. Only the
                # info-level "success" diagnosis stays optional.
                diagnostics=_shipped_diagnosis(
                    _success_diagnosis(
                        parser=corrected.parser,
                        llm_enabled=self._orchestrator.llm_enabled,
                        audit_complete=audit_complete,
                    ),
                    include_diagnostics=request.include_diagnostics,
                ),
                interpretation=self._interpretation(
                    release,
                    translated.audit_query,
                    defaults,
                ),
                data=result,
                visualization=self._visualization(
                    release, translated.audit_query, corrected.corrected_s2sql
                ),
                semantic_query=translated.audit_query,
                resolved_by_llm=resolved_by_llm,
                semantic_decisions=semantic_decisions,
                drilldown=self._drilldown_options(
                    release=release,
                    query=translated.audit_query,
                    project_id=request.project_id,
                    query_id=query_id,
                    actor_id=actor_id,
                ),
                parsed_s2sql=corrected.parsed_s2sql,
                corrected_s2sql=corrected.corrected_s2sql,
                physical_sql=physical.sql if request.include_debug_sql else None,
            )
        except ClarificationSignal as signal:
            if published is None:
                return self._failed_without_release(query_id, trace, signal.code, signal.message)
            clarification_stage = _stage_or_precheck(signal.stage)
            trace.append(
                QueryTraceStep(
                    stage=clarification_stage,
                    status="clarification",
                    detail={
                        "code": signal.code,
                        "element_ids": list(signal.element_ids),
                        "degraded_reasons": list(signal.degraded_reasons),
                    },
                )
            )
            options = self._semantic_options(
                published.release,
                signal.element_ids,
                selection_context=selection_context,
                require_time=signal.code == "AMBIGUOUS_TIME_DIMENSION",
                preferred_dataset_id=selected_scope_dataset_id,
                typed_members=(
                    semantic_clarification_group.members
                    if semantic_clarification_group is not None
                    else None
                ),
                allowed_dataset_ids=(
                    (selected_scope_dataset_id,)
                    if selected_scope_dataset_id is not None
                    else option_dataset_ids
                ),
            )
            return ClarificationQueryResponse(
                query_id=query_id,
                release_id=published.release.id,
                spec_hash=published.release.spec_hash,
                index_snapshot_id=published.index_snapshot.id,
                trace=tuple(trace),
                # A clarification is itself a warning about the question; the
                # caller needs it to know why options appeared at all.
                diagnostics=QueryDiagnosis(
                    category=QueryDiagnosticCategory.AMBIGUITY,
                    stage=clarification_stage.value,
                    severity="warning",
                    summary="同一表达匹配到多个受治理语义对象",
                    recommendation="确认具体指标、维度或维度值后重新执行。",
                ),
                question=signal.message,
                options=options,
            )
        except AnalyticsError as exc:
            failure_detail = {"code": exc.code}
            if request.include_diagnostics:
                failure_detail.update(exc.details)
                failure_detail.update(diagnostic_context)
                if parse_events:
                    failure_detail["parse_events"] = parse_events
            _mark_failed(trace, _stage_or_precheck(exc.stage), failure_detail)
            if published is None:
                return self._failed_without_release(
                    query_id,
                    trace,
                    exc.code,
                    _failure_message(exc),
                    exc.stage,
                    diagnostics=_error_diagnosis(exc),
                )
            self._record_failure(
                request,
                exc,
                published=published,
                effective_question=effective_question,
                actor_id=actor_id,
            )
            return FailedQueryResponse(
                query_id=query_id,
                release_id=published.release.id,
                spec_hash=published.release.spec_hash,
                index_snapshot_id=published.index_snapshot.id,
                trace=tuple(trace),
                # A failure is reported once and the request is not replayable,
                # so stage attribution always ships. It names the failing stage
                # and a next step; the detailed evidence stays behind
                # include_diagnostics.
                diagnostics=_error_diagnosis(exc),
                error=QueryError(stage=exc.stage, code=exc.code, message=_failure_message(exc)),
            )
        except Exception:
            LOGGER.exception(
                "Unhandled analytics query error query_id=%s project_id=%s",
                query_id,
                request.project_id,
            )
            failure_stage = (
                trace[-1].stage if trace and trace[-1].status == "started" else QueryStage.FINISHED
            )
            _mark_failed(trace, failure_stage, {"code": "INTERNAL_ERROR"})
            message = "问数服务发生内部错误，请稍后重试。"
            diagnosis = QueryDiagnosis(
                category=QueryDiagnosticCategory.INTERNAL,
                stage=failure_stage.value,
                severity="error",
                summary="问数链路发生未处理的内部错误",
                recommendation=f"复制 query_id {query_id} 并查询服务日志。",
            )
            if published is None:
                return self._failed_without_release(
                    query_id,
                    trace,
                    "INTERNAL_ERROR",
                    message,
                    diagnostics=diagnosis,
                )
            return FailedQueryResponse(
                query_id=query_id,
                release_id=published.release.id,
                spec_hash=published.release.spec_hash,
                index_snapshot_id=published.index_snapshot.id,
                trace=tuple(trace),
                diagnostics=diagnosis,
                error=QueryError(
                    stage=failure_stage.value,
                    code="INTERNAL_ERROR",
                    message=message,
                ),
            )

    def query_structured(
        self,
        request: StructuredQueryRequest,
        *,
        now: datetime | None = None,
        semantic_release: SemanticRelease | None = None,
        actor_id: str | None = None,
    ) -> QueryResponse:
        """Execute QueryStructReq without Mapper, LLM, or semantic index.

        The semantic layer builds a ``StructQuery`` directly and hands it to the
        structured parser.  A caller
        may bind an unpublished Candidate release explicitly; active-release
        callers continue to resolve it from the normal release provider.
        ``actor_id`` only binds drilldown continuations; omitting it keeps the
        response free of signed tokens.
        """

        query_id = request.query_id or f"q_{uuid.uuid4().hex}"
        trace: list[QueryTraceStep] = [QueryTraceStep(stage=QueryStage.PRECHECK, status="started")]
        published: PublishedRelease | None = None
        bound_release = semantic_release
        index_snapshot_id: str | None = None
        try:
            if bound_release is None:
                published = self._releases.get_active_release(request.project_id)
                bound_release = published.release
                index_snapshot_id = published.index_snapshot.id
            release = bound_release
            if release.project_id != request.project_id:
                raise SemanticParsingError(
                    "请求的语义模型不属于当前项目",
                    code="RELEASE_SCOPE_VIOLATION",
                )
            if request.semantic_query.dataset_id not in {item.id for item in release.datasets}:
                raise SemanticParsingError(
                    "请求的数据集不属于当前发布版本",
                    code="DATASET_SCOPE_VIOLATION",
                )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.PRECHECK,
                status="completed",
                detail={
                    "release_id": release.id,
                    "spec_hash": release.spec_hash,
                    "index_snapshot_id": index_snapshot_id,
                    "semantic_index_required": False,
                    "entry": "structured",
                },
            )
            trace.append(QueryTraceStep(stage=QueryStage.S2SQL_CORRECTING, status="started"))
            corrected = self._orchestrator.correct_structured(
                query=request.semantic_query,
                release=release,
                now=now,
            )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.S2SQL_CORRECTING,
                status="completed",
                detail={"registry": list(self._orchestrator.structured_parser_registry)},
            )
            trace.append(QueryTraceStep(stage=QueryStage.ROUTE_BINDING, status="started"))
            physical = self._translator.translate(
                release=release,
                query=corrected.semantic_query,
            )
            trace[-1] = QueryTraceStep(
                stage=QueryStage.ROUTE_BINDING,
                status="completed",
                detail={"relation_ids": list(physical.relation_ids)},
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.TRANSLATING,
                    status="completed",
                    detail={"relation_ids": list(physical.relation_ids)},
                )
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.PHYSICAL_SQL_VALIDATING,
                    status="completed",
                    detail={"guard": "executor_preflight"},
                )
            )
            self._dry_run(physical=physical, release=release, trace=trace)
            trace.append(QueryTraceStep(stage=QueryStage.EXECUTING, status="started"))
            result = self._executor.execute(query=physical, release=release)
            trace[-1] = QueryTraceStep(
                stage=QueryStage.EXECUTING,
                status="completed",
                detail={"row_count": result.row_count, "truncated": result.truncated},
            )
            trace.extend(
                (
                    QueryTraceStep(stage=QueryStage.POST_PROCESSING, status="completed"),
                    QueryTraceStep(stage=QueryStage.FINISHED, status="completed"),
                )
            )
            defaults = tuple(
                dict.fromkeys((*corrected.applied_defaults, *physical.applied_defaults))
            )
            return CompletedQueryResponse(
                query_id=query_id,
                release_id=release.id,
                spec_hash=release.spec_hash,
                index_snapshot_id=index_snapshot_id,
                trace=tuple(trace),
                interpretation=self._interpretation(
                    release,
                    corrected.semantic_query,
                    defaults,
                ),
                data=result,
                visualization=self._visualization(
                    release, corrected.semantic_query, corrected.canonical_s2sql
                ),
                semantic_query=corrected.semantic_query,
                drilldown=self._drilldown_options(
                    release=release,
                    query=corrected.semantic_query,
                    project_id=request.project_id,
                    query_id=query_id,
                    actor_id=actor_id,
                ),
                parsed_s2sql=corrected.canonical_s2sql,
                corrected_s2sql=corrected.canonical_s2sql,
                physical_sql=physical.sql if request.include_debug_sql else None,
            )
        except AnalyticsError as exc:
            trace.append(
                QueryTraceStep(
                    stage=_stage_or_precheck(exc.stage),
                    status="failed",
                    detail={"code": exc.code},
                )
            )
            if bound_release is None:
                return self._failed_without_release(query_id, trace, exc.code, str(exc), exc.stage)
            return FailedQueryResponse(
                query_id=query_id,
                release_id=bound_release.id,
                spec_hash=bound_release.spec_hash,
                index_snapshot_id=index_snapshot_id,
                trace=tuple(trace),
                error=QueryError(stage=exc.stage, code=exc.code, message=_failure_message(exc)),
            )
        except Exception:
            LOGGER.exception(
                "Unhandled structured analytics query error query_id=%s project_id=%s",
                query_id,
                request.project_id,
            )
            trace.append(
                QueryTraceStep(
                    stage=QueryStage.FINISHED,
                    status="failed",
                    detail={"code": "INTERNAL_ERROR"},
                )
            )
            message = "问数服务发生内部错误，请稍后重试。"
            if bound_release is None:
                return self._failed_without_release(query_id, trace, "INTERNAL_ERROR", message)
            return FailedQueryResponse(
                query_id=query_id,
                release_id=bound_release.id,
                spec_hash=bound_release.spec_hash,
                index_snapshot_id=index_snapshot_id,
                trace=tuple(trace),
                error=QueryError(
                    stage=QueryStage.FINISHED.value,
                    code="INTERNAL_ERROR",
                    message=message,
                ),
            )

    @staticmethod
    def _metric_matches(
        projections: tuple[MappingResult, ...],
        *,
        methods: frozenset[MatchMethod],
    ) -> tuple[SchemaMatch, ...]:
        """Collect every surviving metric candidate from governed Scope views."""

        return tuple(
            match
            for projection in projections
            for match in projection.matches
            if match.element_type is SemanticElementType.METRIC and match.method in methods
        )

    def _should_adjudicate_weak_metric(
        self,
        *,
        confirmation_kind: str,
        metric_ids: tuple[str, ...],
    ) -> bool:
        return (
            self._weak_metric_adjudicator is not None
            and self._weak_metric_adjudication_mode is not WeakMetricAdjudicationMode.OFF
            and confirmation_kind == "metric"
            and bool(metric_ids)
        )

    def _should_adjudicate_intent(
        self,
        *,
        confirmation_kind: str,
        options: tuple[ClarificationOption, ...],
        mode: WeakMetricAdjudicationMode,
    ) -> bool:
        return (
            self._intent_adjudicator is not None
            and mode is not WeakMetricAdjudicationMode.OFF
            and confirmation_kind in {"semantic_element", "metric_multi", "analysis_object"}
            and bool(options)
        )

    @staticmethod
    def _intent_candidates_for_options(
        *,
        release: SemanticRelease,
        options: tuple[ClarificationOption, ...],
        intent_kind: str,
    ) -> tuple[IntentAdjudicationCandidate, ...]:
        metrics = {item.id: item for item in release.metrics}
        dimensions = {item.id: item for item in release.dimensions}
        values = {item.id: item for item in release.dimension_values if item.enabled}
        models = {item.id: item for item in release.models}
        datasets = {item.id: item for item in release.datasets}
        routes = {item.dataset_id: item for item in release.analysis_topic_routes}
        candidates: list[IntentAdjudicationCandidate] = []
        for option in options:
            aliases: tuple[str, ...] = ()
            context: list[str] = []
            if option.kind == "metric" and option.element_id in metrics:
                metric = metrics[option.element_id]
                aliases = metric.aliases
                context.extend(
                    item
                    for item in (
                        f"聚合：{metric.aggregation.value}"
                        if metric.aggregation is not None
                        else None,
                        f"单位：{metric.unit}" if metric.unit else None,
                    )
                    if item is not None
                )
            elif option.kind == "dimension" and option.element_id in dimensions:
                dimension = dimensions[option.element_id]
                aliases = dimension.aliases
                context.append(f"维度类型：{dimension.semantic_type}")
            elif option.kind == "dimension_value" and option.element_id in values:
                value = values[option.element_id]
                aliases = value.aliases
                dimension = dimensions.get(value.dimension_id)
                if dimension is not None:
                    context.append(f"所属维度：{dimension.name}")
            elif option.kind == "analysis_object":
                route = routes.get(option.dataset_id)
                model = models.get(route.root_model_id) if route is not None else None
                dataset = datasets.get(option.dataset_id)
                if model is not None:
                    aliases = model.aliases
                if dataset is not None:
                    metric_names = [
                        metrics[item].name for item in dataset.metric_ids if item in metrics
                    ][:8]
                    dimension_names = [
                        dimensions[item].name
                        for item in dataset.dimension_ids
                        if item in dimensions
                    ][:8]
                    if metric_names:
                        context.append(f"可分析指标：{'、'.join(metric_names)}")
                    if dimension_names:
                        context.append(f"可分析维度：{'、'.join(dimension_names)}")
            candidates.append(
                IntentAdjudicationCandidate(
                    selection_id=option.candidate_id,
                    kind=option.kind,
                    label=option.label,
                    description=option.description,
                    aliases=aliases,
                    business_context=tuple(context),
                )
            )
        return tuple(candidates)

    @staticmethod
    def _group_metric_options_by_detected_text(
        *,
        projections: tuple[MappingResult, ...],
        options: tuple[ClarificationOption, ...],
    ) -> tuple[tuple[str, tuple[ClarificationOption, ...]], ...]:
        detected_by_element: dict[str, set[str]] = {}
        term_detected_text = {
            match.entry_id: match.detected_text
            for projection in projections
            for match in projection.matches
            if match.element_type is SemanticElementType.TERM
        }
        for projection in projections:
            for match in projection.matches:
                if match.element_type is SemanticElementType.METRIC and match.method in {
                    MatchMethod.KEYWORD,
                    MatchMethod.TERM,
                    MatchMethod.EMBEDDING,
                }:
                    detected_text = match.detected_text
                    if match.detected_span_source.startswith("term:"):
                        detected_text = term_detected_text.get(
                            match.detected_span_source.removeprefix("term:"),
                            detected_text,
                        )
                    detected_by_element.setdefault(match.element_id, set()).add(detected_text)
        grouped: dict[str, list[ClarificationOption]] = {}
        display_text: dict[str, set[str]] = {}
        for option in options:
            if option.kind != "metric" or option.element_id is None:
                continue
            for detected_text in detected_by_element.get(option.element_id, ()):
                key = normalize_text(detected_text)
                if not key:
                    continue
                grouped.setdefault(key, []).append(option)
                display_text.setdefault(key, set()).add(detected_text)
        return tuple(
            (
                min(display_text[key], key=lambda value: (value.casefold(), value)),
                tuple(
                    {
                        item.candidate_id: item
                        for item in sorted(
                            grouped[key],
                            key=lambda candidate: candidate.candidate_id,
                        )
                    }.values()
                ),
            )
            for key in sorted(grouped)
        )

    def _adjudicate_intent(
        self,
        *,
        intent_kind: str,
        question: str,
        detected_text: str,
        candidates: tuple[IntentAdjudicationCandidate, ...],
        exact_context: tuple[str, ...],
        query_id: str,
        tenant_id: str,
        release: SemanticRelease,
    ) -> IntentAdjudicationResult:
        assert self._intent_adjudicator is not None
        try:
            result = self._intent_adjudicator.adjudicate(
                intent_kind=intent_kind,
                question=question,
                detected_text=detected_text,
                candidates=candidates,
                exact_context=exact_context,
                query_id=query_id,
                tenant_id=tenant_id,
                release_id=release.id,
                spec_hash=release.spec_hash,
            )
        except Exception:
            LOGGER.warning(
                "intent adjudicator failed query_id=%s kind=%s; using human confirmation",
                query_id,
                intent_kind,
            )
            return IntentAdjudicationResult(
                decision=IntentAdjudicationDecision.UNAVAILABLE,
                candidate_set_hash=content_hash(
                    {
                        "contract": "knowflow-intent-adjudication-v2",
                        "intent_kind": intent_kind,
                        "candidate_count": len(candidates),
                    }
                ),
                failure_code="INTENT_ADJUDICATOR_FAILED",
            )
        offered = {item.selection_id for item in candidates}
        if (
            result.decision is IntentAdjudicationDecision.MATCH
            and result.selection_id not in offered
        ):
            return IntentAdjudicationResult(
                decision=IntentAdjudicationDecision.UNAVAILABLE,
                candidate_set_hash=result.candidate_set_hash,
                failure_code="MODEL_OUTPUT_INVALID",
            )
        return result

    def _adjudicate_intent_many(
        self,
        *,
        intent_kind: str,
        question: str,
        groups: tuple[IntentAdjudicationGroup, ...],
        exact_context: tuple[str, ...],
        query_id: str,
        tenant_id: str,
        release: SemanticRelease,
    ) -> IntentAdjudicationBatchResult:
        assert self._intent_adjudicator is not None
        fallback_hash = content_hash(
            [
                {
                    "detected_text": normalize_text(group.detected_text),
                    "candidate_count": len(group.candidates),
                }
                for group in groups
            ]
        )
        try:
            result = self._intent_adjudicator.adjudicate_many(
                intent_kind=intent_kind,
                question=question,
                groups=groups,
                exact_context=exact_context,
                query_id=query_id,
                tenant_id=tenant_id,
                release_id=release.id,
                spec_hash=release.spec_hash,
            )
        except Exception:
            LOGGER.warning(
                "batch intent adjudicator failed query_id=%s kind=%s; using human confirmation",
                query_id,
                intent_kind,
            )
            return IntentAdjudicationBatchResult(
                candidate_set_hash=fallback_hash,
                failure_code="INTENT_ADJUDICATOR_FAILED",
            )
        offered = {
            normalize_text(group.detected_text): {
                candidate.selection_id for candidate in group.candidates
            }
            for group in groups
        }
        returned = {normalize_text(item.detected_text): item for item in result.items}
        if len(returned) != len(result.items) or (
            result.failure_code is None and set(returned) != set(offered)
        ):
            return IntentAdjudicationBatchResult(
                candidate_set_hash=result.candidate_set_hash,
                failure_code="MODEL_OUTPUT_INVALID",
            )
        for normalized, item in returned.items():
            if (
                item.result.decision is IntentAdjudicationDecision.MATCH
                and item.result.selection_id not in offered[normalized]
            ):
                return IntentAdjudicationBatchResult(
                    candidate_set_hash=result.candidate_set_hash,
                    failure_code="MODEL_OUTPUT_INVALID",
                )
        return result

    def _adjudicate_metric_option_groups(
        self,
        *,
        question: str,
        release: SemanticRelease,
        phrase_groups: tuple[tuple[str, tuple[ClarificationOption, ...]], ...],
        exact_context: tuple[str, ...],
        query_id: str,
        tenant_id: str,
    ) -> tuple[
        tuple[tuple[str, ClarificationOption, tuple[ClarificationOption, ...]], ...],
        dict[str, object],
    ]:
        grouped = phrase_groups
        batch = self._adjudicate_intent_many(
            intent_kind="semantic_element",
            question=question,
            groups=tuple(
                IntentAdjudicationGroup(
                    detected_text=detected_text,
                    candidates=self._intent_candidates_for_options(
                        release=release,
                        options=group_options,
                        intent_kind="semantic_element",
                    ),
                )
                for detected_text, group_options in grouped
            ),
            exact_context=exact_context,
            query_id=query_id,
            tenant_id=tenant_id,
            release=release,
        )
        results_by_text = {normalize_text(item.detected_text): item.result for item in batch.items}
        selected: list[tuple[str, ClarificationOption, tuple[ClarificationOption, ...]]] = []
        group_details: list[dict[str, object]] = []
        for detected_text, group_options in grouped:
            result = results_by_text.get(
                normalize_text(detected_text),
                IntentAdjudicationResult(
                    decision=IntentAdjudicationDecision.UNAVAILABLE,
                    candidate_set_hash=batch.candidate_set_hash,
                    failure_code=(batch.failure_code or "MODEL_OUTPUT_INVALID"),
                ),
            )
            group_details.append(
                {
                    "detected_text": detected_text,
                    "decision": result.decision.value,
                    "candidate_set_hash": result.candidate_set_hash,
                    **(
                        {"failure_code": result.failure_code}
                        if result.failure_code is not None
                        else {}
                    ),
                }
            )
            if result.decision is not IntentAdjudicationDecision.MATCH:
                continue
            chosen = next(
                (item for item in group_options if item.candidate_id == result.selection_id),
                None,
            )
            if chosen is not None:
                selected.append((detected_text, chosen, group_options))
        complete = bool(grouped) and len(selected) == len(grouped)
        detail: dict[str, object] = {
            "mode": self._semantic_intent_adjudication_mode.value,
            "decision": "MATCH" if complete else "AMBIGUOUS",
            "candidate_set_hash": batch.candidate_set_hash,
            "groups": group_details,
            "adopted": False,
        }
        if batch.failure_code is not None:
            detail["failure_code"] = batch.failure_code
        if not complete:
            detail["fallback"] = "human_confirmation"
            return (), detail
        return tuple(selected), detail

    @staticmethod
    def _intent_trace_detail(
        result: IntentAdjudicationResult,
        *,
        mode: WeakMetricAdjudicationMode,
    ) -> dict[str, object]:
        detail: dict[str, object] = {
            "mode": mode.value,
            "decision": result.decision.value,
            "candidate_set_hash": result.candidate_set_hash,
        }
        if result.failure_code:
            detail["failure_code"] = result.failure_code
        return detail

    @staticmethod
    def _decision_obligation(
        *,
        release: SemanticRelease,
        detected_text: str,
        source: SemanticDecisionSource,
        chosen: ClarificationOption,
        options: tuple[ClarificationOption, ...],
    ) -> SemanticDecisionObligation | None:
        typed = tuple(
            {
                (item.element_type, item.element_id): SemanticAmbiguityMember(
                    element_type=SemanticElementType(item.element_type),
                    element_id=item.element_id,
                )
                for item in options
                if item.element_type in {"metric", "dimension", "dimension_value"}
                and item.element_id is not None
            }.values()
        )
        selected = next(
            (
                member
                for member in typed
                if member.element_type.value == chosen.element_type
                and member.element_id == chosen.element_id
            ),
            None,
        )
        if selected is None:
            return None
        values = {item.id: item for item in release.dimension_values if item.enabled}
        return SemanticDecisionObligation(
            detected_text=detected_text or chosen.label,
            source=source,
            selected=selected,
            candidates=typed,
            chosen_option=chosen,
            options=options,
            value_bindings=tuple(
                SemanticValueBinding(
                    element_id=member.element_id,
                    dimension_id=values[member.element_id].dimension_id,
                    raw_value=values[member.element_id].value,
                )
                for member in typed
                if member.element_type is SemanticElementType.DIMENSION_VALUE
                and member.element_id in values
            ),
        )

    @staticmethod
    def _semantic_decision(
        *,
        detected_text: str,
        source: SemanticDecisionSource,
        chosen: ClarificationOption,
        options: tuple[ClarificationOption, ...],
    ) -> SemanticDecision:
        return SemanticDecision(
            source=source,
            detected_text=detected_text or chosen.label,
            chosen=chosen,
            alternatives=tuple(
                item for item in options if item.candidate_id != chosen.candidate_id
            ),
        )

    @staticmethod
    def _confirmation_memory_hashes(
        *,
        options: tuple[ClarificationOption, ...],
        exact_context_hash: str,
    ) -> tuple[str, str]:
        candidate_hash = confirmation_candidate_set_hash(
            (
                item.kind,
                item.element_id or item.label,
                item.dataset_id or None,
            )
            for item in options
        )
        return candidate_hash, exact_context_hash

    def _recall_confirmation_memory(
        self,
        *,
        actor_id: str,
        project_id: str,
        release: SemanticRelease,
        index_snapshot_id: str,
        detected_text: str,
        options: tuple[ClarificationOption, ...],
        exact_context_hash: str,
        now: datetime,
    ) -> tuple[ConfirmationMemory | None, ClarificationOption | None, bool]:
        if self._confirmation_memories is None or not actor_id:
            return None, None, False
        candidate_hash, context_hash = self._confirmation_memory_hashes(
            options=options,
            exact_context_hash=exact_context_hash,
        )
        try:
            memory = self._confirmation_memories.find_confirmation_memory(
                actor_id=actor_id,
                project_id=project_id,
                release_id=release.id,
                spec_hash=release.spec_hash,
                index_snapshot_id=index_snapshot_id,
                normalized_phrase=normalize_text(detected_text),
                candidate_set_hash=candidate_hash,
                exact_context_hash=context_hash,
                now=now,
            )
        except Exception:
            LOGGER.exception(
                "Failed to read confirmation memory project_id=%s",
                project_id,
            )
            return None, None, True
        if memory is None:
            return None, None, False
        chosen = next(
            (
                item
                for item in options
                if item.kind == memory.selection_kind
                and item.element_id == memory.semantic_element_id
                and (memory.dataset_id is None or item.dataset_id == memory.dataset_id)
            ),
            None,
        )
        return (memory, chosen, False) if chosen is not None else (None, None, False)

    def _confirmation_memory_from_choice(
        self,
        *,
        actor_id: str,
        project_id: str,
        release: SemanticRelease,
        index_snapshot_id: str,
        detected_text: str,
        chosen: ClarificationOption,
        options: tuple[ClarificationOption, ...],
        exact_context_hash: str,
        now: datetime,
    ) -> ConfirmationMemory | None:
        if (
            self._confirmation_memories is None
            or not actor_id
            or (chosen.kind != "analysis_object" and chosen.element_id is None)
        ):
            return None
        candidate_hash, context_hash = self._confirmation_memory_hashes(
            options=options,
            exact_context_hash=exact_context_hash,
        )
        memory_identity = {
            "actor_id": actor_id,
            "project_id": project_id,
            "release_id": release.id,
            "spec_hash": release.spec_hash,
            "index_snapshot_id": index_snapshot_id,
            "normalized_phrase": normalize_text(detected_text),
            "selection_kind": chosen.kind,
            "semantic_element_id": (
                None if chosen.kind == "analysis_object" else chosen.element_id
            ),
            "dataset_id": chosen.dataset_id or None,
            "candidate_set_hash": candidate_hash,
            "exact_context_hash": context_hash,
        }
        return ConfirmationMemory(
            id=("cmem_" + content_hash(memory_identity).removeprefix("sha256:")[:32]),
            actor_id=actor_id,
            project_id=project_id,
            release_id=release.id,
            spec_hash=release.spec_hash,
            index_snapshot_id=index_snapshot_id,
            detected_text=detected_text,
            normalized_phrase=normalize_text(detected_text),
            selection_kind=chosen.kind,
            semantic_element_id=(None if chosen.kind == "analysis_object" else chosen.element_id),
            dataset_id=chosen.dataset_id or None,
            candidate_set_hash=candidate_hash,
            exact_context_hash=context_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=self._confirmation_memory_ttl_seconds),
        )

    def _recall_metric_phrase_memories(
        self,
        *,
        actor_id: str,
        project_id: str,
        release: SemanticRelease,
        index_snapshot_id: str,
        combination_options: tuple[ClarificationOption, ...],
        phrase_groups: tuple[tuple[str, tuple[ClarificationOption, ...]], ...],
        selection_context: _SelectionTokenContext,
        exact_context_hash: str,
        now: datetime,
    ) -> tuple[
        tuple[
            str,
            tuple[
                tuple[
                    str,
                    ClarificationOption,
                    tuple[ClarificationOption, ...],
                    ConfirmationMemory,
                ],
                ...,
            ],
        ]
        | None,
        bool,
    ]:
        candidates = []
        datasets = {item.id: item for item in release.datasets}
        for dataset_id in dict.fromkeys(item.dataset_id for item in combination_options):
            dataset = datasets.get(dataset_id)
            if dataset is None:
                continue
            available_metrics = set(dataset.metric_ids)
            selected_groups = []
            for detected_text, group_options in phrase_groups:
                scoped_options = tuple(
                    item.model_copy(
                        update={
                            "candidate_id": self._selection_token(
                                release=release,
                                context=selection_context,
                                dataset_id=dataset_id,
                                semantic_selection_id=f"element:metric:{item.element_id}",
                            ),
                            "dataset_id": dataset_id,
                        }
                    )
                    for item in group_options
                    if item.element_id in available_metrics
                )
                memory, chosen, unavailable = self._recall_confirmation_memory(
                    actor_id=actor_id,
                    project_id=project_id,
                    release=release,
                    index_snapshot_id=index_snapshot_id,
                    detected_text=detected_text,
                    options=scoped_options,
                    exact_context_hash=exact_context_hash,
                    now=now,
                )
                if unavailable:
                    return None, True
                if memory is None or chosen is None:
                    break
                selected_groups.append((detected_text, chosen, scoped_options, memory))
            if len(selected_groups) != len(phrase_groups):
                continue
            selected_metric_ids = tuple(
                dict.fromkeys(
                    chosen.element_id
                    for _detected, chosen, _options, _memory in selected_groups
                    if chosen.element_id is not None
                )
            )
            if any(
                item.dataset_id == dataset_id
                and tuple(
                    selection_id.removeprefix("element:metric:")
                    for selection_id in item.semantic_selection_ids
                )
                == selected_metric_ids
                for item in combination_options
            ):
                candidates.append((dataset_id, tuple(selected_groups)))
        return (candidates[0] if len(candidates) == 1 else None), False

    @staticmethod
    def _exact_semantic_binding_hash(evidence: MappingEvidence) -> str:
        """Hash governed exact identities, separate from the AI display text."""

        return content_hash(
            sorted(
                {
                    (
                        match.element_type.value,
                        match.element_id,
                        match.dimension_id or "",
                        type(match.raw_value).__name__,
                        str(match.raw_value),
                        match.channel.value,
                        match.entry_source,
                    )
                    for match in evidence.matches
                    if match.method is MatchMethod.EXACT
                    and match.origin_term_entry_id is None
                    and match.channel
                    not in {
                        MappingEvidenceChannel.TERM_DICTIONARY,
                        MappingEvidenceChannel.TERM_DATABASE,
                        MappingEvidenceChannel.TERM_EMBEDDING,
                        MappingEvidenceChannel.MANIFEST,
                    }
                }
            )
        )

    @staticmethod
    def _exact_semantic_context(
        release: SemanticRelease,
        evidence: MappingEvidence,
    ) -> tuple[str, ...]:
        dimensions = {item.id: item for item in release.dimensions}
        values = {item.id: item for item in release.dimension_values if item.enabled}
        value_context: dict[str, set[str]] = {}
        direct_dimensions: set[str] = set()
        for match in evidence.matches:
            if (
                match.method is not MatchMethod.EXACT
                or match.origin_term_entry_id is not None
                or match.channel
                in {
                    MappingEvidenceChannel.TERM_DICTIONARY,
                    MappingEvidenceChannel.TERM_DATABASE,
                    MappingEvidenceChannel.TERM_EMBEDDING,
                    MappingEvidenceChannel.MANIFEST,
                }
            ):
                continue
            if match.element_type is SemanticElementType.DIMENSION:
                if match.element_id in dimensions:
                    direct_dimensions.add(match.element_id)
            elif match.element_type is SemanticElementType.DIMENSION_VALUE:
                value = values.get(match.element_id)
                if value is None or value.dimension_id not in dimensions:
                    continue
                value_context.setdefault(value.dimension_id, set()).add(value.display_name)
        rendered = {
            f"{dimensions[dimension_id].name} = {display_name}"
            for dimension_id, display_names in value_context.items()
            for display_name in display_names
        }
        rendered.update(
            dimensions[dimension_id].name
            for dimension_id in direct_dimensions - value_context.keys()
        )
        return tuple(sorted(rendered, key=str.casefold))

    def _adjudicate_weak_metric(
        self,
        *,
        question: str,
        detected_text: str,
        release: SemanticRelease,
        metric_ids: tuple[str, ...],
        evidence: MappingEvidence,
        query_id: str,
        tenant_id: str,
    ) -> WeakMetricAdjudicationResult:
        assert self._weak_metric_adjudicator is not None
        try:
            result = self._weak_metric_adjudicator.adjudicate(
                question=question,
                detected_text=detected_text,
                release=release,
                metric_ids=metric_ids,
                exact_context=self._exact_semantic_context(release, evidence),
                query_id=query_id,
                tenant_id=tenant_id,
            )
        except Exception:
            LOGGER.warning(
                "weak metric adjudicator failed query_id=%s; using human confirmation",
                query_id,
            )
            return WeakMetricAdjudicationResult(
                decision=WeakMetricAdjudicationDecision.UNAVAILABLE,
                candidate_set_hash=content_hash(
                    {
                        "contract": "knowflow-weak-metric-adjudication-v1",
                        "candidate_count": len(metric_ids),
                    }
                ),
                failure_code="WEAK_METRIC_ADJUDICATOR_FAILED",
            )
        if (
            result.decision is WeakMetricAdjudicationDecision.MATCH
            and result.metric_id not in metric_ids
        ):
            return WeakMetricAdjudicationResult(
                decision=WeakMetricAdjudicationDecision.UNAVAILABLE,
                candidate_set_hash=result.candidate_set_hash,
                failure_code="MODEL_OUTPUT_INVALID",
            )
        return result

    @staticmethod
    def _adjudication_trace_detail(
        result: WeakMetricAdjudicationResult,
        *,
        mode: WeakMetricAdjudicationMode,
    ) -> dict[str, object]:
        detail: dict[str, object] = {
            "mode": mode.value,
            "decision": result.decision.value,
            "candidate_set_hash": result.candidate_set_hash,
        }
        if result.failure_code:
            detail["failure_code"] = result.failure_code
        return detail

    @staticmethod
    def _weak_metric_clarification_trace(
        *,
        scope_resolution,
        confirmation_kind: str,
        detected_text: str,
        options: tuple[ClarificationOption, ...],
        projections: tuple[MappingResult, ...],
        adjudication_detail: dict[str, object] | None,
        include_diagnostics: bool,
        adjudication_key: str = "weak_metric_adjudication",
    ) -> QueryTraceStep:
        detail: dict[str, object] = {
            "semantic_confirmation": {
                "kind": confirmation_kind,
                "detected_text": detected_text,
            }
        }
        if include_diagnostics:
            detail.update(
                {
                    "scope_resolution": scope_resolution.to_trace_detail(),
                    "candidate_ids": [item.candidate_id for item in options],
                    "mapping_attempts": [item.model_dump(mode="json") for item in projections],
                }
            )
            if adjudication_detail is not None:
                detail[adjudication_key] = adjudication_detail
        return QueryTraceStep(
            stage=QueryStage.CANDIDATE_DISCOVERY,
            status="clarification",
            detail=detail,
        )

    @staticmethod
    def _weak_metric_clarification_response(
        *,
        query_id: str,
        release: SemanticRelease,
        index_snapshot_id: str,
        trace: list[QueryTraceStep],
        confirmation_kind: str,
        question: str,
        options: tuple[ClarificationOption, ...],
    ) -> ClarificationQueryResponse:
        return ClarificationQueryResponse(
            query_id=query_id,
            release_id=release.id,
            spec_hash=release.spec_hash,
            index_snapshot_id=index_snapshot_id,
            trace=tuple(trace),
            diagnostics=QueryDiagnosis(
                category=QueryDiagnosticCategory.AMBIGUITY,
                stage=QueryStage.CANDIDATE_DISCOVERY.value,
                severity="warning",
                summary=(
                    "问题中的指标说法尚未成为已治理精确语义。"
                    if confirmation_kind == "metric"
                    else "同一说法可能对应不同类型的业务语义。"
                ),
                recommendation=(
                    "确认业务指标；可在建模时把该说法审核为别名。"
                    if confirmation_kind == "metric"
                    else "确认指标或维度后重新执行。"
                ),
                user_hint=(
                    "请选择你实际想查询的业务指标。"
                    if confirmation_kind == "metric"
                    else "请选择你实际表达的业务含义。"
                ),
            ),
            question=question,
            options=options,
        )

    def _weak_metric_confirmation(
        self,
        *,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        selection_context: _SelectionTokenContext,
    ) -> (
        tuple[
            str,
            str,
            str,
            tuple[ClarificationOption, ...],
            tuple[MappingResult, ...],
            tuple[tuple[str, tuple[ClarificationOption, ...]], ...],
        ]
        | None
    ):
        """Build candidate-only metric confirmation without making a choice.

        Stage contract: run after global retrieval and exact Scope resolution,
        before Rule/LLM parsing.  Existing per-Scope Mapper projection remains
        the candidate authority.  Keyword/Term evidence is considered before
        embedding evidence, but every surviving semantic ID in the chosen
        evidence class is shown; scores and score gaps never select one.
        """

        moderate_projections = self._orchestrator.project_scope_evidence(
            evidence=evidence,
            dataset_ids=dataset_ids,
            mode=MapMode.MODERATE,
        )
        loose_projections = self._orchestrator.project_scope_evidence(
            evidence=evidence,
            dataset_ids=dataset_ids,
            mode=MapMode.LOOSE,
        )
        projections = (*moderate_projections, *loose_projections)
        lexical_matches = self._metric_matches(
            moderate_projections,
            methods=frozenset({MatchMethod.KEYWORD, MatchMethod.TERM}),
        )
        embedding_matches = self._metric_matches(
            projections,
            methods=frozenset({MatchMethod.EMBEDDING}),
        )
        groups: dict[str, dict[str, SchemaMatch]] = {}
        detected_by_group: dict[str, set[str]] = {}
        term_detected_text = {
            match.entry_id: match.detected_text
            for projection in projections
            for match in projection.matches
            if match.element_type is SemanticElementType.TERM
        }
        evidence_classes: dict[str, dict[str, dict[str, SchemaMatch]]] = {}
        for evidence_class, matches in (
            ("lexical", lexical_matches),
            ("embedding", embedding_matches),
        ):
            for match in matches:
                detected_text = match.detected_text
                if match.detected_span_source.startswith("term:"):
                    detected_text = term_detected_text.get(
                        match.detected_span_source.removeprefix("term:"),
                        detected_text,
                    )
                normalized = "".join(str(detected_text).casefold().split())
                if not normalized:
                    continue
                evidence_classes.setdefault(normalized, {}).setdefault(evidence_class, {})[
                    match.element_id
                ] = match
                detected_by_group.setdefault(normalized, set()).add(detected_text)
        for normalized, classes in evidence_classes.items():
            # Evidence priority is phrase-local. A lexical hit for one phrase
            # cannot erase an independent embedding-only metric phrase.
            if classes.get("lexical"):
                groups[normalized] = classes["lexical"]
            else:
                groups[normalized] = classes.get("embedding", {})
        if not groups:
            return None
        # One continuation token confirms one phrase. Multiple independent weak
        # metric phrases always remain fail-closed in V1, even when their
        # candidates share an owner; owner-local weak settlement is deferred to
        # the reviewed V2 design.
        if len(groups) > 1:
            detected_text = "、".join(min(detected_by_group[key]) for key in sorted(groups))
            phrase_groups = tuple(
                (
                    min(detected_by_group[key]),
                    self._semantic_options(
                        release,
                        tuple(groups[key]),
                        selection_context=selection_context,
                        require_time=False,
                        typed_members=tuple(
                            SemanticAmbiguityMember(
                                element_type=SemanticElementType.METRIC,
                                element_id=element_id,
                            )
                            for element_id in groups[key]
                        ),
                        allowed_dataset_ids=dataset_ids,
                    ),
                )
                for key in sorted(groups)
            )
            owner_by_metric = {item.id: item.model_id for item in release.metrics}
            if phrase_groups and all(
                len({owner_by_metric.get(choice.element_id or "") for choice in choices}) > 1
                for choices in product(*(options for _phrase, options in phrase_groups))
            ):
                raise MappingError(
                    "多个精确指标分别属于不同事实根，当前版本不支持跨事实查询，请拆分问题。",
                    code="CROSS_FACT_METRICS_UNSUPPORTED",
                )
            options = self._bundle_metric_phrase_groups(
                release=release,
                evidence=evidence,
                dataset_ids=dataset_ids,
                groups=phrase_groups,
                selection_context=selection_context,
            )
            return (
                "metric_multi",
                detected_text,
                "问题中存在多个非精确指标说法，请分别使用已治理指标名称后重试。",
                options,
                projections,
                phrase_groups,
            )
        normalized, candidates = next(iter(groups.items()))
        detected_text = min(detected_by_group[normalized])
        typed_members: dict[tuple[SemanticElementType, str], SemanticAmbiguityMember] = {
            (SemanticElementType.METRIC, element_id): SemanticAmbiguityMember(
                element_type=SemanticElementType.METRIC,
                element_id=element_id,
            )
            for element_id in candidates
        }
        for projection in projections:
            for group in projection.semantic_ambiguity_groups:
                group_normalized = "".join(group.detected_text.casefold().split())
                if group_normalized != normalized and not any(
                    "".join(match.detected_text.casefold().split()) == group_normalized
                    for match in candidates.values()
                ):
                    continue
                if not any(
                    member.element_type is SemanticElementType.METRIC
                    and member.element_id in candidates
                    for member in group.members
                ):
                    continue
                for member in group.members:
                    typed_members[(member.element_type, member.element_id)] = member
        members = tuple(
            typed_members[key]
            for key in sorted(typed_members, key=lambda item: (item[0].value, item[1]))
        )
        if len(members) > _MAX_SEMANTIC_CONFIRMATION_OPTIONS:
            return (
                "metric_overflow",
                detected_text,
                "候选业务指标过多，请使用更具体的指标名称后重试。",
                (),
                projections,
                (),
            )
        member_ids = tuple(dict.fromkeys(member.element_id for member in members))
        options = self._semantic_options(
            release,
            member_ids,
            selection_context=selection_context,
            require_time=False,
            typed_members=members,
            allowed_dataset_ids=dataset_ids,
        )
        phrase_groups = ((detected_text, options),)
        options = self._bundle_semantic_scope_options(
            release=release,
            evidence=evidence,
            dataset_ids=dataset_ids,
            options=options,
            selection_context=selection_context,
        )
        if not options:
            return None
        confirmation_kind = (
            "metric" if all(item.kind == "metric" for item in options) else "semantic_element"
        )
        question = (
            f"你说的「{detected_text}」是否指指标「{options[0].label}」？"
            if len(options) == 1 and confirmation_kind == "metric"
            else f"你说的「{detected_text}」可能对应多个业务指标，请确认具体口径。"
            if confirmation_kind == "metric"
            else f"你说的「{detected_text}」可能对应多个业务含义，请确认具体对象。"
        )
        return (
            confirmation_kind,
            detected_text,
            question,
            options,
            projections,
            phrase_groups,
        )

    def _semantic_confirmation_for_current_evidence(
        self,
        *,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        selection_context: _SelectionTokenContext,
        question: str,
        now: datetime | None,
        selected_element_id: str,
        selected_element_type: SemanticElementType,
        selected_dataset_id: str | None,
    ) -> tuple[str, tuple[ClarificationOption, ...]] | None:
        """Rebuild every semantic card source before accepting a signed choice."""

        resolution = QueryScopeResolver.from_release(release).resolve(
            evidence.matches,
            allowed_dataset_ids=dataset_ids,
        )
        if resolution.ambiguous_metric_groups:
            metric_ids = tuple(
                dict.fromkeys(
                    metric_id
                    for group in resolution.ambiguous_metric_groups
                    for metric_id in group.metric_ids
                )
            )
            options = self._semantic_options(
                release,
                metric_ids,
                selection_context=selection_context,
                require_time=False,
                typed_members=tuple(
                    SemanticAmbiguityMember(
                        element_type=SemanticElementType.METRIC,
                        element_id=metric_id,
                    )
                    for metric_id in metric_ids
                ),
                allowed_dataset_ids=dataset_ids,
            )
            exact_confirmation = (
                "、".join(
                    dict.fromkeys(
                        group.detected_text for group in resolution.ambiguous_metric_groups
                    )
                ),
                self._bundle_semantic_scope_options(
                    release=release,
                    evidence=evidence,
                    dataset_ids=dataset_ids,
                    options=options,
                    selection_context=selection_context,
                ),
            )
            if any(
                item.element_type == selected_element_type.value
                and item.element_id == selected_element_id
                and (selected_dataset_id is None or item.dataset_id == selected_dataset_id)
                for item in exact_confirmation[1]
            ):
                return exact_confirmation
        if not resolution.exact_metric_ids:
            weak = self._weak_metric_confirmation(
                release=release,
                evidence=evidence,
                dataset_ids=dataset_ids,
                selection_context=selection_context,
            )
            if weak is not None:
                candidate = (weak[1], weak[3])
                if any(
                    item.element_type == selected_element_type.value
                    and item.element_id == selected_element_id
                    and (selected_dataset_id is None or item.dataset_id == selected_dataset_id)
                    for item in candidate[1]
                ):
                    return candidate

        projections = self._card_mapping_projections(
            question=question,
            release=release,
            evidence=evidence,
            dataset_ids=dataset_ids,
            now=now,
        )
        seen_groups: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
        for mapping in projections:
            for group in mapping.semantic_ambiguity_groups:
                key = (
                    normalize_text(group.detected_text),
                    tuple(
                        sorted(
                            (member.element_type.value, member.element_id)
                            for member in group.members
                        )
                    ),
                )
                if key in seen_groups:
                    continue
                seen_groups.add(key)
                options = self._semantic_options(
                    release,
                    tuple(member.element_id for member in group.members),
                    selection_context=selection_context,
                    require_time=False,
                    typed_members=group.members,
                    allowed_dataset_ids=dataset_ids,
                )
                options = self._bundle_semantic_scope_options(
                    release=release,
                    evidence=evidence,
                    dataset_ids=dataset_ids,
                    options=options,
                    selection_context=selection_context,
                )
                if any(
                    item.element_type == selected_element_type.value
                    and item.element_id == selected_element_id
                    and (selected_dataset_id is None or item.dataset_id == selected_dataset_id)
                    for item in options
                ):
                    return group.detected_text, options
        return None

    @staticmethod
    def _admit_query_scope_candidates(
        candidates: tuple[ParsedSemanticCandidate, ...],
    ) -> tuple[ParsedSemanticCandidate, ...]:
        """Apply the reviewed exact-metric Scope admission contract.

        Stage boundary: this runs after CandidateOrchestrator discovery and
        before AMBIGUOUS_QUERY_SCOPE. It consumes only governed Mapper evidence;
        it never reads question wording, compares score gaps, invents semantics,
        or changes the selected candidate's textual S2SQL.

        When exactly one Scope contains an exact metric match, value-only and
        keyword/embedding-only candidates cannot answer the requested metric and
        are excluded. Zero or multiple exact-metric Scopes preserve the existing
        fail-closed clarification behavior.
        """

        exact_metric_dataset_ids = AnalyticsQueryService._exact_metric_dataset_ids(candidates)
        if len(exact_metric_dataset_ids) != 1:
            return candidates
        selected_dataset_id = next(iter(exact_metric_dataset_ids))
        return tuple(
            candidate for candidate in candidates if candidate.dataset_id == selected_dataset_id
        )

    @staticmethod
    def _exact_metric_dataset_ids(
        candidates: tuple[ParsedSemanticCandidate, ...],
    ) -> frozenset[str]:
        return frozenset(
            candidate.dataset_id
            for candidate in candidates
            if any(
                match.element_type is SemanticElementType.METRIC
                and match.method is MatchMethod.EXACT
                for match in candidate.mapping.matches
            )
        )

    @staticmethod
    def _clarification_scope_candidates(
        candidates: tuple[ParsedSemanticCandidate, ...],
    ) -> tuple[ParsedSemanticCandidate, ...]:
        """Restrict AMBIGUOUS_QUERY_SCOPE options to metric-bearing scopes.

        When two or more Scopes tie with exact governed-metric evidence, scopes
        whose only evidence is dimension values, dimensions or keyword/embedding
        metric hits cannot answer the requested metric and are not offered as
        clarification options. Explicit resume selections still address any
        discovered candidate; this narrows only what the clarification lists.
        Zero exact-metric Scopes keep every discovered scope on offer.
        """

        exact_metric_dataset_ids = AnalyticsQueryService._exact_metric_dataset_ids(candidates)
        if len(exact_metric_dataset_ids) < 2:
            return candidates
        return tuple(
            candidate
            for candidate in candidates
            if candidate.dataset_id in exact_metric_dataset_ids
        )

    @staticmethod
    def _supports_global_scope_routing(
        release: SemanticRelease,
        dataset_ids: tuple[str, ...],
    ) -> bool:
        """Opt new compiler-owned Releases into the reviewed router.

        Legacy Releases may contain Dataset resources without a frozen root
        Route. They retain the previous per-Dataset discovery path unchanged.
        """

        routed = {item.dataset_id for item in release.analysis_topic_routes}
        return bool(dataset_ids) and set(dataset_ids).issubset(routed)

    @staticmethod
    def _select_candidate(
        candidates: tuple[ParsedSemanticCandidate, ...],
        selected_dataset_id: str | None,
    ) -> ParsedSemanticCandidate:
        if selected_dataset_id is not None:
            try:
                return next(item for item in candidates if item.dataset_id == selected_dataset_id)
            except StopIteration as exc:
                raise SemanticParsingError(
                    "确认的候选已失效，请重新提问", code="CANDIDATE_NOT_FOUND"
                ) from exc
        # The parser sorts cross-dataset candidates and uses the first
        # selected parse when the caller did not explicitly choose one.
        return candidates[0]

    @staticmethod
    def _selection_context(
        *,
        request: QueryRequest,
        published: PublishedRelease,
        dataset_ids: tuple[str, ...],
        actor_id: str,
        semantic_now: datetime | None = None,
    ) -> _SelectionTokenContext:
        return _SelectionTokenContext(
            project_id=request.project_id,
            actor_id=actor_id,
            question=request.question,
            dataset_ids=tuple(sorted(dataset_ids)),
            release_id=published.release.id,
            spec_hash=published.release.spec_hash,
            index_snapshot_id=published.index_snapshot.id,
            conversation_id=request.conversation_id,
            semantic_now=(semantic_now.isoformat() if semantic_now is not None else None),
        )

    def _opaque_selection_ref(self, purpose: str, value: str) -> str:
        digest = hmac.new(
            self._selection_secret,
            f"{purpose}\0{value}".encode(),
            hashlib.sha256,
        ).digest()[:16]
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _selection_context_ref(self, context: _SelectionTokenContext) -> str:
        return self._opaque_selection_ref(
            "selection_context",
            content_hash(
                {
                    "project_id": context.project_id,
                    "actor_id": context.actor_id,
                    "question": context.question,
                    "dataset_ids": context.dataset_ids,
                    "release_id": context.release_id,
                    "spec_hash": context.spec_hash,
                    "index_snapshot_id": context.index_snapshot_id,
                    "conversation_id": context.conversation_id,
                    "semantic_now": context.semantic_now,
                }
            ),
        )

    def _selection_token(
        self,
        *,
        release: SemanticRelease,
        context: _SelectionTokenContext,
        dataset_id: str | None = None,
        semantic_selection_id: str | None = None,
        semantic_selection_ids: tuple[str, ...] = (),
    ) -> str:
        """Issue a short-lived, context-bound token only for a displayed option."""

        if semantic_selection_id is not None and semantic_selection_ids:
            raise ValueError("selection token accepts one semantic input form")
        selections = (
            (semantic_selection_id,)
            if semantic_selection_id is not None
            else tuple(dict.fromkeys(semantic_selection_ids))
        )
        if dataset_id is None and not selections:
            raise ValueError("selection token requires a scope or semantic choice")
        if dataset_id is not None and dataset_id not in context.dataset_ids:
            raise ValueError("selection scope must belong to the request context")
        if selections and not set(selections).issubset(self._semantic_selection_tokens(release)):
            raise ValueError("semantic selection must belong to the release")
        scope_ref = (
            self._opaque_selection_ref(
                "selection_scope",
                f"{release.spec_hash}\0{dataset_id}",
            )
            if dataset_id is not None
            else "-"
        )
        semantic_ref = (
            "~".join(
                self._opaque_selection_ref(
                    "selection_semantic",
                    f"{release.spec_hash}\0{selection_id}",
                )
                for selection_id in selections
            )
            or "-"
        )
        expires_at = format(int(time.time()) + self._selection_token_ttl_seconds, "x")
        unsigned = ".".join(
            (
                _SELECTION_TOKEN_VERSION,
                self._selection_context_ref(context),
                scope_ref,
                semantic_ref,
                expires_at,
            )
        )
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self._selection_secret,
                    unsigned.encode(),
                    hashlib.sha256,
                ).digest()[:16]
            )
            .decode()
            .rstrip("=")
        )
        return f"{unsigned}.{signature}"

    def _decode_selection_token(
        self,
        selected_id: str,
        *,
        release: SemanticRelease,
        context: _SelectionTokenContext,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Authenticate in O(1), then resolve only a valid opaque token."""

        parts = selected_id.split(".")
        if len(parts) != 6 or parts[0] != _SELECTION_TOKEN_VERSION:
            raise SemanticParsingError(
                "确认项格式无效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        unsigned = ".".join(parts[:5])
        expected_signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self._selection_secret,
                    unsigned.encode(),
                    hashlib.sha256,
                ).digest()[:16]
            )
            .decode()
            .rstrip("=")
        )
        if not hmac.compare_digest(parts[5], expected_signature):
            raise SemanticParsingError(
                "确认项已失效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        try:
            expires_at = int(parts[4], 16)
        except ValueError as exc:
            raise SemanticParsingError(
                "确认项格式无效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            ) from exc
        if expires_at < int(time.time()):
            raise SemanticParsingError(
                "确认项已过期，请重新提问",
                code="STALE_QUERY_SELECTION",
            )
        if not hmac.compare_digest(parts[1], self._selection_context_ref(context)):
            raise SemanticParsingError(
                "确认项不属于当前问题，请重新选择",
                code="CANDIDATE_NOT_FOUND",
            )
        scope_ref, semantic_ref = parts[2], parts[3]
        if scope_ref == "-" and semantic_ref == "-":
            raise SemanticParsingError(
                "确认项不包含可用选择，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        dataset_matches = (
            tuple(
                dataset_id
                for dataset_id in context.dataset_ids
                if hmac.compare_digest(
                    scope_ref,
                    self._opaque_selection_ref(
                        "selection_scope",
                        f"{release.spec_hash}\0{dataset_id}",
                    ),
                )
            )
            if scope_ref != "-"
            else ()
        )
        semantic_matches: list[str] = []
        semantic_refs = semantic_ref.split("~") if semantic_ref != "-" else []
        if len(semantic_refs) != len(set(semantic_refs)):
            raise SemanticParsingError(
                "确认项已失效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        for current_ref in semantic_refs:
            matches = tuple(
                selection_id
                for selection_id in self._semantic_selection_tokens(release)
                if hmac.compare_digest(
                    current_ref,
                    self._opaque_selection_ref(
                        "selection_semantic",
                        f"{release.spec_hash}\0{selection_id}",
                    ),
                )
            )
            if len(matches) != 1:
                raise SemanticParsingError(
                    "确认项已失效，请重新提问",
                    code="CANDIDATE_NOT_FOUND",
                )
            semantic_matches.append(matches[0])
        if scope_ref != "-" and len(dataset_matches) != 1:
            raise SemanticParsingError(
                "确认项已失效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        return (
            dataset_matches[0] if dataset_matches else None,
            tuple(semantic_matches),
        )

    # ── drilldown continuations ─────────────────────────────────────────

    def _drilldown_context_ref(
        self,
        *,
        project_id: str,
        actor_id: str,
        query_id: str,
        release: SemanticRelease,
        dataset_id: str,
    ) -> str:
        return self._opaque_selection_ref(
            "drilldown_context",
            content_hash(
                {
                    "project_id": project_id,
                    "actor_id": actor_id,
                    "query_id": query_id,
                    "release_id": release.id,
                    "spec_hash": release.spec_hash,
                    "dataset_id": dataset_id,
                }
            ),
        )

    # op 位 → (kind, action)：d=加维度，r=去维度，f=换过滤值，m=换指标，t=换时间窗。
    _DRILLDOWN_OPS = {
        "d": ("dimension", "add"),
        "r": ("dimension", "remove"),
        "f": ("dimension", "refilter"),
        "m": ("metric", "replace"),
        "t": ("time", "retime"),
    }
    # 时间窗是固定枚举集：窗口 id 即被签名的"元素"，decode 时按集合重枚举匹配。
    _DRILLDOWN_TIME_WINDOWS: dict[str, tuple[str, int | None]] = {
        "__time:7d": ("近 7 天", 7),
        "__time:30d": ("近 30 天", 30),
        "__time:90d": ("近 90 天", 90),
        "__time:all": ("不限时间", None),
    }
    _MAX_DRILLDOWN_REFILTERS = 6

    def _drilldown_token(
        self,
        *,
        release: SemanticRelease,
        context_ref: str,
        kind: str,
        op: str,
        element_id: str,
    ) -> str:
        element_ref = self._opaque_selection_ref(
            "drilldown_element",
            f"{release.spec_hash}\0{kind}\0{element_id}",
        )
        expires_at = format(int(time.time()) + self._selection_token_ttl_seconds, "x")
        unsigned = ".".join(
            (
                _DRILLDOWN_TOKEN_VERSION,
                context_ref,
                element_ref,
                op,
                expires_at,
            )
        )
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self._selection_secret,
                    unsigned.encode(),
                    hashlib.sha256,
                ).digest()[:16]
            )
            .decode()
            .rstrip("=")
        )
        return f"{unsigned}.{signature}"

    def _drilldown_options(
        self,
        *,
        release: SemanticRelease,
        query: SemanticQuery,
        project_id: str,
        query_id: str,
        actor_id: str | None,
    ) -> tuple[DrilldownOption, ...]:
        """Sign follow-up cuts for a completed aggregate answer.

        Candidates come only from the frozen Dataset membership (scope members
        are reachability-compiled at publish time); anything the compiled route
        still cannot serve fails closed later in the structured pipeline.  No
        actor context means no bindable audience, so no tokens are issued.
        """

        actor = str(actor_id or "").strip()
        if not actor or query.query_type is not SemanticQueryType.AGGREGATE:
            return ()
        dataset = next((item for item in release.datasets if item.id == query.dataset_id), None)
        if dataset is None:
            return ()
        dimension_names = {item.id: item.name for item in release.dimensions}
        metric_names = {item.id: item.name for item in release.metrics}
        context_ref = self._drilldown_context_ref(
            project_id=project_id,
            actor_id=actor,
            query_id=query_id,
            release=release,
            dataset_id=dataset.id,
        )
        options: list[DrilldownOption] = []

        def issue(kind: str, op: str, action: str, element_id: str, label: str) -> None:
            options.append(
                DrilldownOption(
                    token=self._drilldown_token(
                        release=release,
                        context_ref=context_ref,
                        kind=kind,
                        op=op,
                        element_id=element_id,
                    ),
                    kind=kind,
                    action=action,
                    label=label,
                )
            )

        used_dimensions = set(query.dimension_ids)
        added = 0
        for dimension_id in dataset.dimension_ids:
            if dimension_id in used_dimensions or dimension_id not in dimension_names:
                continue
            if added >= _MAX_DRILLDOWN_DIMENSIONS:
                break
            added += 1
            issue("dimension", "d", "add", dimension_id, dimension_names[dimension_id])
        # 已用维度可移除；否则下钻链只能变长。移除后必须仍有投影
        # （剩余维度或任一指标），否则不签发。
        for dimension_id in query.dimension_ids:
            if dimension_id not in dimension_names:
                continue
            if not query.metric_ids and len(query.dimension_ids) <= 1:
                break
            issue("dimension", "r", "remove", dimension_id, dimension_names[dimension_id])
        # 已有等值过滤的维度可换值（值由续跑请求携带，是业务值 literal 而非语义 ID）。
        refiltered: list[str] = []
        for item in query.filters:
            if item.operator not in (FilterOperator.EQ, FilterOperator.IN):
                continue
            if item.dimension_id in refiltered or item.dimension_id not in dimension_names:
                continue
            if len(refiltered) >= self._MAX_DRILLDOWN_REFILTERS:
                break
            refiltered.append(item.dimension_id)
            issue("dimension", "f", "refilter", item.dimension_id, dimension_names[item.dimension_id])
        # 受治理默认时间维存在时提供固定时间窗切换。
        if dataset.default_time_dimension_id in dimension_names:
            for window_id, (window_label, _days) in self._DRILLDOWN_TIME_WINDOWS.items():
                issue("time", "t", "retime", window_id, window_label)
        used_metrics = set(query.metric_ids)
        metric_count = 0
        for metric_id in dataset.metric_ids:
            if metric_id in used_metrics or metric_id not in metric_names:
                continue
            if metric_count >= _MAX_DRILLDOWN_METRICS:
                break
            metric_count += 1
            issue("metric", "m", "replace", metric_id, metric_names[metric_id])
        return tuple(options)

    def _decode_drilldown_token(
        self,
        token: str,
        *,
        release: SemanticRelease,
        project_id: str,
        actor_id: str,
        query_id: str,
        dataset_id: str,
    ) -> tuple[str, str]:
        """Authenticate in O(1), then resolve the member by re-enumeration."""

        parts = token.split(".")
        if len(parts) != 6 or parts[0] != _DRILLDOWN_TOKEN_VERSION:
            raise SemanticParsingError(
                "下钻选项格式无效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        unsigned = ".".join(parts[:5])
        expected_signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    self._selection_secret,
                    unsigned.encode(),
                    hashlib.sha256,
                ).digest()[:16]
            )
            .decode()
            .rstrip("=")
        )
        if not hmac.compare_digest(parts[5], expected_signature):
            raise SemanticParsingError(
                "下钻选项已失效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        try:
            expires_at = int(parts[4], 16)
        except ValueError as exc:
            raise SemanticParsingError(
                "下钻选项格式无效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            ) from exc
        if expires_at < int(time.time()):
            raise SemanticParsingError(
                "下钻选项已过期，请重新提问",
                code="STALE_QUERY_SELECTION",
            )
        expected_context = self._drilldown_context_ref(
            project_id=project_id,
            actor_id=actor_id,
            query_id=query_id,
            release=release,
            dataset_id=dataset_id,
        )
        if not hmac.compare_digest(parts[1], expected_context):
            raise SemanticParsingError(
                "下钻选项不属于当前查询，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        op = self._DRILLDOWN_OPS.get(parts[3])
        if op is None:
            raise SemanticParsingError(
                "下钻选项格式无效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        kind, action = op
        dataset = next((item for item in release.datasets if item.id == dataset_id), None)
        if kind == "time":
            members: tuple[str, ...] = tuple(self._DRILLDOWN_TIME_WINDOWS)
        elif dataset is None:
            members = ()
        else:
            members = dataset.dimension_ids if kind == "dimension" else dataset.metric_ids
        matches = tuple(
            element_id
            for element_id in members
            if hmac.compare_digest(
                parts[2],
                self._opaque_selection_ref(
                    "drilldown_element",
                    f"{release.spec_hash}\0{kind}\0{element_id}",
                ),
            )
        )
        if len(matches) != 1:
            raise SemanticParsingError(
                "下钻选项已失效，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        return action, matches[0]

    def query_drilldown(
        self,
        *,
        project_id: str,
        query_id: str,
        token: str,
        base_query: SemanticQuery,
        base_release_id: str,
        base_spec_hash: str,
        actor_id: str,
        value: str | None = None,
        now: datetime | None = None,
    ) -> QueryResponse:
        """Continue a completed answer by one signed drilldown option.

        The base semantics come from the persisted query artifact, never from
        the client.  The continuation always executes against the Active
        Release; if publishing moved it since the answer, the token fails
        closed instead of silently re-running on different semantics.
        """

        published = self._releases.get_active_release(project_id)
        release = published.release
        if release.id != base_release_id or release.spec_hash != base_spec_hash:
            raise SemanticParsingError(
                "语义模型已更新，下钻已失效，请重新提问",
                code="STALE_QUERY_SELECTION",
            )
        action, element_id = self._decode_drilldown_token(
            token,
            release=release,
            project_id=project_id,
            actor_id=actor_id,
            query_id=query_id,
            dataset_id=base_query.dataset_id,
        )
        if action == "refilter":
            if not value or not value.strip():
                raise SemanticParsingError(
                    "请选择要替换的过滤值",
                    code="DRILLDOWN_VALUE_REQUIRED",
                )
            new_query = _apply_refilter(base_query, element_id, value.strip())
        elif action == "retime":
            dataset = next(
                (item for item in release.datasets if item.id == base_query.dataset_id),
                None,
            )
            time_dimension_id = dataset.default_time_dimension_id if dataset else None
            if not time_dimension_id:
                raise SemanticParsingError(
                    "下钻选项已失效，请重新提问",
                    code="CANDIDATE_NOT_FOUND",
                )
            new_query = _apply_retime(
                base_query,
                time_dimension_id,
                self._DRILLDOWN_TIME_WINDOWS[element_id][1],
                now=now,
            )
        else:
            new_query = _apply_drilldown(base_query, action, element_id)
        request = StructuredQueryRequest(
            project_id=project_id,
            semantic_query=new_query,
        )
        return self.query_structured(request, now=now, actor_id=actor_id)

    @staticmethod
    def _semantic_selection_token(
        *,
        selected_element_id: str | None,
        selected_element_type: SemanticElementType | None,
        selected_time_dimension_id: str | None,
    ) -> str | None:
        """Rebuild the opaque semantic choice that must survive another prompt."""

        if selected_time_dimension_id is not None:
            return f"time:{selected_time_dimension_id}"
        if selected_element_id is None or selected_element_type is None:
            return None
        if selected_element_type is SemanticElementType.DIMENSION_VALUE:
            return f"value:{selected_element_id}"
        if selected_element_type in {
            SemanticElementType.METRIC,
            SemanticElementType.DIMENSION,
        }:
            return f"element:{selected_element_type.value}:{selected_element_id}"
        return None

    @staticmethod
    def _semantic_selection_tokens(release: SemanticRelease) -> tuple[str, ...]:
        return (
            *(f"element:metric:{metric.id}" for metric in release.metrics),
            *(f"element:dimension:{dimension.id}" for dimension in release.dimensions),
            *(f"value:{value.id}" for value in release.dimension_values if value.enabled),
            *(
                f"time:{dimension.id}"
                for dimension in release.dimensions
                if dimension.semantic_type == "time"
            ),
        )

    def _query_scope_options(
        self,
        release: SemanticRelease,
        candidates: tuple[ParsedSemanticCandidate, ...],
        *,
        selection_context: _SelectionTokenContext,
    ) -> tuple[ClarificationOption, ...]:
        """Expose at most one candidate per materially different query scope.

        Candidate ordering may rank parsers inside one scope, but it must not
        silently decide which fact grain the user meant.  The existing candidate
        id is the release-bound continuation token, so confirming a scope resumes
        the unchanged textual-S2SQL pipeline rather than introducing a new stage.
        """

        first_by_dataset: dict[str, ParsedSemanticCandidate] = {}
        for candidate in candidates:
            first_by_dataset.setdefault(candidate.dataset_id, candidate)
        return self._query_scope_options_for_dataset_ids(
            release,
            tuple(first_by_dataset),
            selection_context=selection_context,
        )

    def _bundle_semantic_scope_options(
        self,
        *,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        options: tuple[ClarificationOption, ...],
        selection_context: _SelectionTokenContext,
    ) -> tuple[ClarificationOption, ...]:
        """Make one human choice carry both semantic meaning and fact grain.

        A semantic choice that leaves several roots viable used to trigger a
        second ``analysis_object`` card.  The existing opaque token already has
        room for both fields, so issue one option per materially distinct root
        and let a single click resume FINAL_PARSING.
        """

        resolver = QueryScopeResolver.from_release(release)
        routes = {item.dataset_id: item for item in release.analysis_topic_routes}
        models = {item.id: item for item in release.models}
        bundled: list[ClarificationOption] = []
        for option in options:
            semantic_selection_id = (
                f"element:{option.element_type}:{option.element_id}"
                if option.element_type in {"metric", "dimension"} and option.element_id is not None
                else f"value:{option.element_id}"
                if option.element_type == "dimension_value" and option.element_id is not None
                else None
            )
            if semantic_selection_id is None:
                continue
            resolution = resolver.resolve(
                evidence.matches,
                allowed_dataset_ids=dataset_ids,
                selected_element_id=option.element_id,
                selected_element_type=option.element_type,
            )
            candidate_ids = (
                (resolution.selected_dataset_id,)
                if resolution.status is QueryScopeResolutionStatus.SELECTED
                and resolution.selected_dataset_id is not None
                else resolution.candidate_dataset_ids
                if resolution.status is QueryScopeResolutionStatus.CLARIFICATION
                else ()
            )
            if not candidate_ids:
                # The weak semantic itself remains a real user choice even when
                # current exact dimensions make every fact root incompatible.
                # Keep the semantic-only token so confirming it produces the
                # reviewed DIMENSION_NOT_REACHABLE refusal instead of silently
                # dropping the user's metric wording.
                bundled.append(option)
                continue
            roots = [
                routes[item].root_model_id
                for item in candidate_ids
                if item in routes and routes[item].root_model_id in models
            ]
            if len(roots) != len(candidate_ids) or len(roots) != len(set(roots)):
                continue
            for dataset_id in candidate_ids:
                model = models[routes[dataset_id].root_model_id]
                description = option.description
                if len(candidate_ids) > 1:
                    description = f"{description}；分析粒度：{model.name}" + (
                        f"（{model.description}）" if model.description else ""
                    )
                bundled.append(
                    option.model_copy(
                        update={
                            "candidate_id": self._selection_token(
                                release=release,
                                context=selection_context,
                                dataset_id=dataset_id,
                                semantic_selection_id=semantic_selection_id,
                            ),
                            "dataset_id": dataset_id,
                            "description": description,
                        }
                    )
                )
        return tuple(bundled)

    def _bundle_metric_phrase_groups(
        self,
        *,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        groups: tuple[tuple[str, tuple[ClarificationOption, ...]], ...],
        selection_context: _SelectionTokenContext,
    ) -> tuple[ClarificationOption, ...]:
        """Issue one opaque option for one choice in every weak metric phrase."""

        if not groups or any(not options for _detected, options in groups):
            return ()
        combination_count = 1
        for _detected, options in groups:
            combination_count *= len(options)
            if combination_count > _MAX_SEMANTIC_CONFIRMATION_OPTIONS:
                return ()
        resolver = QueryScopeResolver.from_release(release)
        routes = {item.dataset_id: item for item in release.analysis_topic_routes}
        models = {item.id: item for item in release.models}
        bundled: list[ClarificationOption] = []
        option_groups = tuple(options for _detected, options in groups)
        for choices in product(*option_groups):
            if any(choice.element_id is None for choice in choices):
                continue
            metric_ids = tuple(dict.fromkeys(choice.element_id for choice in choices))
            resolution = resolver.resolve(
                evidence.matches,
                allowed_dataset_ids=dataset_ids,
                human_confirmed_metric_ids=metric_ids,
            )
            candidate_ids = (
                (resolution.selected_dataset_id,)
                if resolution.status is QueryScopeResolutionStatus.SELECTED
                and resolution.selected_dataset_id is not None
                else resolution.candidate_dataset_ids
                if resolution.status is QueryScopeResolutionStatus.CLARIFICATION
                else ()
            )
            root_ids = tuple(
                routes[dataset_id].root_model_id
                for dataset_id in candidate_ids
                if dataset_id in routes
            )
            if (
                not candidate_ids
                or len(root_ids) != len(candidate_ids)
                or len(root_ids) != len(set(root_ids))
            ):
                continue
            semantic_ids = tuple(f"element:metric:{metric_id}" for metric_id in metric_ids)
            label = "；".join(
                f"{detected_text}：{choice.label}"
                for (detected_text, _options), choice in zip(groups, choices, strict=True)
            )
            description = "；".join(choice.description for choice in choices)
            for dataset_id in candidate_ids:
                root = models.get(routes[dataset_id].root_model_id)
                rendered_description = description
                if len(candidate_ids) > 1 and root is not None:
                    rendered_description += f"；分析粒度：{root.name}"
                bundled.append(
                    ClarificationOption(
                        candidate_id=self._selection_token(
                            release=release,
                            context=selection_context,
                            dataset_id=dataset_id,
                            semantic_selection_ids=semantic_ids,
                        ),
                        kind="metric",
                        label=label,
                        description=rendered_description,
                        dataset_id=dataset_id,
                        semantic_selection_ids=semantic_ids,
                    )
                )
        return tuple(bundled)

    def _semantic_options_for_ambiguous_scopes(
        self,
        *,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        selection_context: _SelectionTokenContext,
        question: str,
        now: datetime | None,
    ) -> tuple[str, tuple[ClarificationOption, ...]] | None:
        """Bundle an already-detected same-name semantic choice with its root.

        QueryScopeResolver can reach a business-object clarification before the
        selected Scope's ``same_name_ambiguity`` gate runs.  Returning only an
        object card in that order used to reveal the semantic ambiguity on the
        next turn.  This helper inspects deterministic projections of the same
        globally collected Mapper evidence and, for one detected phrase, emits
        tokens that bind both choices.  Multiple independent phrases cannot be
        represented by one confirmation and therefore produce a stable empty
        option set instead of a second card.
        """

        groups_by_phrase: dict[
            str,
            tuple[str, dict[tuple[SemanticElementType, str], SemanticAmbiguityMember]],
        ] = {}
        projections = self._card_mapping_projections(
            question=question,
            release=release,
            evidence=evidence,
            dataset_ids=dataset_ids,
            now=now,
        )
        for mapping in projections:
            for group in same_name_ambiguities(mapping, release):
                normalized = normalize_text(group.detected_text)
                if not normalized:
                    continue
                display_text, members = groups_by_phrase.setdefault(
                    normalized,
                    (group.detected_text, {}),
                )
                for member in group.members:
                    members[(member.element_type, member.element_id)] = member
                groups_by_phrase[normalized] = (display_text, members)
        if not groups_by_phrase:
            return None
        if len(groups_by_phrase) != 1:
            return "多个业务说法", ()

        detected_text, members = next(iter(groups_by_phrase.values()))
        typed_members = tuple(
            members[key]
            for key in sorted(
                members,
                key=lambda item: (item[0].value, item[1]),
            )
        )
        if len(typed_members) < 2:
            return None
        semantic_options = self._semantic_options(
            release,
            tuple(member.element_id for member in typed_members),
            selection_context=selection_context,
            require_time=False,
            typed_members=typed_members,
            allowed_dataset_ids=dataset_ids,
        )
        return (
            detected_text,
            self._bundle_semantic_scope_options(
                release=release,
                evidence=evidence,
                dataset_ids=dataset_ids,
                options=semantic_options,
                selection_context=selection_context,
            ),
        )

    def _card_mapping_projections(
        self,
        *,
        question: str,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        now: datetime | None,
    ) -> tuple[MappingResult, ...]:
        """Mirror selected-Scope mode admission independently for every root."""

        return tuple(
            self._orchestrator.project_admitted_scope_mapping(
                question=question,
                release=release,
                evidence=evidence,
                dataset_id=dataset_id,
                now=now,
            )
            for dataset_id in dataset_ids
        )

    def _query_scope_options_for_dataset_ids(
        self,
        release: SemanticRelease,
        dataset_ids: tuple[str, ...],
        *,
        selection_context: _SelectionTokenContext,
        carried_selection_id: str | None = None,
    ) -> tuple[ClarificationOption, ...]:
        models = {item.id: item for item in release.models}
        metrics = {item.id: item for item in release.metrics}
        datasets = {item.id: item for item in release.datasets}
        routes = {item.dataset_id: item for item in release.analysis_topic_routes}
        datasets_by_root: dict[str, list[str]] = {}
        for dataset_id in dataset_ids:
            route = routes.get(dataset_id)
            if route is None or route.root_model_id not in models:
                return ()
            datasets_by_root.setdefault(route.root_model_id, []).append(dataset_id)
        if any(len(root_datasets) != 1 for root_datasets in datasets_by_root.values()):
            return ()
        options: list[ClarificationOption] = []
        for root_model_id in sorted(datasets_by_root):
            root_datasets = datasets_by_root[root_model_id]
            # Two internal plans for the same business root are not meaningful
            # user choices. They require deterministic compiler convergence or
            # a modeling fix, never duplicate cards with hidden differences.
            dataset_id = root_datasets[0]
            model = models[root_model_id]
            dataset = datasets[dataset_id]
            metric_names = [metrics[item].name for item in dataset.metric_ids if item in metrics][
                :8
            ]
            description_parts = [model.description or f"按{model.name}业务记录分析"]
            if metric_names:
                description_parts.append(f"可分析指标：{'、'.join(metric_names)}")
            candidate_id = self._selection_token(
                release=release,
                context=selection_context,
                dataset_id=dataset_id,
                semantic_selection_id=carried_selection_id,
            )
            options.append(
                ClarificationOption(
                    candidate_id=candidate_id,
                    kind="analysis_object",
                    label=model.name,
                    description="；".join(description_parts),
                    dataset_id=dataset_id,
                )
            )
        return tuple(options)

    def _record_failure(
        self,
        request: QueryRequest,
        exc: AnalyticsError,
        *,
        published: PublishedRelease,
        effective_question: str,
        actor_id: str | None,
    ) -> None:
        """Keep the refused question. Never let the log break the response."""

        if self._query_failures is None:
            return
        try:
            self._query_failures.save_failure(
                QueryFailureRecord(
                    question=request.question,
                    effective_question=effective_question,
                    stage=_stage_or_precheck(exc.stage).value,
                    code=exc.code,
                    message=str(exc),
                    release_id=published.release.id,
                    spec_hash=published.release.spec_hash,
                    index_snapshot_id=published.index_snapshot.id,
                    dataset_ids=tuple(request.dataset_ids),
                    details=exc.details,
                ),
                actor_id=str(actor_id or "").strip(),
                project_id=request.project_id,
            )
        except Exception:
            # 记录失败是旁路：它本身出错不能把一次已经算好的拒答变成 500。
            LOGGER.exception("Failed to record refused query project_id=%s", request.project_id)

    def _record_element_selection(
        self,
        request: QueryRequest,
        *,
        published: PublishedRelease,
        effective_question: str,
        selected_element_id: str,
        selected_element_type: SemanticElementType | None,
        actor_id: str | None,
    ) -> None:
        """Keep the phrase→element pick the user just made; it is alias evidence.

        Whether it answered a clarification or switched an LLM decision, the user
        has told us what their wording means. The same list that feeds term
        mining shows it, so nobody has to re-discover it from logs.
        """

        if self._query_failures is None:
            return
        release = published.release
        elements = (
            release.metrics
            if selected_element_type is SemanticElementType.METRIC
            else release.dimensions
            if selected_element_type is SemanticElementType.DIMENSION
            else (*release.metrics, *release.dimensions)
        )
        element = next((item for item in elements if item.id == selected_element_id), None)
        if element is None:
            return
        try:
            self._query_failures.save_failure(
                QueryFailureRecord(
                    question=request.question,
                    effective_question=effective_question,
                    stage=QueryStage.FINAL_PARSING.value,
                    code="SEMANTIC_ELEMENT_SELECTED",
                    message=f"用户确认这句话里指的是「{element.name}」，可作为别名候选。",
                    release_id=release.id,
                    spec_hash=release.spec_hash,
                    index_snapshot_id=published.index_snapshot.id,
                    dataset_ids=tuple(request.dataset_ids),
                    details={"selected_element_id": selected_element_id},
                ),
                actor_id=str(actor_id or "").strip(),
                project_id=request.project_id,
            )
        except Exception:
            LOGGER.exception("Failed to record element selection project_id=%s", request.project_id)

    def _selection(
        self,
        request: QueryRequest,
        published: PublishedRelease,
        *,
        selection_context: _SelectionTokenContext,
    ) -> tuple[
        str | None,
        str | None,
        SemanticElementType | None,
        str | None,
        tuple[str, ...],
    ]:
        selected_id = request.selected_candidate_id
        if selected_id is None:
            return None, None, None, None, ()
        release = published.release
        if (
            request.expected_release_id != release.id
            or request.expected_spec_hash != release.spec_hash
            or request.expected_index_snapshot_id != published.index_snapshot.id
        ):
            raise SemanticParsingError(
                "确认项所属语义版本已变化，请重新提问",
                code="STALE_QUERY_SELECTION",
            )
        selected_dataset_id, semantic_selection_ids = self._decode_selection_token(
            selected_id,
            release=release,
            context=selection_context,
        )
        if not semantic_selection_ids:
            return selected_dataset_id, None, None, None, ()
        if len(semantic_selection_ids) > 1:
            parsed = tuple(
                self._parse_semantic_selection_id(selection_id)
                for selection_id in semantic_selection_ids
            )
            if all(
                element_id is not None
                and element_type is SemanticElementType.METRIC
                and time_id is None
                for element_id, element_type, time_id in parsed
            ):
                return (
                    selected_dataset_id,
                    None,
                    None,
                    None,
                    tuple(element_id for element_id, _type, _time in parsed if element_id),
                )
            raise SemanticParsingError(
                "组合确认项携带了不支持的语义类型，请重新提问",
                code="CANDIDATE_NOT_FOUND",
            )
        semantic_selection_id = semantic_selection_ids[0]
        element_id, element_type, time_id = self._parse_semantic_selection_id(semantic_selection_id)
        if element_id is not None or time_id is not None:
            return selected_dataset_id, element_id, element_type, time_id, ()
        raise SemanticParsingError(
            "确认项携带的语义选择已失效，请重新提问",
            code="CANDIDATE_NOT_FOUND",
        )

    @staticmethod
    def _parse_semantic_selection_id(
        semantic_selection_id: str | None,
    ) -> tuple[str | None, SemanticElementType | None, str | None]:
        if semantic_selection_id is None:
            return None, None, None
        if semantic_selection_id.startswith("element:"):
            payload = semantic_selection_id.removeprefix("element:")
            for element_type in (
                SemanticElementType.METRIC,
                SemanticElementType.DIMENSION,
            ):
                prefix = f"{element_type.value}:"
                if payload.startswith(prefix):
                    return payload.removeprefix(prefix), element_type, None
        if semantic_selection_id.startswith("value:"):
            return (
                semantic_selection_id.removeprefix("value:"),
                SemanticElementType.DIMENSION_VALUE,
                None,
            )
        if semantic_selection_id.startswith("time:"):
            return None, None, semantic_selection_id.removeprefix("time:")
        return None, None, None

    @staticmethod
    def _scope_datasets_to_selected_element(
        release: SemanticRelease,
        dataset_ids: tuple[str, ...],
        element_id: str,
        *,
        element_type: SemanticElementType | None,
        require_time: bool,
    ) -> tuple[str, ...]:
        dimensions = {item.id: item for item in release.dimensions}
        metrics = {item.id for item in release.metrics}
        dimension_values = {item.id: item for item in release.dimension_values if item.enabled}
        if require_time:
            dimension = dimensions.get(element_id)
            if dimension is None or dimension.semantic_type != "time":
                raise SemanticParsingError(
                    "确认的时间维度已失效",
                    code="SELECTED_ELEMENT_SCOPE_VIOLATION",
                )
        elif element_type is SemanticElementType.METRIC and element_id not in metrics:
            raise SemanticParsingError(
                "确认的指标已失效",
                code="SELECTED_ELEMENT_SCOPE_VIOLATION",
            )
        elif element_type is SemanticElementType.DIMENSION and element_id not in dimensions:
            raise SemanticParsingError(
                "确认的维度已失效",
                code="SELECTED_ELEMENT_SCOPE_VIOLATION",
            )
        elif (
            element_type is SemanticElementType.DIMENSION_VALUE
            and element_id not in dimension_values
        ):
            raise SemanticParsingError(
                "确认的语义对象已失效",
                code="SELECTED_ELEMENT_SCOPE_VIOLATION",
            )
        scoped_element_id = (
            dimension_values[element_id].dimension_id
            if element_type is SemanticElementType.DIMENSION_VALUE
            and element_id in dimension_values
            else element_id
        )
        datasets = {item.id: item for item in release.datasets}

        def contains_selected_element(dataset_id: str) -> bool:
            dataset = datasets[dataset_id]
            if element_type is SemanticElementType.METRIC:
                return scoped_element_id in dataset.metric_ids
            if (
                require_time
                or element_type is SemanticElementType.DIMENSION
                or element_type is SemanticElementType.DIMENSION_VALUE
            ):
                return scoped_element_id in dataset.dimension_ids
            # Compatibility tokens have already been rejected when a bare ID
            # exists in more than one family. Keep the historical union only
            # for the remaining uniquely typed legacy token.
            return scoped_element_id in {
                *dataset.metric_ids,
                *dataset.dimension_ids,
            }

        scoped = tuple(
            dataset_id for dataset_id in dataset_ids if contains_selected_element(dataset_id)
        )
        if not scoped:
            raise SemanticParsingError(
                "确认的语义对象不属于请求的数据集",
                code="SELECTED_ELEMENT_SCOPE_VIOLATION",
            )
        return scoped

    def _semantic_options(
        self,
        release: SemanticRelease,
        element_ids: tuple[str, ...],
        *,
        selection_context: _SelectionTokenContext,
        require_time: bool,
        preferred_dataset_id: str | None = None,
        typed_members: tuple[SemanticAmbiguityMember, ...] | None = None,
        allowed_dataset_ids: tuple[str, ...] | None = None,
    ) -> tuple[ClarificationOption, ...]:
        metrics = {item.id: item for item in release.metrics}
        dimensions = {item.id: item for item in release.dimensions}
        models = {item.id: item for item in release.models}
        values = {item.id: item for item in release.dimension_values if item.enabled}
        if typed_members is None:
            typed_elements = tuple(
                (element_type, element_id, element)
                for element_id in element_ids
                for element_type, index in (
                    (SemanticElementType.METRIC, metrics),
                    (SemanticElementType.DIMENSION, dimensions),
                )
                if (element := index.get(element_id)) is not None
            )
        else:
            typed_elements = tuple(
                (member.element_type, member.element_id, element)
                for member in typed_members
                if member.element_type
                in {SemanticElementType.METRIC, SemanticElementType.DIMENSION}
                and (
                    element := (
                        metrics.get(member.element_id)
                        if member.element_type is SemanticElementType.METRIC
                        else dimensions.get(member.element_id)
                    )
                )
                is not None
            )
        element_labels = [element.name for _type, _id, element in typed_elements]
        # Exact-match filtering intentionally retains every exact match for one
        # detected word. The safer HITL response
        # therefore has to expose governed provenance whenever those alternatives
        # share a display name; otherwise the human cannot make the decision that
        # the mapping stage deliberately deferred.
        duplicate_labels = {label for label, count in Counter(element_labels).items() if count > 1}
        datasets_by_id = {item.id: item for item in release.datasets}
        allowed = (
            set(datasets_by_id)
            if allowed_dataset_ids is None
            else set(allowed_dataset_ids).intersection(datasets_by_id)
        )
        dataset_by_element: dict[tuple[SemanticElementType, str], str] = {}
        if preferred_dataset_id is not None and preferred_dataset_id in allowed:
            preferred = next(item for item in release.datasets if item.id == preferred_dataset_id)
            for element_id in preferred.metric_ids:
                dataset_by_element[(SemanticElementType.METRIC, element_id)] = preferred.id
            for element_id in preferred.dimension_ids:
                dataset_by_element[(SemanticElementType.DIMENSION, element_id)] = preferred.id
        for dataset in release.datasets:
            if dataset.id not in allowed:
                continue
            for element_id in dataset.metric_ids:
                dataset_by_element.setdefault((SemanticElementType.METRIC, element_id), dataset.id)
            for element_id in dataset.dimension_ids:
                dataset_by_element.setdefault(
                    (SemanticElementType.DIMENSION, element_id), dataset.id
                )
        options = []
        for element_type, element_id, element in typed_elements:
            if (element_type, element_id) not in dataset_by_element:
                continue
            if require_time and (
                element_type is not SemanticElementType.DIMENSION
                or dimensions[element_id].semantic_type != "time"
            ):
                continue
            model = models.get(element.model_id)
            description_parts = [
                f"所属实体：{model.name if model is not None else element.model_id}",
                f"业务定义：{element.description or '暂无业务定义'}",
            ]
            if element.name in duplicate_labels:
                description_parts.append(
                    f"对象类型：{'指标' if element_type is SemanticElementType.METRIC else '维度'}"
                )
            semantic_selection_id = (
                f"time:{element_id}"
                if require_time
                else f"element:{element_type.value}:{element_id}"
            )
            options.append(
                ClarificationOption(
                    candidate_id=self._selection_token(
                        release=release,
                        context=selection_context,
                        dataset_id=preferred_dataset_id,
                        semantic_selection_id=semantic_selection_id,
                    ),
                    kind=element_type.value,
                    label=element.name,
                    description="；".join(description_parts),
                    dataset_id=dataset_by_element.get((element_type, element_id), ""),
                    element_type=element_type.value,
                    element_id=element_id,
                )
            )
        value_element_ids = (
            element_ids
            if typed_members is None
            else tuple(
                member.element_id
                for member in typed_members
                if member.element_type is SemanticElementType.DIMENSION_VALUE
            )
        )
        for element_id in value_element_ids:
            value = values.get(element_id)
            if value is None or require_time:
                continue
            dimension = dimensions.get(value.dimension_id)
            if dimension is None:
                continue
            if (SemanticElementType.DIMENSION, value.dimension_id) not in dataset_by_element:
                continue
            options.append(
                ClarificationOption(
                    candidate_id=self._selection_token(
                        release=release,
                        context=selection_context,
                        dataset_id=preferred_dataset_id,
                        semantic_selection_id=f"value:{element_id}",
                    ),
                    kind="dimension_value",
                    label=f"{dimension.name} = {value.display_name}",
                    description=f"业务维度：{dimension.name}",
                    dataset_id=dataset_by_element.get(
                        (SemanticElementType.DIMENSION, value.dimension_id), ""
                    ),
                    element_type="dimension_value",
                    element_id=element_id,
                )
            )
        return tuple(options)

    def _dry_run(
        self,
        *,
        physical: object,
        release: SemanticRelease,
        trace: list[QueryTraceStep],
    ) -> None:
        """Plan the physical query before running it.

        EXPLAIN rejects unknown columns and type errors without touching data,
        turning a class of translation defects into a pre-execution failure. The
        capability is optional: an executor without ``explain`` keeps the previous
        behaviour instead of failing closed on a missing feature.
        """

        if not self._dry_run_before_execute:
            return
        explain = getattr(self._executor, "explain", None)
        if explain is None:
            return
        trace.append(QueryTraceStep(stage=QueryStage.PHYSICAL_SQL_VALIDATING, status="started"))
        explain(query=physical, release=release)
        trace[-1] = QueryTraceStep(
            stage=QueryStage.PHYSICAL_SQL_VALIDATING,
            status="completed",
            detail={"guard": "explain_dry_run"},
        )

    @staticmethod
    def _interpretation(
        release: SemanticRelease,
        query: SemanticQuery,
        defaults: tuple[str, ...],
    ) -> QueryInterpretation:
        metrics = {item.id: item.name for item in release.metrics}
        dimensions = {item.id: item.name for item in release.dimensions}
        return QueryInterpretation(
            dataset_id=query.dataset_id,
            query_type=query.query_type,
            metrics=tuple(metrics[item] for item in query.metric_ids),
            dimensions=tuple(dimensions[item] for item in query.dimension_ids),
            filters=tuple(
                f"{dimensions[item.dimension_id]} "
                f"{_filter_operator_label(item.operator.value)} {item.value}"
                for item in query.filters
            )
            + tuple(
                f"{metrics[item.metric_id]} "
                f"{_filter_operator_label(item.operator.value)} {item.value}"
                for item in query.measure_filters
            )
            + tuple(
                f"{metrics[item.metric_id]}（聚合后） "
                f"{_filter_operator_label(item.operator.value)} {item.value}"
                for item in query.metric_filters
            ),
            applied_defaults=defaults,
        )

    @staticmethod
    def _visualization(
        release: SemanticRelease,
        query: SemanticQuery,
        s2sql: str = "",
    ) -> dict[str, object]:
        dimensions = {item.id: item for item in release.dimensions}
        # QueryType.DETAIL represents field selection rather than an
        # aggregate chart query (common/.../pojo/enums/QueryType.java).
        if query.query_type.value == "detail":
            chart = "table"
        elif not query.dimension_ids and "RATIO_TO_TOTAL(" in s2sql:
            # 组内占比且无分组：单个 0..1 比例值。RATIO_TO_TOTAL 是受治理
            # 保留函数名，corrected_s2sql 权威文本中出现即语义成立；输出列
            # 仍叫指标原名（「净金额」），下游无法从列名/数值可靠判定占比。
            chart = "ratio"
        elif any(dimensions[item].semantic_type == "time" for item in query.dimension_ids):
            chart = "line"
        elif query.metric_ids and query.dimension_ids:
            chart = "bar"
        else:
            chart = "table"
        units = {item.id: item.unit for item in release.metrics}
        return {
            "type": chart,
            "x": query.dimension_ids[0] if query.dimension_ids else None,
            "y": query.metric_ids,
            # 与 y 逐位对齐的展示单位（「元」「件」…），无单位为 None。
            "y_units": [units.get(metric_id) for metric_id in query.metric_ids],
        }

    @staticmethod
    def _failed_without_release(
        query_id: str,
        trace: list[QueryTraceStep],
        code: str,
        message: str,
        stage: str = "PRECHECK",
        diagnostics: QueryDiagnosis | None = None,
    ) -> FailedQueryResponse:
        return FailedQueryResponse(
            query_id=query_id,
            release_id="",
            spec_hash="",
            index_snapshot_id="",
            trace=tuple(trace),
            diagnostics=diagnostics,
            error=QueryError(stage=stage, code=code, message=message),
        )


def _apply_drilldown(base: SemanticQuery, action: str, element_id: str) -> SemanticQuery:
    """Derive the continuation query from the persisted base semantics.

    ``add`` splits by one more governed dimension and keeps every other
    constraint.  ``remove`` drops a grouping dimension: its ORDER BY reference
    goes with it, while value filters on it stay — "not grouped by region" and
    "only 华南" are independent statements.  ``replace`` switches the metric and
    drops anything that referenced the previous metrics (overrides,
    metric/measure filters, metric order columns) — carrying them over would
    either fail validation or quietly filter the new metric by the old one's
    values.
    """

    if action == "add":
        return base.model_copy(
            update={
                "dimension_ids": tuple(dict.fromkeys((*base.dimension_ids, element_id))),
            }
        )
    if action == "remove":
        remaining = tuple(item for item in base.dimension_ids if item != element_id)
        return base.model_copy(
            update={
                "dimension_ids": remaining,
                "order_by": tuple(
                    item for item in base.order_by if item.element_id != element_id
                ),
            }
        )
    kept_order = tuple(
        item
        for item in base.order_by
        if item.element_id in base.dimension_ids or item.element_id == element_id
    )
    return base.model_copy(
        update={
            "metric_ids": (element_id,),
            "aggregation_overrides": (),
            "measure_filters": (),
            "metric_filters": (),
            "order_by": kept_order,
        }
    )


def _apply_refilter(base: SemanticQuery, dimension_id: str, value: str) -> SemanticQuery:
    """Swap the equality filter on one governed dimension for a new value.

    Only that dimension's eq/in predicates are replaced; every other filter,
    grouping, and ordering stays.  The value is a caller-supplied business
    literal — an unknown value matches no rows, which is the safe outcome.
    """

    kept = tuple(
        item
        for item in base.filters
        if not (
            item.dimension_id == dimension_id
            and item.operator in (FilterOperator.EQ, FilterOperator.IN)
        )
    )
    return base.model_copy(
        update={
            "filters": (
                *kept,
                QueryFilter(
                    dimension_id=dimension_id,
                    operator=FilterOperator.EQ,
                    value=value,
                ),
            ),
        }
    )


def _apply_retime(
    base: SemanticQuery,
    time_dimension_id: str,
    days: int | None,
    *,
    now: datetime | None = None,
) -> SemanticQuery:
    """Replace filters on the governed default time dimension with one window.

    ``days=None`` means "all time": the window filters are simply dropped.
    The bound is an ISO date literal; the translator's ``render_time_bound``
    adapts it to the physical column type downstream.
    """

    kept = tuple(item for item in base.filters if item.dimension_id != time_dimension_id)
    if days is None:
        return base.model_copy(update={"filters": kept})
    today = (now or datetime.now(UTC)).date()
    bound = (today - timedelta(days=days)).isoformat()
    return base.model_copy(
        update={
            "filters": (
                *kept,
                QueryFilter(
                    dimension_id=time_dimension_id,
                    operator=FilterOperator.GTE,
                    value=bound,
                ),
            ),
        }
    )


def _failure_message(exc: AnalyticsError) -> str:
    """失败信息带上数据库的真实报错。

    「PostgreSQL query failed」对排障等于零:sqlstate 与 message_primary 早已
    被安全截取进 exc.details,却只在 include_diagnostics 的 trace 里——评测卡
    和普通失败响应都看不到,用户只知道挂了、不知道为什么(2026-08-26 实测)。
    """

    message = str(exc)
    database_message = exc.details.get("database_message")
    sqlstate = exc.details.get("sqlstate")
    if database_message:
        suffix = f" [{sqlstate}]" if sqlstate else ""
        return f"{message}: {database_message}{suffix}"
    return message


def _stage_or_precheck(value: str) -> QueryStage:
    try:
        return QueryStage(value)
    except ValueError:
        return QueryStage.PRECHECK


def _mark_failed(
    trace: list[QueryTraceStep],
    stage: QueryStage,
    detail: dict[str, object],
) -> None:
    """Close an open trace stage or append a failure at the true boundary."""

    failed = QueryTraceStep(stage=stage, status="failed", detail=detail)
    if trace and trace[-1].stage is stage and trace[-1].status == "started":
        trace[-1] = failed
    else:
        trace.append(failed)


def _filter_operator_label(value: str) -> str:
    return {
        "eq": "=",
        "ne": "≠",
        "gt": ">",
        "gte": "≥",
        "lt": "<",
        "lte": "≤",
        "in": "属于",
        "not_in": "不属于",
        "between": "介于",
        "like": "匹配",
        "is_null": "为空",
        "is_not_null": "不为空",
    }.get(value, value)


def _success_diagnosis(
    *,
    parser: str,
    llm_enabled: bool,
    audit_complete: bool = True,
) -> QueryDiagnosis:
    if not audit_complete:
        # The executed SQL is authoritative and correct; only the audit
        # projection behind `interpretation` cannot represent it. Saying so is
        # what keeps the interpretation trustworthy as a verification surface.
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.TRANSLATION,
            stage=QueryStage.TRANSLATING.value,
            severity="warning",
            summary="查询已按 SQL 正确执行，但语义解释无法完整表达其中的过滤条件",
            recommendation=(
                "OR 条件、子查询过滤等无法投影为结构化过滤项；"
                "请以 corrected_s2sql 为准核对口径，不要以语义解释判断是否过滤。"
            ),
            user_hint="结果正确，但「语义解释」面板无法完整展示这次用到的过滤条件。",
        )
    if parser == "rule" and llm_enabled:
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.RULE_FALLBACK,
            stage=QueryStage.FINAL_PARSING.value,
            severity="warning",
            summary="LLM 未形成可执行语义查询，系统使用了 Rule fallback",
            recommendation="检查结果是否遗漏排名、占比、嵌套分析等复杂意图；必要时保持拒答。",
            user_hint="这个结果是按基础规则得出的，可能遗漏了排名、占比等复杂要求，请核对后再用。",
        )
    return QueryDiagnosis(
        category=QueryDiagnosticCategory.SUCCESS,
        stage=QueryStage.FINISHED.value,
        severity="info",
        summary="问数完整链路执行成功",
        recommendation="核对语义解释和结果后可加入黄金问题。",
    )


def _shipped_diagnosis(
    diagnosis: QueryDiagnosis, *, include_diagnostics: bool
) -> QueryDiagnosis | None:
    """Warnings always ship; info-level diagnoses only when asked for."""

    if diagnosis.severity == "warning" or include_diagnostics:
        return diagnosis
    return None


def _error_diagnosis(exc: AnalyticsError) -> QueryDiagnosis:
    stage = _stage_or_precheck(exc.stage)
    if exc.code == "CROSS_FACT_METRICS_UNSUPPORTED":
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.ROUTING,
            stage=QueryStage.CANDIDATE_DISCOVERY.value,
            severity="error",
            summary="问题中的精确指标属于不同事实根",
            recommendation="拆成单事实问题，或先建立经过治理的联合模型/视图。",
            user_hint="这些指标不能安全地在一次查询中合并。请拆开提问，或先建立联合分析模型。",
        )
    if exc.code == "DIMENSION_NOT_REACHABLE":
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.ROUTING,
            stage=QueryStage.CANDIDATE_DISCOVERY.value,
            severity="error",
            summary="指标事实根无法安全到达请求的维度",
            recommendation="检查已发布的关系路径、基数和语义成员覆盖。",
            user_hint="这个指标不能按你指定的维度安全拆分，请换一个维度或调整语义模型。",
        )
    if exc.code == "QUERY_SCOPE_COMPILATION_STALE":
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.MODEL_VERSION,
            stage=QueryStage.CANDIDATE_DISCOVERY.value,
            severity="error",
            summary="当前发布版本的分析路径与语义目录不一致",
            recommendation="重新编译并发布语义模型，禁止运行时临时修补。",
            user_hint="语义模型的查询范围需要重新发布，请联系建模管理员。",
        )
    if stage is QueryStage.EXECUTING:
        return QueryDiagnosis(
            category=QueryDiagnosticCategory.DATABASE_EXECUTION,
            stage=stage.value,
            severity="error",
            summary="数据库拒绝执行生成的只读 SQL",
            recommendation="检查物理 SQL、参数、SQLSTATE 和数据库字段作用域。",
            user_hint="查询在数据库执行时失败。请换一种问法，或缩小时间范围后重试。",
        )
    if stage is QueryStage.PHYSICAL_SQL_VALIDATING:
        category = QueryDiagnosticCategory.SQL_GUARD
        summary = "物理 SQL 未通过只读安全检查"
        recommendation = "检查语句类型、表范围、注释和 LIMIT。"
        user_hint = "这个问题生成的查询超出了允许范围。请只针对已开放的指标和维度提问。"
    elif stage is QueryStage.TRANSLATING:
        category = (
            QueryDiagnosticCategory.ROUTING
            if exc.code.startswith(("MISSING_JOIN", "ANALYSIS_TOPIC", "CROSS_FACT"))
            else QueryDiagnosticCategory.TRANSLATION
        )
        summary = "语义 SQL 无法翻译为受治理物理 SQL"
        recommendation = "检查指标、维度、分析主题路径和表达式作用域。"
        user_hint = (
            "问题涉及的指标和维度之间没有可用的关联。请改为只问一个业务主题内的内容。"
            if category is QueryDiagnosticCategory.ROUTING
            else "问题里的计算方式目前不支持。请换更直接的问法，例如只问一个指标按一个维度拆分。"
        )
    elif stage is QueryStage.S2SQL_CORRECTING:
        category = QueryDiagnosticCategory.CORRECTION
        summary = "语义 SQL 未通过受治理修正与校验"
        recommendation = "检查解析结果、聚合兼容性、时间口径和受治理对象范围。"
        user_hint = "问题中的时间范围或统计方式不明确。请写明时间范围和想看的指标。"
    elif stage is QueryStage.FINAL_PARSING:
        category = QueryDiagnosticCategory.FINAL_PARSING
        summary = "模型或 Rule 未生成合法且完整的语义 SQL"
        recommendation = "检查最终候选、精确值约束、fallback 和 Corrector 输出。"
        user_hint = "没能理解这个问题。请换一种说法，或直接说出想看的指标和维度名称。"
    elif stage is QueryStage.CANDIDATE_DISCOVERY:
        category = QueryDiagnosticCategory.MAPPING
        summary = "问题没有映射到足够的受治理语义对象"
        recommendation = "检查名称、别名、术语、维度值和 Embedding 候选。"
        user_hint = "问题里没有识别出已定义的指标或维度。请用系统中的指标、维度名称重新提问。"
    elif stage is QueryStage.PRECHECK:
        category = QueryDiagnosticCategory.MODEL_VERSION
        summary = "请求与当前语义模型版本不一致"
        recommendation = "刷新页面并确认 Revision、Release 和索引版本。"
        user_hint = "语义模型刚刚更新了。请刷新页面后重新提问。"
    else:
        category = QueryDiagnosticCategory.INTERNAL
        summary = "问数链路发生未分类错误"
        recommendation = "复制诊断信息并根据 query_id 查询服务日志。"
        user_hint = "系统出了点问题。请稍后重试；若持续出现请联系管理员。"
    return QueryDiagnosis(
        category=category,
        stage=stage.value,
        severity="error",
        summary=summary,
        recommendation=recommendation,
        user_hint=user_hint,
    )
