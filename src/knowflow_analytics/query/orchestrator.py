from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from knowflow_analytics.contracts import SemanticQuery, SemanticRelease
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.query.contracts import (
    CandidateSet,
    CorrectedStructuredQuery,
    MapMode,
    MappingEvidence,
    MappingResult,
    ParsedSemanticCandidate,
    QueryOptions,
)
from knowflow_analytics.query.errors import (
    ClarificationSignal,
    MappingError,
    SemanticParsingError,
)
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import (
    GOVERNANCE_BLOCKING_S2SQL_CODES,
    LlmS2SqlParser,
    RuleS2SqlParser,
    StructuredQueryCorrector,
    TextualS2SqlCorrector,
)
from knowflow_analytics.semantic.index import SemanticElementType, SemanticIndexSnapshot


class CandidateOrchestrator:
    """Outer orchestration of the natural-language parsing stages."""

    def __init__(
        self,
        *,
        mapper: SemanticMapper,
        rule_parser: RuleS2SqlParser | None = None,
        llm_parser: LlmS2SqlParser | None = None,
        textual_corrector: TextualS2SqlCorrector | None = None,
        structured_corrector: StructuredQueryCorrector | None = None,
    ) -> None:
        self._mapper = mapper
        self._rule_parser = rule_parser or RuleS2SqlParser()
        self._llm_parser = llm_parser
        self._textual_corrector = textual_corrector or TextualS2SqlCorrector()
        self._structured_corrector = structured_corrector or StructuredQueryCorrector()

    @property
    def corrector_registry(self) -> tuple[str, ...]:
        return self._textual_corrector.registry

    @property
    def enabled_correctors(self) -> tuple[str, ...]:
        return self._textual_corrector.enabled_correctors

    @property
    def structured_parser_registry(self) -> tuple[str, ...]:
        return self._structured_corrector.registry

    @property
    def llm_enabled(self) -> bool:
        return self._llm_parser is not None

    def discover(
        self,
        *,
        question: str,
        release: SemanticRelease,
        index: SemanticIndexSnapshot,
        dataset_ids: tuple[str, ...],
        now: datetime | None = None,
        selected_element_id: str | None = None,
        selected_element_type: SemanticElementType | None = None,
        selected_time_dimension_id: str | None = None,
        tenant_id: str = "",
        allowed_element_ids: frozenset[str] | None = None,
        options: QueryOptions | None = None,
    ) -> CandidateSet:
        attempts: list[MappingResult] = []
        candidates: list[ParsedSemanticCandidate] = []
        clarifications: list[ClarificationSignal] = []
        for dataset_id in dataset_ids:
            dataset_candidates = []
            for mode in (MapMode.STRICT, MapMode.MODERATE):
                mapping = self._mapper.map(
                    question=question,
                    dataset_id=dataset_id,
                    index=index,
                    mode=mode,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    tenant_id=tenant_id,
                    allowed_element_ids=allowed_element_ids,
                )
                attempts.append(mapping)
                candidate, effective_mapping, clarification = self._parse_mapping(
                    question=question,
                    release=release,
                    mapping=mapping,
                    now=now,
                    selected_time_dimension_id=selected_time_dimension_id,
                    options=options,
                )
                attempts[-1] = effective_mapping
                if clarification is not None:
                    clarifications.append(clarification)
                if candidate is not None:
                    dataset_candidates.append(candidate)
            # Match NL2SQLParser exactly: LOOSE is attempted for the current
            # dataset only while no earlier dataset has produced a candidate.
            # Once one dataset is viable, later weak matches must not compete
            # with it merely because embedding recall is broad.
            if not dataset_candidates and not candidates:
                mapping = self._mapper.map(
                    question=question,
                    dataset_id=dataset_id,
                    index=index,
                    mode=MapMode.LOOSE,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    tenant_id=tenant_id,
                )
                attempts.append(mapping)
                candidate, effective_mapping, clarification = self._parse_mapping(
                    question=question,
                    release=release,
                    mapping=mapping,
                    now=now,
                    selected_time_dimension_id=selected_time_dimension_id,
                    options=options,
                )
                attempts[-1] = effective_mapping
                if clarification is not None:
                    clarifications.append(clarification)
                if candidate is not None:
                    dataset_candidates.append(candidate)
            if dataset_candidates:
                candidates.append(self._top_candidate(dataset_candidates))
        unique: dict[tuple[str, str], ParsedSemanticCandidate] = {}
        for candidate in candidates:
            key = (candidate.dataset_id, candidate.parsed_s2sql)
            previous = unique.get(key)
            if previous is None or candidate.score > previous.score:
                unique[key] = candidate
        ordered = tuple(sorted(unique.values(), key=_cross_dataset_sort_key))
        if not ordered:
            if clarifications:
                first = clarifications[0]
                raise ClarificationSignal(
                    code=first.code,
                    message=first.message,
                    element_ids=first.element_ids,
                    degraded_reasons=tuple(
                        dict.fromkeys(
                            reason for attempt in attempts for reason in attempt.degraded_reasons
                        )
                    ),
                )
            raise MappingError(
                "没有在已发布语义模型中找到可回答该问题的指标或维度",
                code="NO_SEMANTIC_MAPPING",
                details={
                    "mapping_attempts": [attempt.model_dump(mode="json") for attempt in attempts]
                },
            )
        return CandidateSet(candidates=ordered, mapping_attempts=tuple(attempts))

    def collect_evidence(
        self,
        *,
        question: str,
        dataset_ids: tuple[str, ...],
        index: SemanticIndexSnapshot,
        tenant_id: str = "",
        allowed_element_ids: frozenset[str] | None = None,
        include_embeddings: bool = True,
    ) -> MappingEvidence:
        """Run the request's expensive semantic retrieval once across all Scopes.

        ``include_embeddings=False`` 只做词表精确/关键词匹配，不调向量模型——给只需要
        精确证据的确定性判定用（多轮改写的门）。
        """

        return self._mapper.collect_evidence(
            question=question,
            dataset_ids=dataset_ids,
            index=index,
            include_embeddings=include_embeddings,
            tenant_id=tenant_id,
            allowed_element_ids=allowed_element_ids,
        )

    def project_scope_evidence(
        self,
        *,
        evidence: MappingEvidence,
        dataset_ids: tuple[str, ...],
        mode: MapMode,
    ) -> tuple[MappingResult, ...]:
        """Expose deterministic Mapper views without invoking any SQL parser.

        CANDIDATE_DISCOVERY uses these already-governed per-Scope projections
        only to build human confirmation options.  The method does not rank
        Scopes, compare score gaps or create a semantic query.
        """

        return tuple(
            self._mapper.project_evidence(
                evidence=evidence,
                dataset_id=dataset_id,
                mode=mode,
            )
            for dataset_id in dataset_ids
        )

    def project_admitted_scope_mapping(
        self,
        *,
        question: str,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_id: str,
        now: datetime | None = None,
    ) -> MappingResult:
        """Return the exact MappingResult the Rule discovery path would admit.

        This is the read-only admission half of ``discover_selected_scope``:
        STRICT and MODERATE both pass through the same Rule parser and score
        ordering; LOOSE is attempted only when neither produced a candidate.
        When no candidate exists, the mapping behind the first real
        clarification is returned. No LLM, semantic translation, or execution
        is invoked.
        """

        attempts: list[MappingResult] = []
        candidates: list[ParsedSemanticCandidate] = []
        clarifications: list[tuple[ClarificationSignal, MappingResult]] = []
        for mode in (MapMode.STRICT, MapMode.MODERATE):
            mapping = self._mapper.project_evidence(
                evidence=evidence,
                dataset_id=dataset_id,
                mode=mode,
            )
            candidate, effective, clarification = self._parse_mapping(
                question=question,
                release=release,
                mapping=mapping,
                now=now,
                selected_time_dimension_id=None,
            )
            attempts.append(effective)
            if candidate is not None:
                candidates.append(candidate)
            if clarification is not None:
                clarifications.append((clarification, effective))
        if not candidates:
            mapping = self._mapper.project_evidence(
                evidence=evidence,
                dataset_id=dataset_id,
                mode=MapMode.LOOSE,
            )
            candidate, effective, clarification = self._parse_mapping(
                question=question,
                release=release,
                mapping=mapping,
                now=now,
                selected_time_dimension_id=None,
            )
            attempts.append(effective)
            if candidate is not None:
                candidates.append(candidate)
            if clarification is not None:
                clarifications.append((clarification, effective))
        if candidates:
            return self._top_candidate(candidates).mapping
        if clarifications:
            return clarifications[0][1]
        return attempts[-1]

    def discover_selected_scope(
        self,
        *,
        question: str,
        release: SemanticRelease,
        evidence: MappingEvidence,
        dataset_id: str,
        now: datetime | None = None,
        selected_element_id: str | None = None,
        selected_element_type: SemanticElementType | None = None,
        selected_time_dimension_id: str | None = None,
        options: QueryOptions | None = None,
    ) -> CandidateSet:
        """Preserve Rule discovery modes after deterministic Scope selection.

        STRICT/MODERATE/LOOSE remain distinct governed MappingResult views, but
        each is projected from the same scope-neutral retrieval evidence.
        """

        attempts: list[MappingResult] = []
        candidates: list[ParsedSemanticCandidate] = []
        clarifications: list[ClarificationSignal] = []
        for mode in (MapMode.STRICT, MapMode.MODERATE):
            mapping = self._mapper.project_evidence(
                evidence=evidence,
                dataset_id=dataset_id,
                mode=mode,
                selected_element_id=selected_element_id,
                selected_element_type=selected_element_type,
            )
            attempts.append(mapping)
            candidate, effective_mapping, clarification = self._parse_mapping(
                question=question,
                release=release,
                mapping=mapping,
                now=now,
                selected_time_dimension_id=selected_time_dimension_id,
            )
            attempts[-1] = effective_mapping
            if clarification is not None:
                clarifications.append(clarification)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            mapping = self._mapper.project_evidence(
                evidence=evidence,
                dataset_id=dataset_id,
                mode=MapMode.LOOSE,
                selected_element_id=selected_element_id,
                selected_element_type=selected_element_type,
            )
            attempts.append(mapping)
            candidate, effective_mapping, clarification = self._parse_mapping(
                question=question,
                release=release,
                mapping=mapping,
                now=now,
                selected_time_dimension_id=selected_time_dimension_id,
            )
            attempts[-1] = effective_mapping
            if clarification is not None:
                clarifications.append(clarification)
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            if clarifications:
                first = clarifications[0]
                raise ClarificationSignal(
                    code=first.code,
                    message=first.message,
                    element_ids=first.element_ids,
                    degraded_reasons=tuple(
                        dict.fromkeys(
                            reason for attempt in attempts for reason in attempt.degraded_reasons
                        )
                    ),
                )
            raise MappingError(
                "没有在已发布语义模型中找到可回答该问题的指标或维度",
                code="NO_SEMANTIC_MAPPING",
                details={
                    "mapping_attempts": [attempt.model_dump(mode="json") for attempt in attempts]
                },
            )
        return CandidateSet(
            candidates=(self._top_candidate(candidates),),
            mapping_attempts=tuple(attempts),
        )

    def _parse_mapping(
        self,
        *,
        question: str,
        release: SemanticRelease,
        mapping: MappingResult,
        now: datetime | None,
        selected_time_dimension_id: str | None,
        options: QueryOptions | None = None,
    ) -> tuple[
        ParsedSemanticCandidate | None,
        MappingResult,
        ClarificationSignal | None,
    ]:
        # Parity source: chat/server/parser/NL2SQLParser.java performs candidate
        # discovery with the rule parser only. Same-surface mapper alternatives stay
        # in that rule candidate for the final LLM parse; discovery never calls LLM.
        effective = mapping
        try:
            return (
                self._rule_parser.parse(
                    question=question,
                    release=release,
                    mapping=effective,
                    now=now,
                    selected_time_dimension_id=selected_time_dimension_id,
                    time_override=options.default_time_window if options is not None else None,
                ),
                effective,
                None,
            )
        except ClarificationSignal as signal:
            return None, effective, signal

    def final_parse(
        self,
        *,
        question: str,
        query_id: str,
        release: SemanticRelease,
        index: SemanticIndexSnapshot,
        selected: ParsedSemanticCandidate,
        now: datetime | None = None,
        selected_element_id: str | None = None,
        selected_element_type: SemanticElementType | None = None,
        selected_time_dimension_id: str | None = None,
        candidate_validator: Callable[[ParsedSemanticCandidate], None] | None = None,
        diagnostic_sink: Callable[[str, dict[str, object]], None] | None = None,
        tenant_id: str = "",
        mapping_evidence: MappingEvidence | None = None,
        allowed_element_ids: frozenset[str] | None = None,
        options: QueryOptions | None = None,
    ) -> ParsedSemanticCandidate:
        def emit(event: str, detail: dict[str, object]) -> None:
            if diagnostic_sink is not None:
                diagnostic_sink(event, detail)

        errors: list[AnalyticsError] = []
        final_mapping = (
            self._mapper.project_evidence(
                evidence=mapping_evidence,
                dataset_id=selected.dataset_id,
                mode=selected.mapping.mode,
                selected_element_id=selected_element_id,
                selected_element_type=selected_element_type,
                final_stage=True,
            )
            if mapping_evidence is not None
            else self._mapper.map(
                question=question,
                dataset_id=selected.dataset_id,
                index=index,
                mode=selected.mapping.mode,
                selected_element_id=selected_element_id,
                selected_element_type=selected_element_type,
                final_stage=True,
                tenant_id=tenant_id,
            )
        )
        emit("final_mapping", final_mapping.model_dump(mode="json"))
        selected = selected.model_copy(update={"mapping": final_mapping})
        rule_fallback_allowed = self._llm_parser is None
        if self._llm_parser is not None:
            try:
                llm_candidate = self._llm_parser.parse(
                    question=question,
                    release=release,
                    mapping=selected.mapping,
                    query_id=query_id,
                    now=now,
                    tenant_id=tenant_id,
                    visible_element_ids=allowed_element_ids,
                    options=options,
                )
            except AnalyticsError as exc:
                emit(
                    "llm_parse_failed",
                    {"code": exc.code, "stage": exc.stage, "message": str(exc)},
                )
                if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                    raise
                # The LLM parser runs before the rule parser.  When the
                # LLM parser adds no candidate, RuleSqlParser may still take over.
                errors.append(exc)
                rule_fallback_allowed = True
            else:
                emit("llm_candidate", llm_candidate.model_dump(mode="json"))
                candidate_under_review = llm_candidate
                try:
                    corrected = self._textual_corrector.correct(
                        tenant_id=tenant_id,
                        candidate=llm_candidate,
                        question=question,
                        query_id=query_id,
                        release=release,
                        now=now,
                        selected_time_dimension_id=selected_time_dimension_id,
                        options=options,
                    )
                    candidate_under_review = corrected
                    if candidate_validator is not None:
                        candidate_validator(corrected)
                    emit("selected_candidate", corrected.model_dump(mode="json"))
                    return corrected
                except AnalyticsError as exc:
                    emit(
                        "llm_candidate_rejected",
                        {
                            "code": exc.code,
                            "stage": exc.stage,
                            "message": str(exc),
                            "candidate": candidate_under_review.model_dump(mode="json"),
                        },
                    )
                    if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                        raise
                    # Once LLMSqlParser has produced a candidate, upstream
                    # RuleSqlParser observes a non-empty candidate list and exits.
                    # A later Corrector failure must go to the ALL retry instead
                    # of resurrecting the discovery-stage Rule candidate.
                    errors.append(exc)
                    rule_fallback_allowed = False
        if rule_fallback_allowed:
            emit("rule_fallback_candidate", selected.model_dump(mode="json"))
            try:
                corrected = self._textual_corrector.correct(
                    tenant_id=tenant_id,
                    candidate=selected,
                    question=question,
                    query_id=query_id,
                    release=release,
                    now=now,
                    selected_time_dimension_id=selected_time_dimension_id,
                    options=options,
                )
                if candidate_validator is not None:
                    candidate_validator(corrected)
                emit("selected_candidate", corrected.model_dump(mode="json"))
                return corrected
            except AnalyticsError as exc:
                emit(
                    "rule_fallback_rejected",
                    {"code": exc.code, "stage": exc.stage, "message": str(exc)},
                )
                if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                    raise
                errors.append(exc)
        if self._llm_parser is not None:
            all_mapping = (
                self._mapper.project_evidence(
                    evidence=mapping_evidence,
                    dataset_id=selected.dataset_id,
                    mode=MapMode.ALL,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    final_stage=True,
                )
                if mapping_evidence is not None
                else self._mapper.map(
                    question=question,
                    dataset_id=selected.dataset_id,
                    index=index,
                    mode=MapMode.ALL,
                    selected_element_id=selected_element_id,
                    selected_element_type=selected_element_type,
                    final_stage=True,
                    tenant_id=tenant_id,
                )
            )
            emit("all_mapping", all_mapping.model_dump(mode="json"))
            all_rule_fallback_allowed = False
            try:
                all_candidate = self._llm_parser.parse(
                    question=question,
                    release=release,
                    mapping=all_mapping,
                    query_id=query_id,
                    now=now,
                    tenant_id=tenant_id,
                    visible_element_ids=allowed_element_ids,
                    options=options,
                )
            except AnalyticsError as exc:
                emit(
                    "all_llm_parse_failed",
                    {"code": exc.code, "stage": exc.stage, "message": str(exc)},
                )
                if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                    raise
                errors.append(exc)
                # The ALL retry keeps Text2SQLType.LLM_OR_RULE upstream.  A
                # failed LLMSqlParser therefore leaves the fresh ALL-pass
                # candidate list empty and allows RuleSqlParser to run.
                all_rule_fallback_allowed = True
            else:
                emit("all_candidate", all_candidate.model_dump(mode="json"))
                try:
                    corrected = self._textual_corrector.correct(
                        tenant_id=tenant_id,
                        candidate=all_candidate,
                        question=question,
                        query_id=query_id,
                        release=release,
                        now=now,
                        selected_time_dimension_id=selected_time_dimension_id,
                        options=options,
                    )
                    if candidate_validator is not None:
                        candidate_validator(corrected)
                    emit("selected_candidate", corrected.model_dump(mode="json"))
                    return corrected
                except AnalyticsError as exc:
                    emit(
                        "all_candidate_rejected",
                        {"code": exc.code, "stage": exc.stage, "message": str(exc)},
                    )
                    if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                        raise
                    # A candidate already exists in this ALL pass, so the
                    # immediately following RuleSqlParser must still exit.
                    errors.append(exc)
            if all_rule_fallback_allowed:
                try:
                    all_rule_candidate = self._rule_parser.parse(
                        question=question,
                        release=release,
                        mapping=all_mapping,
                        now=now,
                        selected_time_dimension_id=selected_time_dimension_id,
                        time_override=(
                            options.default_time_window if options is not None else None
                        ),
                    )
                    if all_rule_candidate is None:
                        emit(
                            "all_rule_fallback_empty",
                            {
                                "dataset_id": all_mapping.dataset_id,
                                "map_mode": all_mapping.mode.value,
                            },
                        )
                    else:
                        emit(
                            "all_rule_fallback_candidate",
                            all_rule_candidate.model_dump(mode="json"),
                        )
                        corrected = self._textual_corrector.correct(
                            tenant_id=tenant_id,
                            candidate=all_rule_candidate,
                            question=question,
                            query_id=query_id,
                            release=release,
                            now=now,
                            selected_time_dimension_id=selected_time_dimension_id,
                        )
                        if candidate_validator is not None:
                            candidate_validator(corrected)
                        emit("selected_candidate", corrected.model_dump(mode="json"))
                        return corrected
                except AnalyticsError as exc:
                    emit(
                        "all_rule_fallback_rejected",
                        {"code": exc.code, "stage": exc.stage, "message": str(exc)},
                    )
                    if exc.code in GOVERNANCE_BLOCKING_S2SQL_CODES:
                        raise
                    errors.append(exc)
        detail = errors[-1] if errors else None
        raise SemanticParsingError(
            "规则与模型均无法生成合法的语义查询",
            code=detail.code if detail is not None else "NO_VALID_S2SQL",
        )

    def correct_structured(
        self,
        *,
        query: SemanticQuery,
        release: SemanticRelease,
        now: datetime | None = None,
        options: QueryOptions | None = None,
    ) -> CorrectedStructuredQuery:
        """Validate one structured semantic query."""

        return self._structured_corrector.correct(
            query=query,
            release=release,
            now=now,
            time_override=options.default_time_window if options is not None else None,
        )

    @staticmethod
    def _top_candidate(
        candidates: list[ParsedSemanticCandidate],
    ) -> ParsedSemanticCandidate:
        unique: dict[str, ParsedSemanticCandidate] = {}
        for candidate in candidates:
            previous = unique.get(candidate.parsed_s2sql)
            if previous is None or candidate.score > previous.score:
                unique[candidate.parsed_s2sql] = candidate
        return min(unique.values(), key=lambda item: (-item.score, item.id))


def _cross_dataset_sort_key(candidate: ParsedSemanticCandidate) -> tuple[object, ...]:
    """Port ``SemanticParseInfo.SemanticParseComparator`` for dataset ranking.

    Rule query-mode ``score`` orders alternative parses inside one dataset. The
    outer comparator deliberately ranks dataset candidates using mapping
    evidence instead: dataset similarity, exact-match count, metric similarity and
    total similarity. The pinned Java comparator computes ``maxMetricUseCnt`` but
    omits it from ``DataSetMatchResult.builder()``, so its effective value is always
    zero. Python deliberately does not activate that dead upstream signal; stable
    semantic IDs only make the otherwise invalid equal comparator result repeatable.
    """

    matches = candidate.mapping.matches
    max_dataset_similarity = max(
        (item.score for item in matches if item.element_type is SemanticElementType.DATASET),
        default=0.0,
    )
    exact_match_count = sum(item.score == 1.0 for item in matches)
    max_metric_similarity = max(
        (item.score for item in matches if item.element_type is SemanticElementType.METRIC),
        default=0.0,
    )
    total_similarity = sum(item.score for item in matches)
    return (
        -max_dataset_similarity,
        -exact_match_count,
        -max_metric_similarity,
        -total_similarity,
        candidate.dataset_id,
        candidate.id,
    )
