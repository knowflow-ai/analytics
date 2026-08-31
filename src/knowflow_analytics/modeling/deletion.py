from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.ai_artifacts import reconcile_query_scopes
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetDetailContract,
    HierarchyContract,
    QueryRuleContract,
    SemanticCatalog,
)


class ResourceKind(StrEnum):
    MODEL = "model"
    RELATION = "relation"
    DIMENSION = "dimension"
    METRIC = "metric"
    DATASET = "dataset"
    TERM = "term"
    DIMENSION_VALUE = "dimension_value"
    SEMANTIC_CONTEXT = "semantic_context"
    HIERARCHY = "hierarchy"
    QUERY_RULE = "query_rule"


class DeletionEffect(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str = Field(pattern="^(delete|unlink)$")
    resource_kind: ResourceKind
    resource_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


class DeletionImpact(BaseModel):
    model_config = ConfigDict(frozen=True)

    resource_kind: ResourceKind
    resource_id: str
    source_catalog_hash: str
    impact_hash: str
    requires_confirmation: bool = True
    effects: tuple[DeletionEffect, ...]


class CatalogDeletionPlanner:
    """Preview and atomically apply dependency-safe catalog deletion.

    Deleting soft-deletes the selected resource and cascades to model-owned
    metrics and dimensions, and every catalog reference is normalized before
    the mutation is accepted. The exact normalized effect
    set is hashed, so a stale confirmation cannot delete a changed graph.
    """

    def preview(
        self,
        catalog: SemanticCatalog,
        *,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> DeletionImpact:
        _, effects = self._plan(
            catalog,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        source_hash = content_hash(catalog.canonical_payload())
        payload = {
            "resource_kind": resource_kind.value,
            "resource_id": resource_id,
            "source_catalog_hash": source_hash,
            "effects": [item.model_dump(mode="json") for item in effects],
        }
        return DeletionImpact(
            resource_kind=resource_kind,
            resource_id=resource_id,
            source_catalog_hash=source_hash,
            impact_hash=content_hash(payload),
            effects=effects,
        )

    def apply(
        self,
        catalog: SemanticCatalog,
        *,
        resource_kind: ResourceKind,
        resource_id: str,
        expected_impact_hash: str,
    ) -> SemanticCatalog:
        impact = self.preview(
            catalog,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        if impact.impact_hash != expected_impact_hash:
            raise SemanticValidationError(
                "catalog changed after deletion impact review",
                code="DELETION_IMPACT_CHANGED",
            )
        updated, _ = self._plan(
            catalog,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        return updated

    def _plan(
        self,
        catalog: SemanticCatalog,
        *,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> tuple[SemanticCatalog, tuple[DeletionEffect, ...]]:
        self._require_resource(catalog, resource_kind, resource_id)
        compiler_owned_dataset_ids = {item.dataset_id for item in catalog.analysis_topic_routes}
        if resource_kind is ResourceKind.DATASET and resource_id in compiler_owned_dataset_ids:
            raise SemanticValidationError(
                "compiler-owned QueryScope cannot be deleted directly; edit its fact root instead",
                code="DERIVED_QUERY_SCOPE_IMMUTABLE",
            )
        deleted_models = {resource_id} if resource_kind is ResourceKind.MODEL else set()
        deleted_relations = {resource_id} if resource_kind is ResourceKind.RELATION else set()
        deleted_dimensions = {resource_id} if resource_kind is ResourceKind.DIMENSION else set()
        deleted_metrics = {resource_id} if resource_kind is ResourceKind.METRIC else set()
        deleted_datasets = {resource_id} if resource_kind is ResourceKind.DATASET else set()
        deleted_terms = {resource_id} if resource_kind is ResourceKind.TERM else set()
        deleted_contexts = (
            {resource_id} if resource_kind is ResourceKind.SEMANTIC_CONTEXT else set()
        )
        deleted_hierarchies = {resource_id} if resource_kind is ResourceKind.HIERARCHY else set()
        deleted_query_rules = {resource_id if resource_kind is ResourceKind.QUERY_RULE else ""} - {
            ""
        }

        if deleted_models:
            deleted_relations.update(
                item.id
                for item in catalog.model_relations
                if item.from_model_id in deleted_models or item.to_model_id in deleted_models
            )
            deleted_dimensions.update(
                item.id for item in catalog.dimensions if item.model_id in deleted_models
            )
            deleted_metrics.update(
                item.id for item in catalog.metrics if item.model_id in deleted_models
            )

        # MetricDefineType.METRIC records explicit dependency IDs.
        # Delete their transitive dependants rather than leave an invalid formula.
        changed = True
        while changed:
            changed = False
            for metric in catalog.metrics:
                params = metric.metric_define_by_metric_params
                dependencies = {item.id for item in params.metrics} if params else set()
                if metric.id not in deleted_metrics and dependencies.intersection(deleted_metrics):
                    deleted_metrics.add(metric.id)
                    changed = True

        effects: list[DeletionEffect] = [
            DeletionEffect(
                action="delete",
                resource_kind=resource_kind,
                resource_id=resource_id,
                reason="用户请求删除",
            )
        ]
        initial = {(resource_kind, resource_id)}
        for kind, identifiers, reason in (
            (ResourceKind.RELATION, deleted_relations, "关联模型被删除"),
            (ResourceKind.DIMENSION, deleted_dimensions, "所属模型被删除"),
            (ResourceKind.METRIC, deleted_metrics, "所属模型或依赖指标被删除"),
        ):
            effects.extend(
                DeletionEffect(
                    action="delete",
                    resource_kind=kind,
                    resource_id=item_id,
                    reason=reason,
                )
                for item_id in sorted(identifiers)
                if (kind, item_id) not in initial
            )

        models = tuple(item for item in catalog.models if item.id not in deleted_models)
        relations = tuple(
            item for item in catalog.model_relations if item.id not in deleted_relations
        )
        dimensions = tuple(item for item in catalog.dimensions if item.id not in deleted_dimensions)
        normalized_models = list(models)
        metrics = [item for item in catalog.metrics if item.id not in deleted_metrics]

        data_sets = []
        for data_set in catalog.data_sets:
            if data_set.id in deleted_datasets:
                continue
            configs = []
            changed_config = False
            for config in data_set.data_set_detail.data_set_model_configs:
                if config.id in deleted_models:
                    changed_config = True
                    continue
                metric_ids = tuple(item for item in config.metrics if item not in deleted_metrics)
                dimension_ids = tuple(
                    item for item in config.dimensions if item not in deleted_dimensions
                )
                if metric_ids != config.metrics or dimension_ids != config.dimensions:
                    changed_config = True
                if not config.includes_all and not metric_ids and not dimension_ids:
                    changed_config = True
                    continue
                configs.append(
                    config.model_copy(update={"metrics": metric_ids, "dimensions": dimension_ids})
                )
            if not configs:
                deleted_datasets.add(data_set.id)
                effects.append(
                    DeletionEffect(
                        action="delete",
                        resource_kind=ResourceKind.DATASET,
                        resource_id=data_set.id,
                        reason="删除依赖后数据集已无可问范围",
                    )
                )
                continue
            if changed_config:
                effects.append(
                    DeletionEffect(
                        action="unlink",
                        resource_kind=ResourceKind.DATASET,
                        resource_id=data_set.id,
                        reason="从数据集范围移除已删除资源",
                    )
                )
            data_sets.append(
                data_set.model_copy(
                    update={
                        "data_set_detail": DataSetDetailContract(
                            data_set_model_configs=tuple(configs)
                        )
                    }
                )
            )

        deleted_contexts.update(
            entry.id
            for entry in catalog.semantic_context
            if (
                (entry.target_type == "model" and entry.target_id in deleted_models)
                or (entry.target_type == "metric" and entry.target_id in deleted_metrics)
                or (entry.target_type == "dimension" and entry.target_id in deleted_dimensions)
                or (entry.target_type == "query_scope" and entry.target_id in deleted_datasets)
            )
        )
        semantic_context = tuple(
            entry for entry in catalog.semantic_context if entry.id not in deleted_contexts
        )
        effects.extend(
            DeletionEffect(
                action="delete",
                resource_kind=ResourceKind.SEMANTIC_CONTEXT,
                resource_id=context_id,
                reason="删除语义目标时清理上下文",
            )
            for context_id in sorted(deleted_contexts)
            if (ResourceKind.SEMANTIC_CONTEXT, context_id) not in initial
        )

        terms = []
        for term in catalog.terms:
            if term.id in deleted_terms:
                continue
            dataset_ids = tuple(item for item in term.dataset_ids if item not in deleted_datasets)
            metric_ids = tuple(item for item in term.metric_ids if item not in deleted_metrics)
            dimension_ids = tuple(
                item for item in term.dimension_ids if item not in deleted_dimensions
            )
            changed_term = (
                dataset_ids != term.dataset_ids
                or metric_ids != term.metric_ids
                or dimension_ids != term.dimension_ids
            )
            if changed_term and not (dataset_ids or metric_ids or dimension_ids):
                deleted_terms.add(term.id)
                effects.append(
                    DeletionEffect(
                        action="delete",
                        resource_kind=ResourceKind.TERM,
                        resource_id=term.id,
                        reason="删除依赖后术语已无语义绑定",
                    )
                )
                continue
            if changed_term:
                effects.append(
                    DeletionEffect(
                        action="unlink",
                        resource_kind=ResourceKind.TERM,
                        resource_id=term.id,
                        reason="从术语绑定移除已删除资源",
                    )
                )
            terms.append(
                term.model_copy(
                    update={
                        "dataset_ids": dataset_ids,
                        "metric_ids": metric_ids,
                        "dimension_ids": dimension_ids,
                    }
                )
            )

        dimension_values = tuple(
            item for item in catalog.dimension_values if item.dimension_id not in deleted_dimensions
        )
        effects.extend(
            DeletionEffect(
                action="delete",
                resource_kind=ResourceKind.DIMENSION_VALUE,
                resource_id=item.id,
                reason="删除维度时清理其值字典项",
            )
            for item in catalog.dimension_values
            if item.dimension_id in deleted_dimensions
        )

        analysis_topic_routes = tuple(
            route
            for route in catalog.analysis_topic_routes
            if route.dataset_id not in deleted_datasets
            and route.root_model_id not in deleted_models
            and all(path.target_model_id not in deleted_models for path in route.paths)
            and all(
                relation_id not in deleted_relations
                for path in route.paths
                for relation_id in path.relation_ids
            )
        )
        removed_route_dataset_ids = {
            route.dataset_id for route in catalog.analysis_topic_routes
        } - {route.dataset_id for route in analysis_topic_routes}
        effects.extend(
            DeletionEffect(
                action="unlink",
                resource_kind=ResourceKind.DATASET,
                resource_id=dataset_id,
                reason="关系或模型变更后移除失效的分析主题路径",
            )
            for dataset_id in sorted(removed_route_dataset_ids - deleted_datasets)
        )
        query_rules = tuple(
            item
            for item in catalog.query_rules
            if item.id not in deleted_query_rules and item.dataset_id not in deleted_datasets
        )

        hierarchies: list[HierarchyContract] = []
        for hierarchy in catalog.hierarchies:
            if hierarchy.id in deleted_hierarchies:
                continue
            if hierarchy.model_id in deleted_models:
                effects.append(
                    DeletionEffect(
                        action="delete",
                        resource_kind=ResourceKind.HIERARCHY,
                        resource_id=hierarchy.id,
                        reason="所属模型被删除",
                    )
                )
                continue
            levels = tuple(item for item in hierarchy.levels if item not in deleted_dimensions)
            if levels == hierarchy.levels:
                hierarchies.append(hierarchy)
                continue
            # 少于两级不再构成层级；留着会让编译器因为 levels 校验直接失败。
            if len(levels) < 2:
                effects.append(
                    DeletionEffect(
                        action="delete",
                        resource_kind=ResourceKind.HIERARCHY,
                        resource_id=hierarchy.id,
                        reason="删除维度后不足两级",
                    )
                )
                continue
            hierarchies.append(hierarchy.model_copy(update={"levels": levels}))
            effects.append(
                DeletionEffect(
                    action="unlink",
                    resource_kind=ResourceKind.HIERARCHY,
                    resource_id=hierarchy.id,
                    reason="移除被删除的层级维度",
                )
            )

        update = {
            "models": tuple(normalized_models),
            "model_relations": relations,
            "dimensions": dimensions,
            "metrics": tuple(metrics),
            "data_sets": tuple(data_sets),
            "terms": tuple(terms),
            "dimension_values": dimension_values,
            "semantic_context": semantic_context,
            "hierarchies": tuple(hierarchies),
            "analysis_topic_routes": (
                # Keep the old compiler projection just long enough for
                # reconcile_query_scopes to identify Scope ownership and
                # preserve surviving fact-root IDs.  It is allowed to be
                # temporarily stale inside this private plan, never persisted.
                catalog.analysis_topic_routes
                if compiler_owned_dataset_ids
                else analysis_topic_routes
            ),
            "query_rules": query_rules,
        }
        planned = catalog.model_copy(update=update)
        if compiler_owned_dataset_ids:
            updated = self._reconcile_after_deletion(planned)
            effects = self._effects_for_reconciled_plan(
                before=catalog,
                after=updated,
                effects=effects,
                reconcile_dataset_effects=True,
            )
        else:
            without_rules = SemanticCatalog.model_validate(
                planned.model_copy(update={"query_rules": ()}).model_dump(mode="python")
            )
            updated = self._restore_valid_query_rules(
                without_rules,
                rules=planned.query_rules,
            )
            effects = self._effects_for_reconciled_plan(
                before=catalog,
                after=updated,
                effects=effects,
                reconcile_dataset_effects=False,
            )
        return updated, effects

    @staticmethod
    def _reconcile_after_deletion(catalog: SemanticCatalog) -> SemanticCatalog:
        """Rebuild compiler output, retaining only rules valid in the new Scope.

        QueryRules are downstream of QueryScope.  They are removed only when
        their Dataset retires or their governed semantic IDs leave that Scope;
        every unaffected rule is restored verbatim after structural compile.
        """

        original_rules = catalog.query_rules
        without_rules = catalog.model_copy(update={"query_rules": ()})
        reconciled = reconcile_query_scopes(without_rules)
        return CatalogDeletionPlanner._restore_valid_query_rules(
            reconciled,
            rules=original_rules,
        )

    @staticmethod
    def _restore_valid_query_rules(
        catalog: SemanticCatalog,
        *,
        rules: tuple[QueryRuleContract, ...],
    ) -> SemanticCatalog:
        """Restore exactly the rules that remain valid after deletion."""

        valid_rules = []
        for rule in rules:
            candidate = SemanticCatalog.model_validate(
                catalog.model_copy(update={"query_rules": (rule,)}).model_dump(mode="python")
            )
            try:
                compile_semantic_catalog(candidate)
            except (KeyError, ValueError, SemanticValidationError):
                continue
            valid_rules.append(rule)
        updated = SemanticCatalog.model_validate(
            catalog.model_copy(update={"query_rules": tuple(valid_rules)}).model_dump(mode="python")
        )
        compile_semantic_catalog(updated)
        return updated

    @classmethod
    def _effects_for_reconciled_plan(
        cls,
        *,
        before: SemanticCatalog,
        after: SemanticCatalog,
        effects: list[DeletionEffect],
        reconcile_dataset_effects: bool,
    ) -> tuple[DeletionEffect, ...]:
        """Describe the final reconciled graph, not its transient cleanup."""

        before_by_kind = cls._resources_by_kind(before)
        after_by_kind = cls._resources_by_kind(after)
        reconciled_effects = [
            item
            for item in effects
            if not (
                reconcile_dataset_effects
                and item.resource_kind is ResourceKind.DATASET
                and item.action == "unlink"
            )
            and not (
                item.action == "delete" and item.resource_id in after_by_kind[item.resource_kind]
            )
        ]
        existing_deletes = {
            (item.resource_kind, item.resource_id)
            for item in reconciled_effects
            if item.action == "delete"
        }
        for kind, original in before_by_kind.items():
            current = after_by_kind[kind]
            for resource_id in sorted(set(original) - set(current)):
                if (kind, resource_id) in existing_deletes:
                    continue
                reconciled_effects.append(
                    DeletionEffect(
                        action="delete",
                        resource_kind=kind,
                        resource_id=resource_id,
                        reason=(
                            "删除依赖后查询规则不再可执行"
                            if kind is ResourceKind.QUERY_RULE
                            else "QueryScope 重编译后资源不再可达"
                        ),
                    )
                )
        if reconcile_dataset_effects:
            before_routes = {item.dataset_id: item for item in before.analysis_topic_routes}
            after_routes = {item.dataset_id: item for item in after.analysis_topic_routes}
            for dataset_id in sorted(
                set(before_by_kind[ResourceKind.DATASET]) & set(after_by_kind[ResourceKind.DATASET])
            ):
                if before_by_kind[ResourceKind.DATASET][dataset_id] == after_by_kind[
                    ResourceKind.DATASET
                ][dataset_id] and before_routes.get(dataset_id) == after_routes.get(dataset_id):
                    continue
                reconciled_effects.append(
                    DeletionEffect(
                        action="unlink",
                        resource_kind=ResourceKind.DATASET,
                        resource_id=dataset_id,
                        reason="删除依赖后重新编译内部 QueryScope",
                    )
                )
        return cls._canonical_effects(reconciled_effects)

    @staticmethod
    def _resources_by_kind(
        catalog: SemanticCatalog,
    ) -> dict[ResourceKind, dict[str, object]]:
        collections = {
            ResourceKind.MODEL: catalog.models,
            ResourceKind.RELATION: catalog.model_relations,
            ResourceKind.DIMENSION: catalog.dimensions,
            ResourceKind.METRIC: catalog.metrics,
            ResourceKind.DATASET: catalog.data_sets,
            ResourceKind.TERM: catalog.terms,
            ResourceKind.DIMENSION_VALUE: catalog.dimension_values,
            ResourceKind.SEMANTIC_CONTEXT: catalog.semantic_context,
            ResourceKind.HIERARCHY: catalog.hierarchies,
            ResourceKind.QUERY_RULE: catalog.query_rules,
        }
        return {kind: {item.id: item for item in items} for kind, items in collections.items()}

    @staticmethod
    def _canonical_effects(
        effects: list[DeletionEffect],
    ) -> tuple[DeletionEffect, ...]:
        # Canonical order makes the impact hash independent of traversal order.
        return tuple(
            sorted(
                {
                    (item.action, item.resource_kind, item.resource_id, item.reason): item
                    for item in effects
                }.values(),
                key=lambda item: (
                    item.action != "delete",
                    item.resource_kind.value,
                    item.resource_id,
                    item.reason,
                ),
            )
        )

    @staticmethod
    def _require_resource(
        catalog: SemanticCatalog,
        resource_kind: ResourceKind,
        resource_id: str,
    ) -> None:
        collections = {
            ResourceKind.MODEL: catalog.models,
            ResourceKind.RELATION: catalog.model_relations,
            ResourceKind.DIMENSION: catalog.dimensions,
            ResourceKind.METRIC: catalog.metrics,
            ResourceKind.DATASET: catalog.data_sets,
            ResourceKind.TERM: catalog.terms,
            ResourceKind.DIMENSION_VALUE: catalog.dimension_values,
            ResourceKind.SEMANTIC_CONTEXT: catalog.semantic_context,
            ResourceKind.HIERARCHY: catalog.hierarchies,
            ResourceKind.QUERY_RULE: catalog.query_rules,
        }
        if not any(item.id == resource_id for item in collections[resource_kind]):
            raise SemanticValidationError(
                f"{resource_kind.value} was not found",
                code="MODELING_RESOURCE_NOT_FOUND",
            )
