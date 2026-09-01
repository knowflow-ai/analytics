from __future__ import annotations

from contextlib import suppress
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from knowflow_analytics.contracts import (
    FrozenModel,
    QueryResult,
    SemanticQuery,
    SemanticQueryType,
)
from knowflow_analytics.semantic.index import SemanticElementType


class MapMode(StrEnum):
    STRICT = "strict"
    MODERATE = "moderate"
    LOOSE = "loose"
    ALL = "all"


class MatchMethod(StrEnum):
    EXACT = "exact"
    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    TERM = "term_description"
    ALL_FIELD = "all_field"


class MappingEvidenceChannel(StrEnum):
    """Scope-neutral source of one raw Mapper evidence item.

    Retrieval happens once across the allowed QueryScope union.  The channel is
    retained so a later projection can apply the existing mapper order and
    thresholds without re-querying lexical or embedding sources.
    """

    DICTIONARY = "dictionary"
    DATABASE = "database"
    EMBEDDING = "embedding"
    TERM_DICTIONARY = "term_dictionary"
    TERM_DATABASE = "term_database"
    TERM_EMBEDDING = "term_embedding"
    MANIFEST = "manifest"


class MemoryStatus(StrEnum):
    """Lifecycle for reviewed Text2SQL exemplars."""

    PENDING = "PENDING"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"


class MemoryReviewResult(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class QueryStage(StrEnum):
    """Observable query stages without changing pipeline decisions.

    PRECHECK, ROUTE_BINDING, guard, execution, and post-processing are
    observability sub-stages around the governed workflow; they never select or
    repair semantic intent.
    """

    PRECHECK = "PRECHECK"
    CANDIDATE_DISCOVERY = "CANDIDATE_DISCOVERY"
    FINAL_PARSING = "FINAL_PARSING"
    S2SQL_CORRECTING = "S2SQL_CORRECTING"
    ROUTE_BINDING = "ROUTE_BINDING"
    TRANSLATING = "TRANSLATING"
    PHYSICAL_SQL_CORRECTING = "PHYSICAL_SQL_CORRECTING"
    PHYSICAL_SQL_VALIDATING = "PHYSICAL_SQL_VALIDATING"
    EXECUTING = "EXECUTING"
    POST_PROCESSING = "POST_PROCESSING"
    FINISHED = "FINISHED"


class QueryState(StrEnum):
    COMPLETED = "COMPLETED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    FAILED = "FAILED"


class QueryDiagnosticCategory(StrEnum):
    SUCCESS = "success"
    MODEL_VERSION = "model_version"
    MAPPING = "mapping"
    AMBIGUITY = "ambiguity"
    FINAL_PARSING = "final_parsing"
    RULE_FALLBACK = "rule_fallback"
    CORRECTION = "correction"
    TRANSLATION = "translation"
    ROUTING = "routing"
    SQL_GUARD = "sql_guard"
    DATABASE_EXECUTION = "database_execution"
    INTERNAL = "internal"


class MappingConfig(FrozenModel):
    """Governed mapper thresholds; changing one changes recall."""

    version: str = "knowflow-mapping-v1"
    detection_size: int = Field(default=8, ge=1)
    detection_max_size: int = Field(default=20, ge=1)
    dimension_value_size: int = Field(default=1, ge=0)
    name_similarity: float = Field(default=0.30, ge=0.0, le=1.0)
    name_min_similarity: float = Field(default=0.25, ge=0.0, le=1.0)
    value_similarity: float = Field(default=0.50, ge=0.0, le=1.0)
    value_min_similarity: float = Field(default=0.30, ge=0.0, le=1.0)
    embedding_similarity: float = Field(default=0.90, ge=-1.0, le=1.0)


class SchemaMatch(FrozenModel):
    entry_id: str
    dataset_id: str
    element_type: SemanticElementType
    element_id: str
    phrase: str
    detected_text: str
    method: MatchMethod
    score: float = Field(ge=0.0, le=1.0)
    priority: int
    dimension_id: str | None = None
    raw_value: Any = None
    # Half-open offsets in the Mapper input that produced ``detected_text``.
    # One normalized phrase may occur more than once; retaining every span is
    # required for longest-hit filtering to distinguish an independent mention
    # from a fragment nested inside a longer mention.
    detected_spans: tuple[tuple[int, int], ...] = ()
    detected_span_source: str = "question"


class MappingEvidenceMatch(FrozenModel):
    """One unprojected semantic match and the Scopes allowed to consume it."""

    entry_id: str
    eligible_dataset_ids: tuple[str, ...]
    element_type: SemanticElementType
    element_id: str
    phrase: str
    normalized_phrase: str
    detected_text: str
    method: MatchMethod
    score: float = Field(ge=-1.0, le=1.0)
    priority: int
    channel: MappingEvidenceChannel
    entry_source: str
    description: str = ""
    dimension_id: str | None = None
    raw_value: Any = None
    origin_term_entry_id: str | None = None
    detected_spans: tuple[tuple[int, int], ...] = ()
    detected_span_source: str = "question"


class MappingEvidence(FrozenModel):
    """Question evidence collected once for an allowed QueryScope union.

    ``matches`` intentionally remains raw: it is neither globally top-k limited
    nor deduplicated by semantic element ID.  Those operations belong to the
    deterministic per-Scope ``MappingResult`` projection.
    """

    normalized_question: str
    dataset_ids: tuple[str, ...]
    matches: tuple[MappingEvidenceMatch, ...]
    config_version: str
    index_snapshot_id: str
    embedding_model_id: str
    embedding_collected: bool = False
    embedding_gateway_available: bool = False


class SemanticAmbiguityMember(FrozenModel):
    """One typed member of a Mapper-authored detected-text collision."""

    element_type: SemanticElementType
    element_id: str = Field(min_length=1)


class SemanticAmbiguityGroup(FrozenModel):
    """A collision boundary that never relies on family-local bare IDs.

    ``detected_text`` belongs to the group itself.  Keeping it next to typed
    members prevents a repeated ID in another semantic family or another
    detected phrase from being backfilled into this group later.
    """

    detected_text: str = Field(min_length=1)
    members: tuple[SemanticAmbiguityMember, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def require_distinct_typed_members(self) -> SemanticAmbiguityGroup:
        keys = tuple((item.element_type, item.element_id) for item in self.members)
        if len(keys) != len(set(keys)):
            raise ValueError("semantic ambiguity members must be unique by type and id")
        return self


class MappingResult(FrozenModel):
    dataset_id: str
    mode: MapMode
    normalized_question: str
    matches: tuple[SchemaMatch, ...]
    # ``ambiguous_groups`` remains as the legacy parser projection. New
    # ambiguity governance must consume ``semantic_ambiguity_groups`` because
    # semantic IDs are unique only within their resource family.
    ambiguous_groups: tuple[tuple[str, ...], ...] = ()
    semantic_ambiguity_groups: tuple[SemanticAmbiguityGroup, ...] = ()
    config_version: str
    degraded_reasons: tuple[str, ...] = ()


class ParsedSemanticCandidate(FrozenModel):
    """Parse-info projection for textual S2SQL.

    Natural-language candidates deliberately do not contain ``SemanticQuery``.
    ``parsed_s2sql``/``corrected_s2sql`` remain authoritative until the textual
    Translator Parser Registry has produced physical SQL.
    """

    id: str
    dataset_id: str
    parsed_s2sql: str
    corrected_s2sql: str
    query_type: SemanticQueryType
    score: float = Field(ge=0.0)
    map_mode: MapMode
    mapping: MappingResult
    parser: Literal["rule", "llm", "structured"]
    rationale: str = ""
    applied_defaults: tuple[str, ...] = ()


class CorrectedStructuredQuery(FrozenModel):
    """Validated QueryStructReq-equivalent input for the structured path only."""

    semantic_query: SemanticQuery
    canonical_s2sql: str
    applied_defaults: tuple[str, ...] = ()


class CandidateSet(FrozenModel):
    candidates: tuple[ParsedSemanticCandidate, ...]
    mapping_attempts: tuple[MappingResult, ...]


class ClarificationOption(FrozenModel):
    candidate_id: str
    kind: Literal["metric", "dimension", "dimension_value", "analysis_object"] = "analysis_object"
    label: str
    description: str
    # Internal routing aid only. Never serialize a QueryScope/Dataset ID into
    # the ordinary clarification response shown to end users.
    dataset_id: str = Field(exclude=True)
    # Opaque wire tokens cannot double as semantic identity.  Settlement uses
    # these excluded fields to disclose the exact member the LLM chose without
    # parsing or leaking the signed continuation token.
    element_type: Literal["metric", "dimension", "dimension_value"] | None = Field(
        default=None,
        exclude=True,
    )
    element_id: str | None = Field(default=None, exclude=True)
    # A multi-phrase confirmation binds one governed choice per phrase. The
    # opaque token carries these refs; they never enter the ordinary wire.
    semantic_selection_ids: tuple[str, ...] = Field(default=(), exclude=True)


class ResolvedAmbiguity(FrozenModel):
    """One ambiguous phrase the final LLM settled; shipped so the choice is visible.

    ``chosen``/``alternatives`` reuse ``ClarificationOption`` so the caller can
    switch with the same ``selected_candidate_id`` a clarification would use.
    """

    detected_text: str
    chosen: ClarificationOption
    alternatives: tuple[ClarificationOption, ...]


class SemanticDecisionSource(StrEnum):
    HUMAN = "human"
    AI = "ai"
    MEMORY = "memory"
    FINAL_LLM = "final_llm"


class SemanticDecision(FrozenModel):
    """Public, business-only disclosure of one automatic or confirmed choice."""

    source: SemanticDecisionSource
    detected_text: str
    chosen: ClarificationOption
    alternatives: tuple[ClarificationOption, ...] = ()


class QueryTraceStep(FrozenModel):
    stage: QueryStage
    status: Literal["started", "completed", "failed", "clarification"]
    detail: dict[str, Any] = Field(default_factory=dict)


class ObservedTrace(list):
    """实时通知观察者的 trace 列表；不改变任何既有 trace 语义。

    流水线在几十处直接 `trace.append(...)` / `trace[-1] = ...` 记录阶段，
    没有单一记录函数。用列表子类接管这两个写入口，是让调用方实时看到阶段
    推进、又完全不动决策代码的唯一方式；观察者只读，异常一律吞掉——可观察
    性不得影响查询结果。
    """

    def __init__(
        self,
        iterable: Any = (),
        *,
        observer: Any = None,
    ) -> None:
        super().__init__(iterable)
        self._observer = observer
        for item in self:
            self._notify(item)

    def _notify(self, item: Any) -> None:
        if self._observer is None:
            return
        # 可观察性不得影响查询结果：观察者抛什么都吞掉。
        with suppress(Exception):
            self._observer(item)

    def append(self, item: Any) -> None:
        super().append(item)
        self._notify(item)

    def __setitem__(self, index: Any, item: Any) -> None:
        super().__setitem__(index, item)
        if not isinstance(index, slice):
            self._notify(item)


class QueryError(FrozenModel):
    stage: str
    code: str
    message: str
    retryable: bool = False


class QueryDiagnosis(FrozenModel):
    category: QueryDiagnosticCategory
    stage: str
    severity: Literal["info", "warning", "error"]
    summary: str = Field(min_length=1, max_length=1_000)
    # 给建模者看的：指向物理 SQL、Embedding 候选、Revision 版本这些内部产物。
    recommendation: str = Field(default="", max_length=2_000)
    # 给提问的业务用户看的：只说"换个问法 / 缩小范围 / 刷新"这类他能做的事。
    # 两者刻意分开，终端问数界面只展示这一条。
    user_hint: str = Field(default="", max_length=1_000)


class QueryFailureRecord(FrozenModel):
    """One refused question, kept so the vocabulary it reveals is not lost.

    成功的问句只为多轮改写而存（且要求 conversation_id）；失败的问句此前直接丢弃。
    于是"系统听不懂哪些说法"这份最有价值的数据从未落地：术语挖掘、别名缺口、
    黄金问题种子都没有输入。这条记录不参与任何在线链路，只供离线聚合。
    """

    question: str = Field(min_length=1, max_length=4_000)
    effective_question: str = Field(default="", max_length=4_000)
    stage: str
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=4_000)
    release_id: str
    spec_hash: str
    index_snapshot_id: str
    dataset_ids: tuple[str, ...] = ()
    # 错误自带的结构化上下文；映射失败时是各次映射尝试，含命中了哪些语义对象。
    details: dict[str, Any] = Field(default_factory=dict)


class QueryInterpretation(FrozenModel):
    dataset_id: str
    query_type: SemanticQueryType = SemanticQueryType.AGGREGATE
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[str, ...]
    applied_defaults: tuple[str, ...] = ()


class QueryResponseBase(FrozenModel):
    query_id: str
    state: QueryState
    release_id: str
    spec_hash: str
    # Structured QueryStructReq execution has no semantic-index dependency.
    # Natural-language responses bind the exact Mapper index here; structured
    # responses leave it unset rather than manufacturing a fake snapshot.
    index_snapshot_id: str | None = None
    trace: tuple[QueryTraceStep, ...]
    diagnostics: QueryDiagnosis | None = None


class DrilldownOption(FrozenModel):
    """One follow-up cut the caller may take on a completed answer.

    ``token`` is an opaque HMAC continuation (``drl1``) bound to the exact
    actor/project/query/release context, mirroring the ``sel1`` clarification
    tokens.  The ordinary wire carries only the governed business ``label``;
    the semantic element is recovered server-side from the token.

    ``action``: ``add`` splits by another dimension, ``remove`` drops one the
    answer already groups by, ``refilter`` swaps the value of an existing
    dimension filter, ``replace`` switches the metric, ``retime`` swaps the
    governed default time window.  Without the shrinking/altering actions a
    drilldown chain could only ever grow.
    """

    token: str = Field(min_length=1, max_length=1_024)
    kind: Literal["dimension", "metric", "time"]
    action: Literal["add", "remove", "refilter", "replace", "retime"]
    label: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def action_matches_kind(self) -> DrilldownOption:
        allowed = {
            "dimension": {"add", "remove", "refilter"},
            "metric": {"replace"},
            "time": {"retime"},
        }
        if self.action not in allowed[self.kind]:
            raise ValueError("drilldown action does not match its kind")
        return self


class DrilldownQueryRequest(FrozenModel):
    """Continuation of a completed query by one displayed drilldown option.

    ``value`` accompanies only ``refilter`` tokens: it is a business value
    literal (the same trust level as words in a natural-language question),
    never a semantic ID.  An unknown value simply matches no rows.
    """

    project_id: str = Field(min_length=1, max_length=128)
    query_id: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=1, max_length=1_024)
    value: str | None = Field(default=None, min_length=1, max_length=512)


class CompletedQueryResponse(QueryResponseBase):
    state: Literal[QueryState.COMPLETED] = QueryState.COMPLETED
    interpretation: QueryInterpretation
    data: QueryResult
    visualization: dict[str, Any]
    # Output-only inspector projection. For natural-language requests the
    # execution authority is corrected_s2sql; this DTO must never be fed back
    # into the textual Translator path.
    semantic_query: SemanticQuery
    # Distinct-name ambiguities the LLM decided. Empty when the phrase was
    # unambiguous or the user already picked via ``selected_candidate_id``.
    resolved_by_llm: tuple[ResolvedAmbiguity, ...] = ()
    semantic_decisions: tuple[SemanticDecision, ...] = ()
    # Signed follow-up cuts (split by another dimension / switch the metric).
    # Empty when the caller context cannot bind a token (no actor) or the
    # dataset has no remaining governed members.
    drilldown: tuple[DrilldownOption, ...] = ()
    # Display label per result column, positionally aligned with
    # ``data.columns``.  The textual path emits the SQL alias as the result
    # column (``RATIO_OVER("净收入") AS "同比"`` yields ``同比``), so mapping
    # result columns through semantic IDs alone degrades them to "结果列 N".
    column_labels: tuple[str, ...] = ()
    # 与 data.columns 逐位对齐的时间粒度；DATE_TRUNC 派生列非空。按年分组的
    # 结果值仍是 timestamptz，展示前要收敛到该粒度。
    column_grains: tuple[str | None, ...] = ()
    parsed_s2sql: str
    corrected_s2sql: str
    physical_sql: str | None = None


class ClarificationQueryResponse(QueryResponseBase):
    state: Literal[QueryState.CLARIFICATION_REQUIRED] = QueryState.CLARIFICATION_REQUIRED
    question: str
    options: tuple[ClarificationOption, ...]


class FailedQueryResponse(QueryResponseBase):
    state: Literal[QueryState.FAILED] = QueryState.FAILED
    error: QueryError


QueryResponse = CompletedQueryResponse | ClarificationQueryResponse | FailedQueryResponse


class QueryRequest(FrozenModel):
    project_id: str
    question: str = Field(min_length=1, max_length=4_000)
    dataset_ids: tuple[str, ...] = Field(default=(), max_length=100)
    selected_candidate_id: str | None = Field(default=None, min_length=1, max_length=1_024)
    expected_release_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_spec_hash: str | None = Field(default=None, min_length=1, max_length=128)
    expected_index_snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    query_id: str | None = Field(default=None, min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_debug_sql: bool = False
    include_diagnostics: bool = False

    @field_validator("dataset_ids")
    @classmethod
    def dataset_scope_is_bounded_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value or len(value) > 128 for value in normalized):
            raise ValueError("dataset scope is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("dataset scope must be unique")
        return normalized

    @model_validator(mode="after")
    def bind_selection_to_release(self) -> QueryRequest:
        expected = (
            self.expected_release_id,
            self.expected_spec_hash,
            self.expected_index_snapshot_id,
        )
        if self.selected_candidate_id is not None and any(item is None for item in expected):
            raise ValueError("candidate selection requires the originating release version")
        if self.selected_candidate_id is None and any(item is not None for item in expected):
            raise ValueError("release version is only valid with a candidate selection")
        return self


class StructuredQueryRequest(FrozenModel):
    """Structured query request that carries semantic IDs only."""

    project_id: str = Field(min_length=1, max_length=128)
    semantic_query: SemanticQuery
    query_id: str | None = Field(default=None, min_length=1, max_length=128)
    include_debug_sql: bool = False
