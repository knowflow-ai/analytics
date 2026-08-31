from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionValueSpec,
    FrozenModel,
    SemanticContextEntry,
    SemanticRelease,
)
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.catalog_contracts import (
    MetricContract,
    SemanticCatalog,
)


class SchemaColumnSnapshot(FrozenModel):
    name: str = Field(min_length=1, max_length=256)
    data_type: str = Field(min_length=1, max_length=256)
    nullable: bool
    comment: str = Field(default="", max_length=4_000)
    ordinal_position: int = Field(ge=0)
    primary_key: bool = False
    unique: bool = False


class ForeignKeySnapshot(FrozenModel):
    name: str | None = Field(default=None, max_length=256)
    constrained_columns: tuple[str, ...] = Field(min_length=1)
    referred_schema: str
    referred_table: str
    referred_columns: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def matching_arity(self) -> ForeignKeySnapshot:
        if len(self.constrained_columns) != len(self.referred_columns):
            raise ValueError("foreign key columns must have matching arity")
        return self


class TableSnapshot(FrozenModel):
    schema_name: str
    name: str
    source_type: Literal["table", "view"] = "table"
    comment: str = Field(default="", max_length=4_000)
    columns: tuple[SchemaColumnSnapshot, ...] = Field(min_length=1)
    foreign_keys: tuple[ForeignKeySnapshot, ...] = ()


class TableCatalogEntry(FrozenModel):
    """Read-only datasource metadata for a modeling UI or API client."""

    schema_name: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    source_type: Literal["table", "view"] = "table"
    comment: str = Field(default="", max_length=4_000)


class SchemaSnapshot(FrozenModel):
    id: str
    datasource_type: Literal["postgresql"] = "postgresql"
    database_name: str
    tables: tuple[TableSnapshot, ...] = Field(min_length=1)
    captured_at: datetime
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        database_name: str,
        tables: tuple[TableSnapshot, ...],
        captured_at: datetime,
    ) -> SchemaSnapshot:
        digest = content_hash(
            {
                "datasource_type": "postgresql",
                "database_name": database_name,
                "tables": [table.model_dump(mode="json") for table in tables],
            }
        )
        return cls(
            id=f"schema_{digest.removeprefix('sha256:')[:16]}",
            database_name=database_name,
            tables=tables,
            captured_at=captured_at,
            content_hash=digest,
        )


class EvidenceRef(FrozenModel):
    knowledgebase_id: str
    document_id: str
    document_revision: str
    chunk_id: str
    quote_hash: str
    citation: str = Field(default="", max_length=1_000)


class SuggestionSource(StrEnum):
    RULE = "rule"
    AI_SCHEMA = "ai_schema"
    AI_KNOWLEDGE = "ai_knowledge"
    DATABASE_CONSTRAINT = "database_constraint"


class SuggestionState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CONFLICT = "conflict"


class SuggestionPatch(FrozenModel):
    id: str
    target_kind: Literal["model", "field", "relation"]
    target_id: str
    changes: dict[str, Any]
    source: SuggestionSource
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=2_000)
    evidence: tuple[EvidenceRef, ...] = ()
    high_impact: bool = False
    state: SuggestionState = SuggestionState.PENDING

    @model_validator(mode="after")
    def validate_change_keys(self) -> SuggestionPatch:
        allowed = {
            "model": {"name", "biz_name", "description", "aliases"},
            "field": {
                "name",
                "description",
                "kind",
                "identifier_type",
                "dimension_type",
                "semantic_expr",
                "unit",
                "aggregation",
                "create_dimension",
                "create_metric",
            },
            "relation": {
                "left_model_id",
                "right_model_id",
                "join_type",
                "cardinality",
                "conditions",
            },
        }
        unknown = set(self.changes) - allowed[self.target_kind]
        if unknown:
            raise ValueError(f"unsupported {self.target_kind} patch keys: {sorted(unknown)}")
        if not self.changes:
            raise ValueError("suggestion patch cannot be empty")
        if self.source is SuggestionSource.AI_KNOWLEDGE and not self.evidence:
            raise ValueError("knowledge-backed AI suggestion requires evidence")
        return self


class SuggestionDecision(FrozenModel):
    suggestion_id: str
    accept: bool
    overrides: dict[str, Any] = Field(default_factory=dict)


class RevisionState(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    FROZEN = "frozen"
    PUBLISHED = "published"


class ModelingRevision(FrozenModel):
    id: str
    project_id: str
    schema_snapshot_hash: str
    etag: int = Field(ge=1)
    state: RevisionState = RevisionState.DRAFT
    semantic_spec: SemanticRelease
    semantic_catalog: SemanticCatalog | None = None
    suggestions: tuple[SuggestionPatch, ...] = ()
    parent_revision_id: str | None = None
    modeling_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    ai_modeling_artifact_hash: str | None = Field(default=None, max_length=128)
    ai_alias_reviewed_resources: tuple[str, ...] = Field(default=(), max_length=20_000)
    semantic_context_review_hash: str | None = Field(default=None, max_length=128)
    semantic_context_reviewed_by: str | None = Field(default=None, min_length=1, max_length=128)
    semantic_context_reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def catalog_projection_is_bound(self) -> ModelingRevision:
        review_values = (
            self.semantic_context_review_hash,
            self.semantic_context_reviewed_by,
            self.semantic_context_reviewed_at,
        )
        if any(item is not None for item in review_values) and any(
            item is None for item in review_values
        ):
            raise ValueError("semantic context review audit must be complete")
        if self.semantic_spec.semantic_context:
            if self.semantic_context_review_hash is not None and (
                self.semantic_context_review_hash
                != semantic_context_content_hash(self.semantic_spec.semantic_context)
            ):
                raise ValueError("semantic context review does not bind current content")
        elif any(item is not None for item in review_values):
            raise ValueError("empty semantic context cannot carry a review audit")
        if self.semantic_catalog is None:
            if self.semantic_spec.modeling_catalog is not None:
                raise ValueError("semantic projection has an untyped modeling catalog")
            return self
        if self.semantic_catalog.project_id != self.project_id:
            raise ValueError("modeling catalog belongs to another project")
        if self.semantic_catalog.revision_id != self.id:
            raise ValueError("modeling catalog belongs to another revision")
        stored_projection = self.semantic_spec.modeling_catalog
        # 存量投影可能带已退役键(如 upstreamCommit)。catalog 字段侧靠
        # drop_retired_keys 能加载,投影比较也必须过同一合同归一化——退役键被
        # 对称丢弃,真实结构漂移仍然拒载。拿原始字典做全等,等于退役一个字段
        # 就把所有历史 Revision 判死(全线 INTERNAL_ERROR 的事故来源)。
        normalized_projection = (
            SemanticCatalog.model_validate(stored_projection).canonical_payload()
            if stored_projection is not None
            else None
        )
        if normalized_projection != self.semantic_catalog.canonical_payload():
            raise ValueError("modeling catalog and query projection differ")
        return self


class ModelingRunSource(StrEnum):
    API = "api"
    UI = "ui"
    DEEPAGENT = "deepagent"


class ModelingRunStatus(StrEnum):
    COMPLETED = "completed"
    APPLIED = "applied"


class ModelingSuggestionRun(FrozenModel):
    """An auditable AI result that does not mutate its target revision.

    The UI may overlay these patches as prefilled form values. Only a separate
    human-confirmed apply command can change the semantic revision.
    """

    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    status: ModelingRunStatus = ModelingRunStatus.COMPLETED
    source: ModelingRunSource = ModelingRunSource.API
    source_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    manifest_hash: str | None = Field(default=None, max_length=128)
    input_hash: str = Field(min_length=1, max_length=128)
    suggestions: tuple[SuggestionPatch, ...] = ()
    created_at: datetime
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_at: datetime | None = None
    decisions: tuple[SuggestionDecision, ...] = ()
    resulting_revision_etag: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def review_state_is_complete(self) -> ModelingSuggestionRun:
        review_values = (
            self.reviewed_by,
            self.reviewed_at,
            self.resulting_revision_etag,
        )
        if self.status is ModelingRunStatus.COMPLETED:
            if any(item is not None for item in review_values) or self.decisions:
                raise ValueError("completed modeling run cannot contain review results")
            return self
        if any(item is None for item in review_values):
            raise ValueError("applied modeling run requires a complete human review audit")
        return self


AliasResourceType = Literal["dimension", "metric", "dimension_value"]


class SemanticAliasReview(FrozenModel):
    """Human review of one AI-proposed semantic alias resource."""

    resource_type: AliasResourceType
    resource_id: str = Field(min_length=1, max_length=128)
    aliases: tuple[str, ...] = Field(default=(), max_length=100)
    display_name: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def aliases_are_clean(self) -> SemanticAliasReview:
        normalized: set[str] = set()
        for alias in self.aliases:
            value = alias.strip()
            if not value or len(value) > 256:
                raise ValueError("semantic aliases must contain 1 to 256 characters")
            key = value.casefold()
            if key in normalized:
                raise ValueError("semantic aliases must be unique")
            normalized.add(key)
        if self.resource_type != "dimension_value" and self.display_name is not None:
            raise ValueError("only dimension-value aliases may change display_name")
        return self


class SemanticAliasDraft(SemanticAliasReview):
    """One editable alias suggestion included in an AI modeling Candidate."""

    resource_name: str = Field(default="", max_length=256)


QUERY_SCOPE_COMPILER_VERSION = "knowflow-query-scope-v1"


class QueryScopeExclusionDiagnostic(FrozenModel):
    element_id: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=128)


class QueryScopeCompilationDiagnostic(FrozenModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    root_model_id: str = Field(min_length=1, max_length=128)
    model_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    default_count_metric_id: str | None = Field(default=None, max_length=128)
    path_relation_ids: tuple[tuple[str, ...], ...] = ()
    canonical_names: dict[str, str] = Field(default_factory=dict)
    exclusions: tuple[QueryScopeExclusionDiagnostic, ...] = ()


class AiModelingArtifact(FrozenModel):
    """The complete output returned by one click on “AI 自动建模”."""

    alias_drafts: tuple[SemanticAliasDraft, ...] = ()
    base_semantic_spec_hash: str = Field(min_length=1, max_length=128)
    dimension_values: tuple[DimensionValueSpec, ...] = ()
    default_count_metrics: tuple[MetricContract, ...] = ()
    analysis_topic_datasets: tuple[DatasetSpec, ...] = ()
    analysis_topic_routes: tuple[AnalysisTopicRouteSpec, ...] = ()
    semantic_context: tuple[SemanticContextEntry, ...] = ()
    query_scope_compiler_version: str = QUERY_SCOPE_COMPILER_VERSION
    query_scope_compilation_hash: str | None = Field(default=None, max_length=128)
    query_scope_diagnostics: tuple[QueryScopeCompilationDiagnostic, ...] = ()
    artifact_hash: str = Field(min_length=1, max_length=128)

    @classmethod
    def create(
        cls,
        *,
        base_semantic_spec_hash: str,
        alias_drafts: Iterable[SemanticAliasDraft],
        dimension_values: Iterable[DimensionValueSpec],
        default_count_metrics: Iterable[MetricContract],
        analysis_topic_datasets: Iterable[DatasetSpec],
        analysis_topic_routes: Iterable[AnalysisTopicRouteSpec],
        semantic_context: Iterable[SemanticContextEntry] = (),
        query_scope_diagnostics: Iterable[QueryScopeCompilationDiagnostic] = (),
        query_scope_compiler_version: str = QUERY_SCOPE_COMPILER_VERSION,
    ) -> AiModelingArtifact:
        diagnostics = tuple(query_scope_diagnostics)
        values = {
            "alias_drafts": tuple(alias_drafts),
            "dimension_values": tuple(dimension_values),
            "default_count_metrics": tuple(default_count_metrics),
            "analysis_topic_datasets": tuple(analysis_topic_datasets),
            "analysis_topic_routes": tuple(analysis_topic_routes),
            "semantic_context": tuple(semantic_context),
            "query_scope_diagnostics": diagnostics,
        }
        return cls(
            base_semantic_spec_hash=base_semantic_spec_hash,
            **values,
            query_scope_compiler_version=query_scope_compiler_version,
            query_scope_compilation_hash=_query_scope_compilation_hash(
                compiler_version=query_scope_compiler_version,
                datasets=values["analysis_topic_datasets"],
                routes=values["analysis_topic_routes"],
                diagnostics=diagnostics,
            ),
            artifact_hash=_modeling_artifact_hash(
                base_semantic_spec_hash=base_semantic_spec_hash,
                values=values,
            ),
        )

    @model_validator(mode="after")
    def artifact_is_self_consistent(self) -> AiModelingArtifact:
        dataset_ids = [item.id for item in self.analysis_topic_datasets]
        route_ids = [item.dataset_id for item in self.analysis_topic_routes]
        if len(dataset_ids) != len(set(dataset_ids)) or len(route_ids) != len(set(route_ids)):
            raise ValueError("AI modeling artifact contains duplicate analysis topics")
        if set(dataset_ids) != set(route_ids):
            raise ValueError("AI modeling artifact datasets and routes must match")
        expected_values: dict[str, tuple] = {
            "alias_drafts": self.alias_drafts,
            "dimension_values": self.dimension_values,
            "default_count_metrics": self.default_count_metrics,
            "analysis_topic_datasets": self.analysis_topic_datasets,
            "analysis_topic_routes": self.analysis_topic_routes,
        }
        if self.semantic_context or self.query_scope_compilation_hash is not None:
            expected_values["semantic_context"] = self.semantic_context
        if self.query_scope_compilation_hash is not None:
            expected_values["query_scope_diagnostics"] = self.query_scope_diagnostics
        expected = _modeling_artifact_hash(
            base_semantic_spec_hash=self.base_semantic_spec_hash,
            values=expected_values,
        )
        if expected != self.artifact_hash:
            raise ValueError("AI modeling artifact hash is invalid")
        if self.query_scope_compilation_hash is not None:
            expected_scope_hash = _query_scope_compilation_hash(
                compiler_version=self.query_scope_compiler_version,
                datasets=self.analysis_topic_datasets,
                routes=self.analysis_topic_routes,
                diagnostics=self.query_scope_diagnostics,
            )
            if expected_scope_hash != self.query_scope_compilation_hash:
                raise ValueError("query scope compilation hash is invalid")
        return self

    def with_alias_reviews(
        self,
        reviews: Iterable[SemanticAliasReview],
    ) -> AiModelingArtifact:
        review_items = tuple(reviews)
        review_by_key = {(item.resource_type, item.resource_id): item for item in review_items}
        draft_by_key = {(item.resource_type, item.resource_id): item for item in self.alias_drafts}
        if len(review_by_key) != len(review_items) or set(review_by_key) != set(draft_by_key):
            raise ValueError("every AI alias draft requires exactly one human review")
        reviewed_drafts = tuple(
            draft.model_copy(
                update={
                    "aliases": review_by_key[key].aliases,
                    "display_name": review_by_key[key].display_name,
                }
            )
            for key, draft in draft_by_key.items()
        )
        return AiModelingArtifact.create(
            base_semantic_spec_hash=self.base_semantic_spec_hash,
            alias_drafts=reviewed_drafts,
            dimension_values=self.dimension_values,
            default_count_metrics=self.default_count_metrics,
            analysis_topic_datasets=self.analysis_topic_datasets,
            analysis_topic_routes=self.analysis_topic_routes,
            semantic_context=self.semantic_context,
            query_scope_diagnostics=self.query_scope_diagnostics,
            query_scope_compiler_version=self.query_scope_compiler_version,
        )


def semantic_context_content_hash(
    entries: Iterable[SemanticContextEntry],
) -> str:
    """Bind the exact reviewed context independently from unrelated artifacts."""

    return content_hash(
        {
            "semantic_context": [
                item.model_dump(mode="json") for item in tuple(entries)
            ]
        }
    )


def _query_scope_compilation_hash(
    *,
    compiler_version: str,
    datasets: tuple[DatasetSpec, ...],
    routes: tuple[AnalysisTopicRouteSpec, ...],
    diagnostics: tuple[QueryScopeCompilationDiagnostic, ...],
) -> str:
    return content_hash(
        {
            "compiler_version": compiler_version,
            "datasets": [item.model_dump(mode="json") for item in datasets],
            "routes": [item.model_dump(mode="json") for item in routes],
            "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        }
    )


def _modeling_artifact_hash(
    *,
    base_semantic_spec_hash: str,
    values: dict[str, tuple],
) -> str:
    return content_hash(
        {
            "base_semantic_spec_hash": base_semantic_spec_hash,
            "resources": {
                key: [item.model_dump(mode="json") for item in items]
                for key, items in values.items()
            },
        }
    )


class ModelingProposalStatus(StrEnum):
    DRAFT = "draft"
    APPLIED = "applied"


class ModelingProposal(FrozenModel):
    """A human-editable overlay for one immutable AI suggestion run.

    The model-schema build produces a complete ``ModelSchema`` per table.  That
    batch result stays outside the governed Revision until one explicit final
    confirmation.  It is deliberately a patch overlay, not a second Catalog.
    """

    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    suggestion_run_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    semantic_spec_hash: str = Field(min_length=1, max_length=128)
    etag: int = Field(ge=1)
    status: ModelingProposalStatus = ModelingProposalStatus.DRAFT
    suggestions: tuple[SuggestionPatch, ...]
    decisions: tuple[SuggestionDecision, ...]
    artifact: AiModelingArtifact
    reviewed_artifact_hash: str | None = Field(default=None, max_length=128)
    proposal_hash: str = Field(min_length=1, max_length=128)
    created_by: str = Field(min_length=1, max_length=128)
    created_at: datetime
    updated_by: str = Field(min_length=1, max_length=128)
    updated_at: datetime
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_at: datetime | None = None
    resulting_revision_etag: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def proposal_is_complete_and_auditable(self) -> ModelingProposal:
        suggestion_ids = [item.id for item in self.suggestions]
        decision_ids = [item.suggestion_id for item in self.decisions]
        if len(suggestion_ids) != len(set(suggestion_ids)):
            raise ValueError("modeling proposal contains duplicate suggestions")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("modeling proposal contains duplicate decisions")
        if set(suggestion_ids) != set(decision_ids):
            raise ValueError("modeling proposal requires one decision per suggestion")
        review_values = (self.reviewed_by, self.reviewed_at, self.resulting_revision_etag)
        if self.status is ModelingProposalStatus.DRAFT and any(
            item is not None for item in review_values
        ):
            raise ValueError("draft modeling proposal cannot contain review audit")
        if self.status is ModelingProposalStatus.APPLIED and any(
            item is None for item in review_values
        ):
            raise ValueError("applied modeling proposal requires complete review audit")
        if (
            self.reviewed_artifact_hash is not None
            and self.reviewed_artifact_hash != self.artifact.artifact_hash
        ):
            raise ValueError("modeling proposal review must bind the current artifact")
        if self.status is ModelingProposalStatus.APPLIED and self.reviewed_artifact_hash is None:
            raise ValueError("applied modeling proposal requires the current artifact review")
        return self


class ModelingProposalApplyResult(FrozenModel):
    proposal: ModelingProposal
    revision: ModelingRevision


class KnowledgeManifest(FrozenModel):
    hash: str
    knowledgebase_ids: tuple[str, ...]
    documents: tuple[dict[str, str], ...]


class ProfiledValue(FrozenModel):
    """A bounded, observed categorical value from the governed datasource."""

    value: str | int | float | bool
    frequency: int = Field(ge=1)


class DimensionDataProfile(FrozenModel):
    dimension_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    field_id: str = Field(min_length=1, max_length=128)
    sampled_rows: int = Field(ge=0)
    observed_distinct_values: int = Field(ge=0)
    source_rows_truncated: bool = False
    truncated: bool = False
    values: tuple[ProfiledValue, ...] = ()


class SemanticDataProfile(FrozenModel):
    """Immutable evidence used to enrich one reviewed semantic revision."""

    id: str = Field(min_length=1, max_length=128)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(min_length=1, max_length=128)
    captured_at: datetime
    dimensions: tuple[DimensionDataProfile, ...] = ()
    warnings: tuple[str, ...] = ()


class DimensionDictionaryStatus(StrEnum):
    COMPLETED = "completed"
    APPLIED = "applied"


class DimensionDictionaryEligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    REVIEW = "review"
    INELIGIBLE = "ineligible"


class DimensionDictionaryEligibility(FrozenModel):
    """Explain whether one governed dimension may enter a value dictionary."""

    dimension_id: str = Field(min_length=1, max_length=128)
    status: DimensionDictionaryEligibilityStatus
    reason_code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000)
    observed_distinct_values: int | None = Field(default=None, ge=0)


class DimensionDictionaryRefreshInterval(StrEnum):
    MANUAL = "manual"
    DAILY = "daily"
    WEEKLY = "weekly"


class DimensionValueListState(StrEnum):
    NORMAL = "normal"
    BLACK = "black"
    WHITE = "white"


class DimensionDictionaryPolicy(FrozenModel):
    """Version-bound dictionary visibility and refresh policy.

    This is the item-value config consumed by the scheduled dictionary task.  It
    is review metadata, not an instruction that may silently mutate a semantic
    revision.
    """

    dimension_id: str = Field(min_length=1, max_length=128)
    visible: bool = True
    ai_aliases: bool = False
    refresh_interval: DimensionDictionaryRefreshInterval = DimensionDictionaryRefreshInterval.MANUAL
    black_list: tuple[str | int | float | bool, ...] = Field(default=(), max_length=10_000)
    white_list: tuple[str | int | float | bool, ...] = Field(default=(), max_length=10_000)
    refreshed_at: datetime | None = None
    next_refresh_at: datetime | None = None

    @model_validator(mode="after")
    def lists_and_schedule_are_consistent(self) -> DimensionDictionaryPolicy:
        black = {content_hash({"value": value}) for value in self.black_list}
        white = {content_hash({"value": value}) for value in self.white_list}
        if len(black) != len(self.black_list) or len(white) != len(self.white_list):
            raise ValueError("dimension dictionary lists must not contain duplicates")
        if black & white:
            raise ValueError("dimension dictionary black and white lists must be disjoint")
        if self.refresh_interval is DimensionDictionaryRefreshInterval.MANUAL:
            if self.next_refresh_at is not None:
                raise ValueError("manual dictionary refresh cannot have a next refresh time")
        elif self.refreshed_at is not None and self.next_refresh_at is None:
            raise ValueError("scheduled dictionary refresh requires a next refresh time")
        return self


class DimensionValueCandidate(FrozenModel):
    """One observed or already-governed value shown on the review screen."""

    id: str = Field(min_length=1, max_length=128)
    dimension_value_id: str = Field(min_length=1, max_length=128)
    dimension_id: str = Field(min_length=1, max_length=128)
    value: str | int | float | bool
    frequency: int | None = Field(default=None, ge=1)
    observed: bool = False
    current: bool = False
    display_name: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = Field(default=(), max_length=100)
    enabled: bool = True
    list_state: DimensionValueListState = DimensionValueListState.NORMAL

    @model_validator(mode="after")
    def has_review_evidence(self) -> DimensionValueCandidate:
        if not self.observed and not self.current:
            raise ValueError("dimension value candidate must be observed or already governed")
        return self


class DimensionValueDecision(FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    accept: bool
    display_name: str | None = Field(default=None, min_length=1, max_length=256)
    aliases: tuple[str, ...] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    list_state: DimensionValueListState | None = None

    @model_validator(mode="after")
    def rejected_values_have_no_overrides(self) -> DimensionValueDecision:
        if not self.accept and any(
            item is not None
            for item in (self.display_name, self.aliases, self.enabled, self.list_state)
        ):
            raise ValueError("rejected dimension values cannot contain overrides")
        if self.display_name is not None and not self.display_name.strip():
            raise ValueError("dimension value display name cannot be blank")
        normalized: set[str] = set()
        for alias in self.aliases or ():
            if not alias.strip() or len(alias) > 256:
                raise ValueError("dimension value aliases must contain 1 to 256 characters")
            key = alias.strip().casefold()
            if key in normalized:
                raise ValueError("dimension value aliases must be unique")
            normalized.add(key)
        return self


class DimensionDictionaryPreview(FrozenModel):
    """Persisted datasource evidence awaiting one complete human review."""

    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    semantic_spec_hash: str = Field(min_length=1, max_length=128)
    selected_dimension_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    policies: tuple[DimensionDictionaryPolicy, ...] = Field(min_length=1, max_length=100)
    eligibilities: tuple[DimensionDictionaryEligibility, ...] = ()
    status: DimensionDictionaryStatus = DimensionDictionaryStatus.COMPLETED
    profile: SemanticDataProfile
    candidates: tuple[DimensionValueCandidate, ...] = Field(default=(), max_length=10_000)
    created_at: datetime
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_at: datetime | None = None
    decisions: tuple[DimensionValueDecision, ...] = ()
    resulting_revision_etag: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def review_state_is_complete(self) -> DimensionDictionaryPreview:
        policy_ids = tuple(item.dimension_id for item in self.policies)
        if policy_ids != self.selected_dimension_ids:
            raise ValueError("dictionary policies must match selected dimensions in order")
        eligibility_ids = tuple(item.dimension_id for item in self.eligibilities)
        if eligibility_ids and eligibility_ids != self.selected_dimension_ids:
            raise ValueError("dictionary eligibility must match selected dimensions in order")
        review_values = (self.reviewed_by, self.reviewed_at, self.resulting_revision_etag)
        if self.status is DimensionDictionaryStatus.COMPLETED:
            if any(item is not None for item in review_values) or self.decisions:
                raise ValueError("completed dimension dictionary preview cannot contain review")
            return self
        if any(item is None for item in review_values):
            raise ValueError("applied dimension dictionary preview requires a complete audit")
        if len(self.decisions) != len(self.candidates):
            raise ValueError("applied dimension dictionary preview requires every decision")
        return self


class DimensionDictionaryApplyResult(FrozenModel):
    preview: DimensionDictionaryPreview
    revision: ModelingRevision
