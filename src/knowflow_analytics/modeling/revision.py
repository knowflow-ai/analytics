from __future__ import annotations

from collections.abc import Iterable, Sequence

from knowflow_analytics.contracts import (
    FieldKind,
    MetricKind,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import semantic_release_hash
from knowflow_analytics.modeling.ai_artifacts import validate_ai_modeling_completeness
from knowflow_analytics.modeling.analysis_topics import validate_analysis_topic_route
from knowflow_analytics.modeling.catalog_compiler import (
    compile_semantic_catalog,
    validate_m0_publishable,
)
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog
from knowflow_analytics.modeling.catalog_editor import apply_catalog_suggestion
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    RevisionState,
    SuggestionDecision,
    SuggestionPatch,
    SuggestionSource,
    SuggestionState,
    semantic_context_content_hash,
)
from knowflow_analytics.modeling.type_system import aggregation_accepts_type, types_can_join


class RevisionConflictError(SemanticValidationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="REVISION_CONFLICT")


def unprocessed_suggestions(
    generated: Sequence[SuggestionPatch],
    existing: Sequence[SuggestionPatch],
) -> tuple[SuggestionPatch, ...]:
    """Drop suggestions the user already accepted, rejected or resolved.

    Suggestion ids are deliberately stable (revision + model + output hash), so a
    re-run produces the same ids and ``apply_suggestion_run`` rejects it as a
    duplicate application. That guard is correct — applying the same patch twice
    must not silently succeed — but it made re-running the AI a hard error. The
    fix belongs at generation time: only offer what the user has not settled yet.
    Anything already staged on the revision is dropped regardless of state,
    because the downstream duplicate check keys on id alone.
    """

    # 按全部已存在 id 过滤，而不只是已决策的：apply_suggestion_run 的重复判定
    # 同样不看 state（对整个 revision.suggestions 取 id 交集），放行一条已暂存的
    # PENDING 建议会让整批以 REVISION_CONFLICT 失败。
    staged = {item.id for item in existing}
    return tuple(item for item in generated if item.id not in staged)


class RevisionEditor:
    def create(
        self,
        *,
        project_id: str,
        schema_snapshot_hash: str,
        semantic_spec: SemanticRelease | None = None,
        semantic_catalog: SemanticCatalog | None = None,
        suggestions: Iterable[SuggestionPatch] = (),
        parent_revision_id: str | None = None,
        modeling_job_id: str | None = None,
    ) -> ModelingRevision:
        if semantic_catalog is not None:
            semantic_spec = compile_semantic_catalog(semantic_catalog)
        if semantic_spec is None:
            raise RevisionConflictError("revision requires a semantic catalog or projection")
        if semantic_spec.project_id != project_id:
            raise RevisionConflictError("semantic spec belongs to another project")
        return ModelingRevision(
            id=semantic_spec.revision_id or semantic_spec.id,
            project_id=project_id,
            schema_snapshot_hash=schema_snapshot_hash,
            etag=1,
            semantic_spec=semantic_spec,
            semantic_catalog=semantic_catalog,
            suggestions=tuple(suggestions),
            parent_revision_id=parent_revision_id,
            modeling_job_id=modeling_job_id,
        )

    def replace_semantic_catalog(
        self,
        revision: ModelingRevision,
        *,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        semantic_catalog: SemanticCatalog,
        suggestions: Iterable[SuggestionPatch] | None = None,
    ) -> ModelingRevision:
        """Atomically replace the governed catalog and its query projection."""

        self._check_version(revision, expected_etag, expected_schema_snapshot_hash)
        self._require_editable(revision)
        if semantic_catalog.project_id != revision.project_id:
            raise RevisionConflictError("modeling catalog belongs to another project")
        if semantic_catalog.revision_id != revision.id:
            raise RevisionConflictError("modeling catalog belongs to another revision")
        semantic_spec = compile_semantic_catalog(semantic_catalog)
        # Governed DataSet save/update behavior:
        # model-scoped resources with the same display name remain distinct by
        # ID and model. Its conflictCheck exists but is not invoked by save and
        # is commented out in update. Query mapping must resolve that ambiguity
        # from model context or ask for clarification, not reject the catalog.
        next_suggestions = revision.suggestions if suggestions is None else tuple(suggestions)
        self._validate_targets(semantic_spec, next_suggestions)
        previous_context_by_id = {
            item.id: item for item in revision.semantic_spec.semantic_context
        }
        context_is_reviewed_subset = bool(semantic_spec.semantic_context) and all(
            previous_context_by_id.get(item.id) == item
            for item in semantic_spec.semantic_context
        )
        preserve_context_review = (
            revision.semantic_context_review_hash is not None
            and context_is_reviewed_subset
        )
        return ModelingRevision(
            id=revision.id,
            project_id=revision.project_id,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            etag=revision.etag + 1,
            state=RevisionState.DRAFT,
            semantic_spec=semantic_spec,
            semantic_catalog=semantic_catalog,
            suggestions=next_suggestions,
            parent_revision_id=revision.parent_revision_id,
            modeling_job_id=revision.modeling_job_id,
            ai_modeling_artifact_hash=revision.ai_modeling_artifact_hash,
            ai_alias_reviewed_resources=revision.ai_alias_reviewed_resources,
            semantic_context_review_hash=(
                semantic_context_content_hash(semantic_spec.semantic_context)
                if preserve_context_review
                else None
            ),
            semantic_context_reviewed_by=(
                revision.semantic_context_reviewed_by
                if preserve_context_review
                else None
            ),
            semantic_context_reviewed_at=(
                revision.semantic_context_reviewed_at
                if preserve_context_review
                else None
            ),
        )

    def add_suggestions(
        self,
        revision: ModelingRevision,
        *,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        suggestions: Iterable[SuggestionPatch],
    ) -> ModelingRevision:
        self._check_version(revision, expected_etag, expected_schema_snapshot_hash)
        self._require_editable(revision)
        existing = {item.id for item in revision.suggestions}
        additions = tuple(suggestions)
        duplicate = existing.intersection(item.id for item in additions)
        if duplicate:
            raise RevisionConflictError(f"duplicate suggestion ids: {sorted(duplicate)}")
        self._validate_targets(revision.semantic_spec, additions)
        return revision.model_copy(
            update={
                "etag": revision.etag + 1,
                "state": RevisionState.DRAFT,
                "suggestions": revision.suggestions + additions,
            }
        )

    def apply_suggestion_run(
        self,
        revision: ModelingRevision,
        *,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        suggestions: Iterable[SuggestionPatch],
        decisions: Iterable[SuggestionDecision],
    ) -> ModelingRevision:
        """Apply a separately stored AI run after a complete human review."""

        self._check_version(revision, expected_etag, expected_schema_snapshot_hash)
        self._require_editable(revision)
        suggestion_items = tuple(suggestions)
        decision_items = tuple(decisions)
        suggestion_ids = {item.id for item in suggestion_items}
        decision_ids = {item.suggestion_id for item in decision_items}
        if len(suggestion_ids) != len(suggestion_items):
            raise RevisionConflictError("suggestion run contains duplicate identifiers")
        if len(decision_ids) != len(decision_items):
            raise RevisionConflictError("duplicate suggestion decisions")
        if not suggestion_ids.issubset(decision_ids):
            raise RevisionConflictError(
                "every AI suggestion must be explicitly accepted or rejected"
            )
        existing_ids = {item.id for item in revision.suggestions}
        duplicate = existing_ids.intersection(suggestion_ids)
        if duplicate:
            raise RevisionConflictError(f"suggestion run was already applied: {sorted(duplicate)}")
        self._validate_targets(revision.semantic_spec, suggestion_items)
        staged = revision.model_copy(
            update={"suggestions": (*revision.suggestions, *suggestion_items)}
        )
        return self.apply_decisions(
            staged,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
            decisions=decision_items,
        )

    def apply_decisions(
        self,
        revision: ModelingRevision,
        *,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
        decisions: Iterable[SuggestionDecision],
    ) -> ModelingRevision:
        self._check_version(revision, expected_etag, expected_schema_snapshot_hash)
        self._require_editable(revision)
        decision_items = tuple(decisions)
        decisions_by_id = {decision.suggestion_id: decision for decision in decision_items}
        if len(decisions_by_id) != len(decision_items):
            raise RevisionConflictError("duplicate suggestion decisions")
        suggestions_by_id = {item.id: item for item in revision.suggestions}
        constrained_field_kinds = {
            item.target_id: item.changes["kind"]
            for item in revision.suggestions
            if item.target_kind == "field" and item.source is SuggestionSource.DATABASE_CONSTRAINT
        }
        constrained_field_changes = {
            item.target_id: item.changes
            for item in revision.suggestions
            if item.target_kind == "field" and item.source is SuggestionSource.DATABASE_CONSTRAINT
        }
        unknown = set(decisions_by_id) - set(suggestions_by_id)
        if unknown:
            raise RevisionConflictError(f"unknown suggestion decisions: {sorted(unknown)}")

        if revision.semantic_catalog is None:
            raise RevisionConflictError("revision has no authoritative modeling catalog")
        semantic_catalog = revision.semantic_catalog
        updated_suggestions: list[SuggestionPatch] = []
        for suggestion in revision.suggestions:
            decision = decisions_by_id.get(suggestion.id)
            if decision is None:
                updated_suggestions.append(suggestion)
                continue
            if suggestion.state is not SuggestionState.PENDING:
                raise RevisionConflictError(f"suggestion was already decided: {suggestion.id}")
            if decision.accept:
                constrained_kind = constrained_field_kinds.get(suggestion.target_id)
                override_kind = decision.overrides.get("kind")
                if (
                    constrained_kind is not None
                    and override_kind is not None
                    and override_kind != constrained_kind
                ):
                    raise RevisionConflictError(
                        f"database constraint classification cannot be overridden: "
                        f"{suggestion.target_id}"
                    )
                merged_changes = dict(suggestion.changes)
                merged_changes.update(decision.overrides)
                if constrained_kind is not None:
                    merged_changes["kind"] = constrained_kind
                    merged_changes["identifier_type"] = constrained_field_changes[
                        suggestion.target_id
                    ]["identifier_type"]
                    merged_changes.pop("dimension_type", None)
                    merged_changes.pop("unit", None)
                    merged_changes["create_dimension"] = True
                    merged_changes["create_metric"] = False
                    if constrained_kind != FieldKind.MEASURE.value:
                        merged_changes.pop("aggregation", None)
                accepted = suggestion.model_copy(
                    update={"changes": merged_changes, "state": SuggestionState.ACCEPTED}
                )
                accepted = SuggestionPatch.model_validate(accepted.model_dump(mode="python"))
                semantic_catalog = apply_catalog_suggestion(semantic_catalog, accepted)
                updated_suggestions.append(accepted)
            else:
                if decision.overrides:
                    raise RevisionConflictError("rejected suggestion cannot contain overrides")
                if (
                    suggestion.target_kind == "field"
                    and suggestion.source is SuggestionSource.DATABASE_CONSTRAINT
                ):
                    raise RevisionConflictError(
                        f"database constraint classification cannot be rejected: "
                        f"{suggestion.target_id}"
                    )
                updated_suggestions.append(
                    suggestion.model_copy(update={"state": SuggestionState.REJECTED})
                )

        return self.replace_semantic_catalog(
            revision,
            expected_etag=expected_etag,
            expected_schema_snapshot_hash=expected_schema_snapshot_hash,
            semantic_catalog=semantic_catalog,
            suggestions=tuple(updated_suggestions),
        )

    def validate_for_publish(self, revision: ModelingRevision) -> ModelingRevision:
        self._require_editable(revision)
        pending = [
            item.id for item in revision.suggestions if item.state is SuggestionState.PENDING
        ]
        if pending:
            raise SemanticValidationError(
                f"unreviewed modeling suggestions remain: {pending[:5]}",
                code="UNREVIEWED_SUGGESTIONS",
            )
        if revision.semantic_spec.semantic_context and (
            revision.semantic_context_review_hash is None
            or revision.semantic_context_reviewed_by is None
            or revision.semantic_context_reviewed_at is None
        ):
            raise SemanticValidationError(
                "semantic context requires an artifact-bound human review",
                code="UNREVIEWED_SEMANTIC_CONTEXT",
            )
        if revision.semantic_catalog is not None:
            validate_m0_publishable(revision.semantic_catalog)
            compiled = compile_semantic_catalog(revision.semantic_catalog)
            # 与 catalog_projection_is_bound 同一原则:存量投影是老合同序列化的,
            # 比较前必须过同一合同归一化。否则每次给目录合同加字段(hierarchies、
            # aggTimeDimensionId),所有存量 revision 都会在这里报假漂移,一律不能
            # 发布/评测(2026-08-25 实际发生)。spec_hash 是 modeling_catalog 的
            # 下游,归一化后按同一函数重算再比。
            stored = revision.semantic_spec
            if stored.modeling_catalog is not None:
                normalized_catalog = SemanticCatalog.model_validate(
                    stored.modeling_catalog
                ).canonical_payload()
                stored = stored.model_copy(update={"modeling_catalog": normalized_catalog})
                stored = stored.model_copy(update={"spec_hash": semantic_release_hash(stored)})
            if compiled != stored:
                raise SemanticValidationError(
                    "modeling catalog and query projection are inconsistent",
                    code="MODELING_PROJECTION_DRIFT",
                )
        SemanticRelease.model_validate(revision.semantic_spec.model_dump(mode="python"))
        self._validate_constraint_classification(revision, revision.semantic_spec)
        self._validate_queryable_scope(revision.semantic_spec)
        self._validate_metric_field_types(revision.semantic_spec)
        self._validate_relation_field_types(revision.semantic_spec)
        if revision.semantic_spec.analysis_topic_routes:
            routed = {item.dataset_id for item in revision.semantic_spec.analysis_topic_routes}
            missing_routes = {item.id for item in revision.semantic_spec.datasets} - routed
            if missing_routes:
                raise SemanticValidationError(
                    f"analysis topics require frozen routes: {sorted(missing_routes)}",
                    code="ANALYSIS_TOPIC_ROUTE_REQUIRED",
                )
        for route in revision.semantic_spec.analysis_topic_routes:
            validate_analysis_topic_route(revision.semantic_spec, route)
        if revision.ai_modeling_artifact_hash is not None:
            validate_ai_modeling_completeness(
                revision.semantic_spec,
                alias_reviewed_resources=revision.ai_alias_reviewed_resources,
            )
        if revision.state is RevisionState.VALIDATED:
            return revision
        return revision.model_copy(
            update={"etag": revision.etag + 1, "state": RevisionState.VALIDATED}
        )

    @staticmethod
    def _require_editable(revision: ModelingRevision) -> None:
        if revision.state in {RevisionState.FROZEN, RevisionState.PUBLISHED}:
            raise RevisionConflictError(f"{revision.state.value} revisions are immutable")

    @staticmethod
    def _check_version(
        revision: ModelingRevision,
        expected_etag: int,
        expected_schema_snapshot_hash: str,
    ) -> None:
        if revision.etag != expected_etag:
            raise RevisionConflictError("revision etag changed; reload before applying decisions")
        if revision.schema_snapshot_hash != expected_schema_snapshot_hash:
            raise RevisionConflictError("schema snapshot changed; suggestions cannot be applied")

    @staticmethod
    def _validate_targets(spec: SemanticRelease, suggestions: Iterable[SuggestionPatch]) -> None:
        models = {item.id for item in spec.models}
        fields = {item.id for item in spec.fields}
        for suggestion in suggestions:
            if suggestion.target_kind == "model" and suggestion.target_id not in models:
                raise RevisionConflictError(f"unknown suggestion model: {suggestion.target_id}")
            if suggestion.target_kind == "field" and suggestion.target_id not in fields:
                raise RevisionConflictError(f"unknown suggestion field: {suggestion.target_id}")

    @staticmethod
    def _validate_physical_facts(current: SemanticRelease, proposed: SemanticRelease) -> None:
        if proposed.project_id != current.project_id:
            raise RevisionConflictError("semantic spec belongs to another project")
        current_models = {item.id: (item.schema_name, item.table) for item in current.models}
        proposed_models = {item.id: (item.schema_name, item.table) for item in proposed.models}
        if proposed_models != current_models:
            raise RevisionConflictError("model physical sources differ from the schema snapshot")
        current_fields = {
            item.id: (item.model_id, item.column, item.data_type, item.nullable)
            for item in current.fields
        }
        proposed_fields = {
            item.id: (item.model_id, item.column, item.data_type, item.nullable)
            for item in proposed.fields
        }
        if proposed_fields != current_fields:
            raise RevisionConflictError("field physical mappings differ from the schema snapshot")

    @staticmethod
    def _validate_constraint_classification(
        revision: ModelingRevision,
        proposed: SemanticRelease,
    ) -> None:
        proposed_fields = {item.id: item for item in proposed.fields}
        for suggestion in revision.suggestions:
            if (
                suggestion.target_kind != "field"
                or suggestion.source is not SuggestionSource.DATABASE_CONSTRAINT
            ):
                continue
            field = proposed_fields.get(suggestion.target_id)
            if field is None:
                raise RevisionConflictError(
                    f"database-constrained field disappeared: {suggestion.target_id}"
                )
            expected_kind = FieldKind(suggestion.changes["kind"])
            if field.kind is not expected_kind:
                raise RevisionConflictError(
                    f"database constraint classification cannot be overridden: "
                    f"{suggestion.target_id}"
                )
            if field.identifier_type != suggestion.changes.get("identifier_type"):
                raise RevisionConflictError(
                    f"database identifier subtype cannot be overridden: {suggestion.target_id}"
                )
            if not field.create_dimension or field.create_metric:
                raise RevisionConflictError(
                    f"database identifier must remain a governed dimension: {suggestion.target_id}"
                )

    @staticmethod
    def _validate_queryable_scope(spec: SemanticRelease) -> None:
        if not spec.models:
            raise SemanticValidationError(
                "semantic revision has no selected models",
                code="MODELING_SCOPE_EMPTY",
            )
        if not spec.datasets:
            raise SemanticValidationError(
                "semantic revision requires an explicitly configured dataset",
                code="DATASET_REQUIRED",
            )
        empty = [
            dataset.id
            for dataset in spec.datasets
            if not dataset.metric_ids and not dataset.dimension_ids
        ]
        if empty:
            raise SemanticValidationError(
                f"datasets expose no governed metrics or dimensions: {empty[:5]}",
                code="DATASET_EMPTY",
            )

    @staticmethod
    def _validate_metric_field_types(spec: SemanticRelease) -> None:
        fields = {item.id: item for item in spec.fields}
        for metric in spec.metrics:
            if metric.kind is not MetricKind.ATOMIC:
                continue
            field = fields[metric.field_id]  # reference validity is checked by SemanticRelease
            aggregation = metric.aggregation
            if aggregation is None or aggregation_accepts_type(aggregation, field.data_type):
                continue
            raise SemanticValidationError(
                f"metric {metric.id} cannot apply {aggregation.value} to "
                f"field type {field.data_type}",
                code="INVALID_METRIC_AGGREGATION_TYPE",
            )

    @staticmethod
    def _validate_relation_field_types(spec: SemanticRelease) -> None:
        fields = {item.id: item for item in spec.fields}
        for relation in spec.relations:
            for condition in relation.conditions:
                left = fields[condition.left_field_id]
                right = fields[condition.right_field_id]
                if types_can_join(left.data_type, right.data_type):
                    continue
                raise SemanticValidationError(
                    f"relation {relation.id} joins incompatible field types "
                    f"{left.data_type} and {right.data_type}",
                    code="INVALID_RELATION_FIELD_TYPES",
                )
