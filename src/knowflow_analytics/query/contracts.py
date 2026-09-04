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
    # 用户在指标卡上明确选择的指标。它没有被问句召回过——正因如此才需要问——
    # 所以既不能伪装成 EXACT，也不能算进弱召回。
    CONFIRMED = "confirmed"


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
    CONFIRMED = "confirmed"


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
    # 模型报的"这个说法我理解成了那个成员"，已过字面子串校验。给反馈页做预填。
    # Rule 路径为空——它不理解问句，只做模式匹配。
    inferred_terms: tuple[tuple[str, str], ...] = Field(default=(), max_length=10)


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
    """一次「系统没接住用户说法」的记录，供离线术语挖掘，不参与任何在线链路。

    同一个信号有三种收场，都记在这里（``kind``）：

    - ``refused``：查询被拒答。只知道失败了，正解未知，要人去诊断。
    - ``clarified``：弹了澄清卡，用户选了。**自带正解**——「业绩」→「销售金额」
      可以直接采纳成别名，是最有价值的一类。
    - ``inferred``：没弹卡，模型自己挑了一个词典里没有的说法对应的成员。正解是**模型
      猜的**，可能对也可能错，但同一说法被猜过很多次本身就是该补词典的信号。
    - ``unknown_value``：过滤值不在该维度的已发布取值里，查询返回 0 行。正解可能
      是近似建议（「卡布奇洛」→「卡布奇诺」），也可能确实没有。

    ``resolution`` 是这次的正解（用户选中的成员名，或近似建议值），没有就留空。
    历史原因保留 ``QueryFailureRecord`` 这个名字与 ``query-failures`` 路由；
    三类都是同一个词汇缺口，只是这一轮怎么收场不同。
    """

    kind: Literal["refused", "clarified", "inferred", "unknown_value"] = "refused"
    # 这次的正解：用户在澄清卡上选中的成员名，或未发布取值的近似建议。
    resolution: str = Field(default="", max_length=256)
    question: str = Field(min_length=1, max_length=4_000)
    effective_question: str = Field(default="", max_length=4_000)
    stage: str
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(default="", max_length=4_000)
    release_id: str
    spec_hash: str
    index_snapshot_id: str
    dataset_ids: tuple[str, ...] = ()
    # 模型报的"这个说法我理解成了那个成员"，已过字面子串校验。**首选**：它带配对，
    # 预填术语表单要的正是这一对；而且该沉默时会沉默（对照实验 10/12 vs 7/12）。
    inferred_terms: tuple[tuple[str, str], ...] = Field(default=(), max_length=10)
    # 问句里没被任何精确证据覆盖的片段。**补漏用**：模型会漏报（实测「各门店的业绩」
    # 返回空，而同类的「营业额」「毛利」都报了），而这条永远算得出来。
    #
    # 两个来源都留，因为两种失误的代价不对称：漏报意味着这个说法**永远补不进词典**
    # （用户根本不知道有这回事），误报只是列表里多一条一眼就不像术语的东西。
    unmatched_phrases: tuple[str, ...] = Field(default=(), max_length=20)
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


class QueryRowFilter(FrozenModel):
    """行级权限的一条谓词，用**受治理维度 ID** 表达。

    只接受 ``eq`` / ``in`` 两种结构化算子，不接受自由 SQL：自由 SQL 的行级过滤
    是注入面，也无法在翻译期证明它落在正确的模型上。核心按 dimension_id 反查
    field_id 与 model_id，注入到该模型的数据源包装里。
    """

    dimension_id: str = Field(min_length=1, max_length=128)
    values: tuple[str, ...] = Field(min_length=1, max_length=1_000)

    @field_validator("values")
    @classmethod
    def values_are_bounded(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value or "") for value in values)
        if any(len(value) > 512 for value in normalized):
            raise ValueError("row filter value is too long")
        return tuple(dict.fromkeys(normalized))


class QueryOptions(FrozenModel):
    """一次问数的可覆盖配置。

    **每一项都可以为空，空 = 跟随部署的全局默认。** 这与 ``llm_id`` 空值跟随租户默认
    是同一个模式：助手不填就用部署配的那套，填了才在这次请求上生效。所以打开这个功能
    不改变任何现有部署的行为。

    这些值按请求逐层传下去，而不是在装配期固化——``tenant_id``、``visible_element_ids``
    走的也是这条路。让"每请求变化的东西"只有一种传法，比再造一套克隆机制少一处会漂移
    的地方。
    """

    # 同一问题独立生成多次取多数，压模型的形态漂移。代价是模型调用数按倍数增加。
    self_consistency_number: int | None = Field(default=None, ge=1, le=9)
    s2sql_corrector_enabled: bool | None = None
    physical_sql_corrector_enabled: bool | None = None
    multi_turn_enabled: bool | None = None
    dry_run_before_execute: bool | None = None
    # 空 = 跟随租户默认模型。指定则这次问数走指定的那个。
    llm_id: str | None = Field(default=None, max_length=256)
    # 首次生成的温度。重试的逐级升温是内置机制，不在这里暴露——把它压成单个值
    # 会让"失败后跳出重复无效输出"的递进失效。
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8_192)

    def merged(self, field: str, fallback):
        """助手填了就用助手的，没填就用全局的。"""

        value = getattr(self, field)
        return fallback if value is None else value


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
    # 列级权限的成员白名单（受治理指标/维度 ID）。``None`` = 不收窄；空元组 =
    # 一个成员都不可见（合法状态，来自"授权的实体已从目录删除"）。核心只负责
    # **应用**这个约束，不做权限判断——判断在宿主 BFF，与 dataset_ids 同源。
    allowed_element_ids: tuple[str, ...] | None = Field(default=None, max_length=5_000)
    # 行级权限：按受治理维度限定可见行。``None`` 与空元组都表示"不限制行"——
    # 行级与列级不同，没有"一行都看不到"这种由空集合表达的状态；要挡住整个项目
    # 应该不授权，而不是发一份空的行过滤。
    row_filters: tuple[QueryRowFilter, ...] | None = Field(default=None, max_length=100)
    # 助手级配置。空对象 = 全部跟随全局默认。
    options: QueryOptions = Field(default_factory=QueryOptions)

    @field_validator("allowed_element_ids")
    @classmethod
    def element_whitelist_is_bounded(
        cls, values: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if values is None:
            return None
        normalized = tuple(str(value or "").strip() for value in values)
        if any(not value or len(value) > 256 for value in normalized):
            raise ValueError("element whitelist is invalid")
        return tuple(dict.fromkeys(normalized))

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
    # 与 QueryRequest 同一套行列级权限输入。下钻走的就是这条路径：签名 token 绑了
    # actor/project/release，但绑不住"授权此后被撤销"，所以权限必须每次请求重算
    # 后传进来，而不是从 token 里恢复。
    allowed_element_ids: tuple[str, ...] | None = Field(default=None, max_length=5_000)
    row_filters: tuple[QueryRowFilter, ...] | None = Field(default=None, max_length=100)
