"""Product-facing projections over the governed semantic-modeling contracts.

Editing Model, Dimension, Metric and DataSet resources directly is a lot to ask
of a first-run user, so this onboarding/decision-queue projection sits on top of
them.  The governed resources and the Revision remain the only query-time truth.
The reviewed divergence is documented in
``docs/knowflow-analytics-modeling-product-logic-optimization.md``.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from knowflow_analytics.contracts import DatasetSpec, FrozenModel
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    ModelingRunStatus,
    ModelingSuggestionRun,
    SuggestionPatch,
    SuggestionSource,
    SuggestionState,
    TableSnapshot,
)
from knowflow_analytics.modeling.revision import RevisionConflictError
from knowflow_analytics.modeling.rule_modeller import stable_id

_MODELING_PLAN_CONTRACT_VERSION = "product-plan-v3"


class ScopeInference(StrEnum):
    DATABASE_CONSTRAINT = "database_constraint"
    SCHEMA_ONLY = "schema_only"


class ScopeRecommendationGroup(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    schema_name: str = Field(min_length=1, max_length=256)
    tables: tuple[str, ...] = Field(min_length=1, max_length=10_000)
    inference: ScopeInference
    foreign_key_count: int = Field(ge=0)
    evidence: tuple[str, ...] = Field(default=(), max_length=100)


class ScopeRecommendationSet(FrozenModel):
    project_id: str = Field(min_length=1, max_length=128)
    datasource_id: str = Field(min_length=1, max_length=128)
    schema_name: str = Field(min_length=1, max_length=256)
    total_table_count: int = Field(ge=0, le=10_000)
    groups: tuple[ScopeRecommendationGroup, ...] = Field(default=(), max_length=10_000)


class DecisionRiskLevel(StrEnum):
    PRESENTATION = "presentation"
    EXECUTION = "execution"
    BLOCKING = "blocking"


class ModelingDecisionOption(FrozenModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=1_000)
    recommended: bool = False


ModelingDecisionKind = Literal[
    "semantic_name",
    "semantic_name_batch",
    "field_classification",
    "measure_aggregation",
    "relation_cardinality",
    "dataset_scope",
    "suggestion_conflict",
    "validation_blocker",
]


class ModelingDecision(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    kind: ModelingDecisionKind
    risk_level: DecisionRiskLevel
    title: str = Field(min_length=1, max_length=512)
    proposal: str = Field(default="", max_length=2_000)
    reason: str = Field(default="", max_length=2_000)
    options: tuple[ModelingDecisionOption, ...] = Field(min_length=1, max_length=20)
    affected_resource_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    source_suggestion_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    write_commands_preview: tuple[str, ...] = Field(default=(), max_length=20)
    proposed_resource: dict[str, Any] | None = None
    option_accepts_suggestion_ids: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        max_length=20,
    )

    @model_validator(mode="after")
    def options_are_unique(self) -> ModelingDecision:
        option_ids = [item.id for item in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("modeling decision options must be unique")
        if set(self.option_accepts_suggestion_ids) - set(option_ids):
            raise ValueError("modeling decision option effects reference unknown options")
        source_ids = set(self.source_suggestion_ids)
        if any(
            not set(accepted_ids).issubset(source_ids)
            for accepted_ids in self.option_accepts_suggestion_ids.values()
        ):
            raise ValueError("modeling decision option effects reference unknown suggestions")
        return self


class ModelingInformation(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["physical_fact"] = "physical_fact"
    title: str = Field(min_length=1, max_length=512)
    detail: str = Field(default="", max_length=2_000)
    affected_resource_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    source_suggestion_ids: tuple[str, ...] = Field(default=(), max_length=1_000)


class DecisionQueueSummary(FrozenModel):
    blocking: int = Field(ge=0)
    needs_confirmation: int = Field(ge=0)
    informational: int = Field(ge=0)


class DecisionQueue(FrozenModel):
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    plan_id: str = Field(min_length=1, max_length=128)
    summary: DecisionQueueSummary
    decisions: tuple[ModelingDecision, ...] = Field(default=(), max_length=10_000)
    information: tuple[ModelingInformation, ...] = Field(default=(), max_length=10_000)


class ModelingPlanPhase(StrEnum):
    REVIEWING_SEMANTICS = "reviewing_semantics"
    REVIEWING_DATASET = "reviewing_dataset"
    READY_FOR_VALIDATION = "ready_for_validation"
    BLOCKED = "blocked"


class ModelingPlanStatus(StrEnum):
    READY = "ready"
    APPLIED = "applied"


class DecisionChoice(FrozenModel):
    decision_id: str = Field(min_length=1, max_length=128)
    option_id: str = Field(min_length=1, max_length=64)


class ModelingPlan(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    input_hash: str = Field(min_length=1, max_length=128)
    contract_version: str = Field(default="product-plan-v1", min_length=1, max_length=64)
    phase: ModelingPlanPhase
    status: ModelingPlanStatus = ModelingPlanStatus.READY
    suggestion_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    queue: DecisionQueue
    created_at: datetime
    choices: tuple[DecisionChoice, ...] = ()
    resulting_revision_etag: int | None = Field(default=None, ge=1)
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=128)
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def review_audit_is_complete(self) -> ModelingPlan:
        review_values = (self.resulting_revision_etag, self.reviewed_by, self.reviewed_at)
        if self.status is ModelingPlanStatus.READY:
            if any(item is not None for item in review_values) or self.choices:
                raise ValueError("ready modeling plan cannot contain review results")
        else:
            if any(item is None for item in review_values):
                raise ValueError("applied modeling plan requires a complete review audit")
            if len(self.choices) != len(self.queue.decisions):
                raise ValueError("applied modeling plan requires every decision choice")
        return self


class ModelingPlanApplyResult(FrozenModel):
    plan: ModelingPlan
    revision: ModelingRevision


class ModelingResourceCounts(FrozenModel):
    models: int = Field(ge=0)
    fields: int = Field(ge=0)
    relations: int = Field(ge=0)
    metrics: int = Field(ge=0)
    dimensions: int = Field(ge=0)
    datasets: int = Field(ge=0)


class ModelingSummary(FrozenModel):
    project_id: str = Field(min_length=1, max_length=128)
    project_name: str = Field(min_length=1, max_length=256)
    stage: Literal[
        "selecting_data",
        "building_draft",
        "reviewing_decisions",
        "blocked",
        "verifying",
        "ready_to_publish",
        "published",
    ]
    active_release_id: str | None = Field(default=None, max_length=128)
    revision_id: str | None = Field(default=None, max_length=128)
    revision_etag: int | None = Field(default=None, ge=1)
    revision_state: str | None = Field(default=None, max_length=32)
    schema_snapshot_hash: str | None = Field(default=None, max_length=128)
    plan_id: str | None = Field(default=None, max_length=128)
    pending_confirmations: int = Field(default=0, ge=0)
    informational_items: int = Field(default=0, ge=0)
    counts: ModelingResourceCounts = ModelingResourceCounts(
        models=0,
        fields=0,
        relations=0,
        metrics=0,
        dimensions=0,
        datasets=0,
    )


class ScopeRecommendationBuilder:
    """Group only tables connected by real database foreign keys.

    Names/comments may later improve labels, but they must never add an edge or
    expand the selected table set.  This is deliberately more conservative than
    a generic similarity cluster because scope mistakes propagate to every later
    modeling stage.
    """

    def build(
        self,
        *,
        project_id: str,
        datasource_id: str,
        schema_name: str,
        tables: Iterable[TableSnapshot],
    ) -> ScopeRecommendationSet:
        table_items = tuple(
            sorted(
                (item for item in tables if item.schema_name == schema_name),
                key=lambda item: item.name,
            )
        )
        table_by_key = {(item.schema_name, item.name): item for item in table_items}
        neighbors: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        edges: set[tuple[tuple[str, str], tuple[str, str], str]] = set()
        for table in table_items:
            left = (table.schema_name, table.name)
            neighbors[left]
            for foreign_key in table.foreign_keys:
                right = (foreign_key.referred_schema, foreign_key.referred_table)
                if right not in table_by_key:
                    continue
                neighbors[left].add(right)
                neighbors[right].add(left)
                edge = tuple(sorted((left, right)))
                edges.add((edge[0], edge[1], foreign_key.name or "foreign_key"))

        groups: list[ScopeRecommendationGroup] = []
        visited: set[tuple[str, str]] = set()
        for start in sorted(neighbors):
            if start in visited:
                continue
            stack = [start]
            component: set[tuple[str, str]] = set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                visited.add(current)
                stack.extend(sorted(neighbors[current] - component, reverse=True))
            component_edges = tuple(
                item for item in sorted(edges) if item[0] in component and item[1] in component
            )
            table_names = tuple(sorted(item[1] for item in component))
            inference = (
                ScopeInference.DATABASE_CONSTRAINT
                if component_edges
                else ScopeInference.SCHEMA_ONLY
            )
            title = self._title(tuple(table_by_key[item] for item in sorted(component)))
            digest = content_hash(
                {
                    "datasource_id": datasource_id,
                    "schema_name": schema_name,
                    "tables": table_names,
                    "edges": component_edges,
                }
            )
            groups.append(
                ScopeRecommendationGroup(
                    id=f"scope_{digest.removeprefix('sha256:')[:20]}",
                    title=title,
                    schema_name=schema_name,
                    tables=table_names,
                    inference=inference,
                    foreign_key_count=len(component_edges),
                    evidence=tuple(
                        f"constraint:{name}:{left[1]}->{right[1]}"
                        for left, right, name in component_edges
                    ),
                )
            )
        groups.sort(key=lambda item: (-len(item.tables), item.tables))
        return ScopeRecommendationSet(
            project_id=project_id,
            datasource_id=datasource_id,
            schema_name=schema_name,
            total_table_count=len(table_items),
            groups=tuple(groups),
        )

    @staticmethod
    def _title(tables: tuple[TableSnapshot, ...]) -> str:
        if len(tables) == 1:
            title = tables[0].comment.strip() or tables[0].name
            return title[:256]
        commented = [item.comment.strip() for item in tables if item.comment.strip()]
        if commented:
            return "、".join(commented[:3])[:256]
        return "、".join(item.name for item in tables[:3])[:256]


class ModelingPlanBuilder:
    """Translate governed patches/resources into a server-owned decision queue."""

    def build(
        self,
        *,
        revision: ModelingRevision,
        project_name: str,
        suggestion_run: ModelingSuggestionRun | None = None,
    ) -> ModelingPlan:
        self._validate_run(revision, suggestion_run)
        revision_suggestions = tuple(
            item for item in revision.suggestions if item.state is SuggestionState.PENDING
        )
        run_suggestions = suggestion_run.suggestions if suggestion_run is not None else ()
        suggestions = (*revision_suggestions, *run_suggestions)
        input_hash = content_hash(
            {
                "contract_version": _MODELING_PLAN_CONTRACT_VERSION,
                "revision_id": revision.id,
                "revision_etag": revision.etag,
                "schema_snapshot_hash": revision.schema_snapshot_hash,
                "semantic_spec_hash": revision.semantic_spec.spec_hash,
                "suggestion_run_id": suggestion_run.id if suggestion_run else None,
                "suggestion_run_input_hash": suggestion_run.input_hash if suggestion_run else None,
            }
        )
        plan_id = f"plan_{input_hash.removeprefix('sha256:')[:24]}"
        decisions: list[ModelingDecision] = []
        information: list[ModelingInformation] = []
        reviewable: list[SuggestionPatch] = []
        for suggestion in suggestions:
            if (
                suggestion.source is SuggestionSource.DATABASE_CONSTRAINT
                and suggestion.target_kind != "relation"
            ):
                information.append(self._physical_information(suggestion))
            else:
                reviewable.append(suggestion)
        conflict_groups = self._conflicting_suggestion_groups(tuple(reviewable))
        conflict_ids = {item.id for group in conflict_groups for item in group}
        decisions.extend(self._suggestion_conflict(group) for group in conflict_groups)
        name_suggestions = tuple(
            item
            for item in reviewable
            if item.id not in conflict_ids
            if item.target_kind == "model"
            and set(item.changes).issubset({"name", "biz_name", "description", "aliases"})
        )
        if name_suggestions:
            decisions.append(self._semantic_name_batch(name_suggestions))
        name_ids = {item.id for item in name_suggestions}
        decisions.extend(
            self._suggestion_decision(item)
            for item in reviewable
            if item.id not in name_ids and item.id not in conflict_ids
        )

        if suggestions:
            phase = ModelingPlanPhase.REVIEWING_SEMANTICS
        elif not revision.semantic_spec.datasets:
            dataset = self._dataset_proposal(revision, project_name)
            if dataset is None:
                phase = ModelingPlanPhase.BLOCKED
                decisions.append(self._empty_scope_blocker())
            else:
                phase = ModelingPlanPhase.REVIEWING_DATASET
                decisions.append(self._dataset_decision(dataset))
        else:
            phase = ModelingPlanPhase.READY_FOR_VALIDATION

        queue = DecisionQueue(
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            plan_id=plan_id,
            summary=DecisionQueueSummary(
                blocking=sum(item.risk_level is DecisionRiskLevel.BLOCKING for item in decisions),
                needs_confirmation=len(decisions),
                informational=len(information),
            ),
            decisions=tuple(decisions),
            information=tuple(information),
        )
        return ModelingPlan(
            id=plan_id,
            project_id=revision.project_id,
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            input_hash=input_hash,
            contract_version=_MODELING_PLAN_CONTRACT_VERSION,
            phase=phase,
            suggestion_run_id=suggestion_run.id if suggestion_run else None,
            queue=queue,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def validate_choices(
        *,
        plan: ModelingPlan,
        revision: ModelingRevision,
        choices: Iterable[DecisionChoice],
    ) -> dict[str, DecisionChoice]:
        if (
            revision.id != plan.revision_id
            or revision.project_id != plan.project_id
            or revision.etag != plan.revision_etag
            or revision.schema_snapshot_hash != plan.schema_snapshot_hash
        ):
            raise RevisionConflictError("modeling plan is stale; generate a new plan")
        if plan.status is not ModelingPlanStatus.READY:
            raise RevisionConflictError("modeling plan was already applied")
        choice_items = tuple(choices)
        by_id = {item.decision_id: item for item in choice_items}
        if len(by_id) != len(choice_items):
            raise RevisionConflictError("duplicate modeling decision choices")
        expected = {item.id for item in plan.queue.decisions}
        if set(by_id) != expected:
            raise RevisionConflictError("every modeling decision requires one explicit choice")
        decisions = {item.id: item for item in plan.queue.decisions}
        for decision_id, choice in by_id.items():
            valid_options = {item.id for item in decisions[decision_id].options}
            if choice.option_id not in valid_options:
                raise RevisionConflictError(
                    f"unknown decision option for {decision_id}: {choice.option_id}"
                )
        return by_id

    @staticmethod
    def _validate_run(
        revision: ModelingRevision,
        suggestion_run: ModelingSuggestionRun | None,
    ) -> None:
        if suggestion_run is None:
            return
        if (
            suggestion_run.project_id != revision.project_id
            or suggestion_run.revision_id != revision.id
            or suggestion_run.revision_etag != revision.etag
            or suggestion_run.schema_snapshot_hash != revision.schema_snapshot_hash
        ):
            raise RevisionConflictError("modeling suggestion run is stale")
        if suggestion_run.status is not ModelingRunStatus.COMPLETED:
            raise RevisionConflictError("modeling suggestion run was already reviewed")

    @staticmethod
    def _physical_information(suggestion: SuggestionPatch) -> ModelingInformation:
        return ModelingInformation(
            id=_bounded_id("information", suggestion.id),
            title="已识别数据库物理约束",
            detail=suggestion.reason,
            affected_resource_ids=(suggestion.target_id,),
            source_suggestion_ids=(suggestion.id,),
        )

    @staticmethod
    def _semantic_name_batch(
        suggestions: tuple[SuggestionPatch, ...],
    ) -> ModelingDecision:
        digest = content_hash({"suggestion_ids": sorted(item.id for item in suggestions)})
        return ModelingDecision(
            id=f"decision:names:{digest.removeprefix('sha256:')[:20]}",
            kind="semantic_name_batch",
            risk_level=DecisionRiskLevel.PRESENTATION,
            title="确认业务名称与说明",
            proposal=f"采用 {len(suggestions)} 项业务名称和说明建议",
            reason="这些内容用于业务理解与自然语言匹配，不改变物理表和字段",
            options=(
                ModelingDecisionOption(id="accept", label="采用这组建议", recommended=True),
                ModelingDecisionOption(id="reject", label="暂不采用"),
            ),
            affected_resource_ids=tuple(item.target_id for item in suggestions),
            source_suggestion_ids=tuple(item.id for item in suggestions),
            write_commands_preview=("suggestion.apply",),
        )

    @staticmethod
    def _conflicting_suggestion_groups(
        suggestions: tuple[SuggestionPatch, ...],
    ) -> tuple[tuple[SuggestionPatch, ...], ...]:
        by_target: dict[tuple[str, str], list[SuggestionPatch]] = defaultdict(list)
        for item in suggestions:
            by_target[(item.target_kind, item.target_id)].append(item)
        groups: list[tuple[SuggestionPatch, ...]] = []
        for target in sorted(by_target):
            items = tuple(sorted(by_target[target], key=lambda item: item.id))
            neighbors: dict[str, set[str]] = {item.id: set() for item in items}
            by_id = {item.id: item for item in items}
            for index, left in enumerate(items):
                for right in items[index + 1 :]:
                    shared = set(left.changes).intersection(right.changes)
                    if shared and any(left.changes[key] != right.changes[key] for key in shared):
                        neighbors[left.id].add(right.id)
                        neighbors[right.id].add(left.id)
            visited: set[str] = set()
            for start in sorted(neighbors):
                if start in visited or not neighbors[start]:
                    continue
                stack = [start]
                component: set[str] = set()
                while stack:
                    current = stack.pop()
                    if current in component:
                        continue
                    component.add(current)
                    visited.add(current)
                    stack.extend(sorted(neighbors[current] - component, reverse=True))
                groups.append(tuple(by_id[item_id] for item_id in sorted(component)))
        return tuple(groups)

    @staticmethod
    def _suggestion_conflict(
        suggestions: tuple[SuggestionPatch, ...],
    ) -> ModelingDecision:
        digest = content_hash({"suggestion_ids": [item.id for item in suggestions]})
        options = tuple(
            ModelingDecisionOption(
                id=f"proposal:{index}",
                label=f"采用方案 {index}：{_proposal_text(item.changes)[:220]}",
            )
            for index, item in enumerate(suggestions, start=1)
        ) + (
            ModelingDecisionOption(
                id="reject_all",
                label="全部暂不采用",
                description="保留物理字段，但不应用这组互斥语义",
            ),
        )
        return ModelingDecision(
            id=f"decision:conflict:{digest.removeprefix('sha256:')[:20]}",
            kind="suggestion_conflict",
            risk_level=DecisionRiskLevel.BLOCKING,
            title="同一数据项存在互斥建议",
            proposal=f"检测到 {len(suggestions)} 个不能同时生效的方案",
            reason="多个建议修改了同一语义属性，系统不会按生成顺序静默覆盖",
            options=options,
            affected_resource_ids=(suggestions[0].target_id,),
            source_suggestion_ids=tuple(item.id for item in suggestions),
            write_commands_preview=("suggestion.apply",),
            option_accepts_suggestion_ids={
                **{
                    f"proposal:{index}": (item.id,)
                    for index, item in enumerate(suggestions, start=1)
                },
                "reject_all": (),
            },
        )

    @staticmethod
    def _suggestion_decision(suggestion: SuggestionPatch) -> ModelingDecision:
        kind: ModelingDecisionKind
        if suggestion.target_kind == "model":
            kind = "semantic_name"
            title = "确认业务数据名称"
        elif suggestion.target_kind == "relation":
            kind = "relation_cardinality"
            title = "确认两类数据的关联方式"
        elif suggestion.changes.get("kind") == "measure" or "aggregation" in suggestion.changes:
            kind = "measure_aggregation"
            title = "确认数值字段的计算方式"
        else:
            kind = "field_classification"
            title = "确认字段在问数中的用途"
        risk = (
            DecisionRiskLevel.EXECUTION
            if suggestion.high_impact
            or suggestion.target_kind == "relation"
            or kind in {"field_classification", "measure_aggregation"}
            else DecisionRiskLevel.PRESENTATION
        )
        options = ModelingPlanBuilder._suggestion_options(
            kind=kind,
            suggestion=suggestion,
        )
        return ModelingDecision(
            id=_bounded_id("decision", suggestion.id),
            kind=kind,
            risk_level=risk,
            title=title,
            proposal=_proposal_text(suggestion.changes),
            reason=suggestion.reason,
            options=options,
            affected_resource_ids=(suggestion.target_id,),
            source_suggestion_ids=(suggestion.id,),
            write_commands_preview=("suggestion.apply",),
        )

    @staticmethod
    def _suggestion_options(
        *,
        kind: ModelingDecisionKind,
        suggestion: SuggestionPatch,
    ) -> tuple[ModelingDecisionOption, ...]:
        if kind == "relation_cardinality":
            proposed = str(suggestion.changes.get("cardinality") or "")
            labels = {
                "many_to_one": "多条左侧数据对应一条右侧数据",
                "one_to_one": "左右一一对应",
                "one_to_many": "一条左侧数据对应多条右侧数据",
            }
            return (
                *(
                    ModelingDecisionOption(
                        id=f"cardinality:{value}",
                        label=label,
                        recommended=value == proposed,
                    )
                    for value, label in labels.items()
                ),
                ModelingDecisionOption(
                    id="exclude",
                    label="不允许关联",
                    description="保留单表问数，不创建这条跨表 Join",
                ),
            )
        if kind == "measure_aggregation":
            proposed = str(suggestion.changes.get("aggregation") or "")
            return (
                *(
                    ModelingDecisionOption(
                        id=f"aggregation:{value}",
                        label=label,
                        recommended=(value == proposed and suggestion.confidence >= 0.8),
                    )
                    for value, label in (
                        ("sum", "求和"),
                        ("count", "计数"),
                        ("count_distinct", "去重计数"),
                        ("avg", "平均值"),
                        ("min", "最小值"),
                        ("max", "最大值"),
                    )
                ),
                ModelingDecisionOption(
                    id="exclude",
                    label="暂不开放为指标",
                    description="字段仍保留，但不用于聚合计算",
                ),
            )
        return (
            ModelingDecisionOption(id="accept", label="采用建议", recommended=True),
            ModelingDecisionOption(
                id="reject",
                label="暂不采用",
                description="不确定的执行语义不会进入当前问数模型",
            ),
        )

    @staticmethod
    def _dataset_proposal(
        revision: ModelingRevision,
        project_name: str,
    ) -> DatasetSpec | None:
        spec = revision.semantic_spec
        if not spec.models or (not spec.metrics and not spec.dimensions):
            return None
        return DatasetSpec(
            id=stable_id("dataset", revision.project_id, revision.id),
            name=project_name,
            model_ids=tuple(item.id for item in spec.models),
            metric_ids=tuple(item.id for item in spec.metrics),
            dimension_ids=tuple(item.id for item in spec.dimensions),
        )

    @staticmethod
    def _dataset_decision(dataset: DatasetSpec) -> ModelingDecision:
        return ModelingDecision(
            id=_bounded_id("decision", dataset.id),
            kind="dataset_scope",
            risk_level=DecisionRiskLevel.EXECUTION,
            title="确认可以被提问的数据范围",
            proposal=(
                f"开放 {len(dataset.metric_ids)} 个指标、{len(dataset.dimension_ids)} 个维度，"
                f"使用 {len(dataset.model_ids)} 个数据模型"
            ),
            reason="问数范围限制运行时可使用的指标、维度和模型",
            options=(
                ModelingDecisionOption(id="accept", label="确认开放", recommended=True),
                ModelingDecisionOption(
                    id="reject",
                    label="暂不开放",
                    description="当前草稿将不能进入问数验证",
                ),
            ),
            affected_resource_ids=(
                dataset.id,
                *dataset.model_ids,
            ),
            write_commands_preview=("dataset.upsert",),
            proposed_resource=dataset.model_dump(mode="json"),
        )

    @staticmethod
    def _empty_scope_blocker() -> ModelingDecision:
        return ModelingDecision(
            id="decision:empty-query-scope",
            kind="validation_blocker",
            risk_level=DecisionRiskLevel.BLOCKING,
            title="当前没有可开放的问数内容",
            proposal="",
            reason="至少需要一个已确认的指标或维度，才能创建问数范围",
            options=(ModelingDecisionOption(id="open_advanced", label="进入高级设置补充"),),
            write_commands_preview=(),
        )


def _bounded_id(prefix: str, raw: str) -> str:
    value = f"{prefix}:{raw}"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _proposal_text(changes: dict[str, Any]) -> str:
    return "；".join(f"{key}={value}" for key, value in sorted(changes.items()))[:2_000]
