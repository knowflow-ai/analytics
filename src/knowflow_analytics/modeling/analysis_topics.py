"""Cube-style analysis-topic proposals over the semantic catalog.

The proposal algorithm is deterministic and non-mutating.  It only uses governed
model metadata and confirmed relation cardinality; AI may decorate the returned
name/description later, but cannot select members or join paths.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict, deque

from pydantic import Field

from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    Cardinality,
    DatasetSpec,
    FieldKind,
    FrozenModel,
    RelationSpec,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.rule_modeller import stable_id


class AnalysisTopicExclusion(FrozenModel):
    element_id: str = Field(min_length=1, max_length=128)
    reason_code: str = Field(min_length=1, max_length=128)


class AnalysisTopicProposal(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    dataset: DatasetSpec
    route: AnalysisTopicRouteSpec
    exclusions: tuple[AnalysisTopicExclusion, ...] = ()


class AnalysisTopicProposalSet(FrozenModel):
    revision_id: str = Field(min_length=1, max_length=128)
    revision_etag: int = Field(ge=1)
    schema_snapshot_hash: str = Field(min_length=1, max_length=128)
    proposals: tuple[AnalysisTopicProposal, ...]


def validate_analysis_topic_route(
    release: SemanticRelease,
    route: AnalysisTopicRouteSpec,
) -> None:
    """Validate continuity and dataset reachability of one frozen topic route."""

    datasets = {item.id: item for item in release.datasets}
    models = {item.id for item in release.models}
    relations = {item.id: item for item in release.relations}
    dataset = datasets.get(route.dataset_id)
    if dataset is None:
        raise SemanticValidationError(
            f"analysis topic dataset was not found: {route.dataset_id}",
            code="ANALYSIS_TOPIC_DATASET_NOT_FOUND",
        )
    if route.root_model_id not in dataset.model_ids:
        raise SemanticValidationError(
            "analysis topic root is outside the dataset",
            code="ANALYSIS_TOPIC_ROOT_OUT_OF_SCOPE",
        )
    metrics = {item.id: item for item in release.metrics}
    fields = {item.id: item for item in release.fields}
    dimensions = {item.id: item for item in release.dimensions}
    if route.default_count_metric_id is not None:
        count_metric = metrics.get(route.default_count_metric_id)
        if count_metric is None or count_metric.id not in dataset.metric_ids:
            raise SemanticValidationError(
                "analysis topic default count metric is not exposed by its dataset",
                code="ANALYSIS_TOPIC_DEFAULT_COUNT_METRIC_NOT_EXPOSED",
            )
        if count_metric.model_id != route.root_model_id:
            raise SemanticValidationError(
                "analysis topic default count metric must belong to its fact root",
                code="ANALYSIS_TOPIC_DEFAULT_COUNT_METRIC_OUTSIDE_ROOT",
            )
        if count_metric.aggregation not in {
            Aggregation.COUNT,
            Aggregation.COUNT_DISTINCT,
        }:
            raise SemanticValidationError(
                "analysis topic default count metric must use COUNT or COUNT_DISTINCT",
                code="ANALYSIS_TOPIC_DEFAULT_COUNT_METRIC_INVALID",
            )
    metric_models = {metrics[item].model_id for item in dataset.metric_ids}
    if metric_models - {route.root_model_id}:
        raise SemanticValidationError(
            "analysis topic metrics must belong to its fact root",
            code="ANALYSIS_TOPIC_METRIC_OUTSIDE_ROOT",
        )
    technical_dimensions = sorted(
        dimension_id
        for dimension_id in dataset.dimension_ids
        if (
            fields[dimensions[dimension_id].field_id].kind is FieldKind.IDENTIFIER
            or dimensions[dimension_id].semantic_type == "identifier"
        )
    )
    if technical_dimensions:
        raise SemanticValidationError(
            f"analysis topic exposes technical identifiers: {technical_dimensions[:5]}",
            code="ANALYSIS_TOPIC_TECHNICAL_IDENTIFIER_EXPOSED",
        )
    members_by_name: dict[str, list[str]] = defaultdict(list)
    scoped_names = scope_canonical_names(release, route)
    for member_type, member_id, _model_id, _name in _scope_members(
        dataset=dataset,
        metrics=metrics,
        dimensions=dimensions,
    ):
        members_by_name[_canonical_name(scoped_names[member_id])].append(
            f"{member_type}:{member_id}"
        )
    conflicting_members = sorted(
        member_id
        for member_ids in members_by_name.values()
        if len(set(member_ids)) > 1
        for member_id in set(member_ids)
    )
    if conflicting_members:
        raise SemanticValidationError(
            f"analysis topic has conflicting canonical names: {conflicting_members[:5]}",
            code="ANALYSIS_TOPIC_CANONICAL_NAME_CONFLICT",
        )
    reachable = {route.root_model_id}
    governed_prefixes: dict[str, tuple[str, ...]] = {route.root_model_id: ()}
    for path in route.paths:
        current = route.root_model_id
        visited_models = {current}
        relation_prefix: list[str] = []
        for relation_id in path.relation_ids:
            relation = relations.get(relation_id)
            if relation is None:
                raise SemanticValidationError(
                    f"analysis topic relation was not found: {relation_id}",
                    code="ANALYSIS_TOPIC_RELATION_NOT_FOUND",
                )
            if relation.left_model_id == current:
                next_model = relation.right_model_id
            elif relation.right_model_id == current:
                next_model = relation.left_model_id
            else:
                raise SemanticValidationError(
                    f"analysis topic path is discontinuous at relation {relation_id}",
                    code="ANALYSIS_TOPIC_PATH_DISCONTINUOUS",
                )
            if _expands_rows(relation, from_model_id=current):
                raise SemanticValidationError(
                    f"analysis topic path expands the fact grain at relation {relation_id}",
                    code="ANALYSIS_TOPIC_FANOUT_PATH",
                )
            if next_model in visited_models:
                raise SemanticValidationError(
                    "analysis topic path contains a cycle",
                    code="ANALYSIS_TOPIC_PATH_CYCLE",
                )
            visited_models.add(next_model)
            reachable.add(next_model)
            relation_prefix.append(relation_id)
            existing_prefix = governed_prefixes.get(next_model)
            if existing_prefix is not None and existing_prefix != tuple(relation_prefix):
                raise SemanticValidationError(
                    f"analysis topic has conflicting paths to model {next_model}",
                    code="ANALYSIS_TOPIC_PATH_CONFLICT",
                )
            governed_prefixes[next_model] = tuple(relation_prefix)
            current = next_model
        if current != path.target_model_id:
            raise SemanticValidationError(
                "analysis topic path does not end at its target",
                code="ANALYSIS_TOPIC_PATH_TARGET_MISMATCH",
            )
    out_of_scope_models = reachable - set(dataset.model_ids)
    if out_of_scope_models:
        raise SemanticValidationError(
            "analysis topic path traverses models outside the dataset: "
            f"{sorted(out_of_scope_models)}",
            code="ANALYSIS_TOPIC_PATH_MODEL_OUT_OF_SCOPE",
        )
    target_models = {item.target_model_id for item in route.paths}
    missing_models = set(dataset.model_ids) - {route.root_model_id} - target_models
    if missing_models:
        raise SemanticValidationError(
            f"analysis topic has dataset models without a frozen route: {sorted(missing_models)}",
            code="ANALYSIS_TOPIC_MODEL_UNREACHABLE",
        )
    if not reachable.issubset(models):  # defensive; SemanticRelease validates references too
        raise SemanticValidationError(
            "analysis topic route contains an unknown model",
            code="ANALYSIS_TOPIC_MODEL_NOT_FOUND",
        )


def _expands_rows(relation: RelationSpec, *, from_model_id: str) -> bool:
    if relation.cardinality is Cardinality.MANY_TO_MANY:
        return True
    from_is_left = from_model_id == relation.left_model_id
    if relation.cardinality is Cardinality.ONE_TO_MANY:
        return from_is_left
    if relation.cardinality is Cardinality.MANY_TO_ONE:
        return not from_is_left
    return False


ENTITY_NAME_DIMENSION_SUFFIX = "名称"


def entity_name_dimension_name(model_name: str) -> str:
    """The compiler-owned canonical name for an entity's name dimension."""

    return f"{model_name}{ENTITY_NAME_DIMENSION_SUFFIX}"


def entity_name_dimension_ids(
    release: SemanticRelease,
    *,
    model_id: str,
) -> frozenset[str]:
    """Dimensions holding the compiler-named entity name of one model."""

    model = next((item for item in release.models if item.id == model_id), None)
    if model is None:
        return frozenset()
    expected = _canonical_name(entity_name_dimension_name(model.name))
    return frozenset(
        item.id
        for item in release.dimensions
        if item.model_id == model_id and _canonical_name(item.name) == expected
    )


def _canonical_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def scope_canonical_names(
    release: SemanticRelease,
    route: AnalysisTopicRouteSpec,
) -> dict[str, str]:
    """Return the exact S2SQL canonical name for every member in one scope.

    Raw member names remain authoritative unless the same normalized name occurs
    across models. Only colliding dimensions on a non-root path are qualified,
    using the exact wire syntax ``<path.prefix>.<dimension.name>``. Metrics and
    root-model dimensions cannot be path-qualified and therefore remain
    fail-closed when they still collide.
    """

    dataset = next((item for item in release.datasets if item.id == route.dataset_id), None)
    if dataset is None:
        raise SemanticValidationError(
            f"analysis topic dataset was not found: {route.dataset_id}",
            code="ANALYSIS_TOPIC_DATASET_NOT_FOUND",
        )
    metrics = {item.id: item for item in release.metrics}
    dimensions = {item.id: item for item in release.dimensions}
    members = _scope_members(dataset=dataset, metrics=metrics, dimensions=dimensions)
    raw_groups: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for member in members:
        raw_groups[_canonical_name(member[3])].append(member)
    conflicting_names = {
        name for name, grouped in raw_groups.items() if len({item[1] for item in grouped}) > 1
    }
    prefixes = {item.target_model_id: item.prefix for item in route.paths if item.prefix}
    scoped = {}
    for member_type, member_id, model_id, name in members:
        prefix = prefixes.get(model_id)
        if (
            member_type == "dimension"
            and model_id != route.root_model_id
            and _canonical_name(name) in conflicting_names
            and prefix is not None
        ):
            scoped[member_id] = f"{prefix}.{name}"
        else:
            scoped[member_id] = name
    return scoped


def _scope_members(
    *,
    dataset: DatasetSpec,
    metrics: dict,
    dimensions: dict,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        sorted(
            (
                *(
                    ("metric", metric_id, metrics[metric_id].model_id, metrics[metric_id].name)
                    for metric_id in dataset.metric_ids
                ),
                *(
                    (
                        "dimension",
                        dimension_id,
                        dimensions[dimension_id].model_id,
                        dimensions[dimension_id].name,
                    )
                    for dimension_id in dataset.dimension_ids
                ),
            ),
            key=lambda item: (item[0], item[1]),
        )
    )


def _models_requiring_scope_prefix(
    *,
    dataset: DatasetSpec,
    root_model_id: str,
    metrics: dict,
    dimensions: dict,
) -> set[str]:
    groups: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for member in _scope_members(dataset=dataset, metrics=metrics, dimensions=dimensions):
        groups[_canonical_name(member[3])].append(member)
    required: set[str] = set()
    for members in groups.values():
        if len({item[1] for item in members}) < 2 or len({item[2] for item in members}) < 2:
            continue
        required.update(
            model_id
            for member_type, _member_id, model_id, _name in members
            if member_type == "dimension" and model_id != root_model_id
        )
    return required


def route_relation_ids_for_models(
    release: SemanticRelease,
    *,
    dataset_id: str,
    required_model_ids: set[str],
) -> tuple[str, tuple[str, ...]] | None:
    """Return ``(root, relation_ids)`` for a governed topic, if one exists."""

    route = next(
        (item for item in release.analysis_topic_routes if item.dataset_id == dataset_id),
        None,
    )
    if route is None:
        return None
    # 不在这里重跑 ``validate_analysis_topic_route``：路由在应用 Catalog 时已按同一
    # 输入编译并校验，发布后冻结，不会漂移。问数期唯一会走到这里的"违规"路由是
    # 生成阶段的并集——它把多个事实根的指标故意放进同一个词汇表，只用于让模型写出
    # 业务名 S2SQL，再逐个真实作用域反推。在这里校验它，等于让"问并集"这条诚实
    # 兜底永远以 ANALYSIS_TOPIC_METRIC_OUTSIDE_ROOT 收场：一个需要 JOIN 的查询
    # 不管真正的毛病是什么，用户与重试链拿到的都是这个与他无关的建模码。
    paths = {item.target_model_id: item.relation_ids for item in route.paths}
    selected: list[str] = []
    for model_id in sorted(required_model_ids - {route.root_model_id}):
        relation_ids = paths.get(model_id)
        if relation_ids is None:
            raise SemanticValidationError(
                f"analysis topic has no frozen path to model {model_id}",
                code="ANALYSIS_TOPIC_PATH_MISSING",
            )
        selected.extend(relation_ids)
    return route.root_model_id, tuple(dict.fromkeys(selected))


class AnalysisTopicProposer:
    """Compile one internal query scope for every metric or entity root.

    Reviewed QueryScope contract (2026-08-27): the Catalog is authoritative and
    this deterministic projection retains ``DatasetSpec`` plus
    ``AnalysisTopicRouteSpec`` only as the runtime wire representation. Business
    metric owners remain roots without a primary identifier, and every confirmed
    primary entity keeps an independent count root even when another scope can
    reach it.
    """

    def propose(self, release: SemanticRelease) -> tuple[AnalysisTopicProposal, ...]:
        fields = {item.id: item for item in release.fields}
        metrics_by_model: dict[str, list[str]] = defaultdict(list)
        dimensions_by_model: dict[str, list[str]] = defaultdict(list)
        exclusions_by_model: dict[str, list[AnalysisTopicExclusion]] = defaultdict(list)
        for metric in release.metrics:
            metrics_by_model[metric.model_id].append(metric.id)
        for dimension in release.dimensions:
            field = fields[dimension.field_id]
            if field.kind is FieldKind.IDENTIFIER or dimension.semantic_type == "identifier":
                exclusions_by_model[dimension.model_id].append(
                    AnalysisTopicExclusion(
                        element_id=dimension.id,
                        reason_code="technical_identifier",
                    )
                )
            else:
                dimensions_by_model[dimension.model_id].append(dimension.id)

        primary_fields_by_model = {}
        for field in sorted(release.fields, key=lambda item: item.id):
            if field.kind is FieldKind.IDENTIFIER and field.identifier_type == "primary":
                primary_fields_by_model.setdefault(field.model_id, field)
        generated_count_ids = {
            model_id: stable_id("metric", "default_count", model_id, primary.id)
            for model_id, primary in primary_fields_by_model.items()
        }
        business_metric_models = {
            metric.model_id
            for metric in release.metrics
            if metric.id != generated_count_ids.get(metric.model_id)
        }
        roots = sorted(business_metric_models | set(primary_fields_by_model))
        models = {item.id: item for item in release.models}
        metrics = {item.id: item for item in release.metrics}
        dimensions = {item.id: item for item in release.dimensions}
        datasets = {item.id: item for item in release.datasets}
        existing_routes_by_root: dict[str, list[AnalysisTopicRouteSpec]] = defaultdict(list)
        for route in release.analysis_topic_routes:
            existing_routes_by_root[route.root_model_id].append(route)
        proposals: list[AnalysisTopicProposal] = []
        for root_model_id in roots:
            paths, ambiguous = self._safe_unique_paths(
                root_model_id=root_model_id,
                relations=release.relations,
            )
            model_ids = (root_model_id, *sorted(paths))
            metric_ids = tuple(sorted(metrics_by_model[root_model_id]))
            dimension_ids = tuple(
                item
                for model_id in model_ids
                for item in sorted(dimensions_by_model.get(model_id, ()))
            )
            exclusions = [
                item for model_id in model_ids for item in exclusions_by_model.get(model_id, ())
            ]
            exclusions.extend(
                AnalysisTopicExclusion(
                    element_id=model_id,
                    reason_code="ambiguous_safe_path",
                )
                for model_id in sorted(ambiguous)
            )
            exclusions.sort(key=lambda item: (item.reason_code, item.element_id))
            root_model = models[root_model_id]
            generated_count_metric_id = generated_count_ids.get(root_model_id)
            default_count_metric_id = (
                generated_count_metric_id
                if generated_count_metric_id is not None and generated_count_metric_id in metric_ids
                else None
            )
            existing_routes = sorted(
                existing_routes_by_root.get(root_model_id, ()),
                key=lambda item: item.dataset_id,
            )
            if len(existing_routes) > 1:
                raise SemanticValidationError(
                    f"query scope root has multiple existing scopes: {root_model_id}",
                    code="ANALYSIS_TOPIC_ROOT_CONFLICT",
                )
            reviewed_contexts = {item.ai_context for item in existing_routes if item.ai_context}
            preserved_context = next(iter(reviewed_contexts), "")
            existing_route = existing_routes[0] if len(existing_routes) == 1 else None
            existing = (
                datasets.get(existing_route.dataset_id) if existing_route is not None else None
            )
            existing_datasets = [
                item
                for item in release.datasets
                if root_model_id in item.model_ids
                and set(item.metric_ids).intersection(metric_ids)
                and all(
                    metrics[metric_id].model_id == root_model_id for metric_id in item.metric_ids
                )
            ]
            if existing is None and len(existing_datasets) == 1:
                existing = existing_datasets[0]
            dataset_id = (
                existing.id
                if existing is not None
                else stable_id("dataset", "topic", root_model_id)
            )
            dataset = (
                existing.model_copy(
                    update={
                        "model_ids": model_ids,
                        "metric_ids": metric_ids,
                        "dimension_ids": dimension_ids,
                    }
                )
                if existing is not None
                else DatasetSpec(
                    id=dataset_id,
                    name=f"{root_model.name}分析",
                    biz_name=f"{root_model.biz_name or root_model.id}_analysis",
                    model_ids=model_ids,
                    metric_ids=metric_ids,
                    dimension_ids=dimension_ids,
                    aliases=(root_model.name,),
                    description=f"以{root_model.name}为事实根的受治理分析主题",
                )
            )
            qualified_models = _models_requiring_scope_prefix(
                dataset=dataset,
                root_model_id=root_model_id,
                metrics=metrics,
                dimensions=dimensions,
            )
            route = AnalysisTopicRouteSpec(
                dataset_id=dataset_id,
                root_model_id=root_model_id,
                default_count_metric_id=default_count_metric_id,
                paths=tuple(
                    AnalysisTopicPathSpec(
                        target_model_id=target_model_id,
                        relation_ids=relation_ids,
                        prefix=(
                            models[target_model_id].name
                            if target_model_id in qualified_models
                            else None
                        ),
                    )
                    for target_model_id, relation_ids in sorted(paths.items())
                ),
                ai_context=preserved_context,
            )
            proposed_release = release.model_copy(
                update={
                    "datasets": (
                        *(item for item in release.datasets if item.id != dataset.id),
                        dataset,
                    ),
                    "analysis_topic_routes": (
                        *(
                            item
                            for item in release.analysis_topic_routes
                            if item.dataset_id != route.dataset_id
                        ),
                        route,
                    ),
                }
            )
            validate_analysis_topic_route(proposed_release, route)
            proposals.append(
                AnalysisTopicProposal(
                    id=stable_id("analysis-topic-proposal", release.id, root_model_id),
                    dataset=dataset,
                    route=route,
                    exclusions=tuple(exclusions),
                )
            )
        return tuple(proposals)

    @staticmethod
    def _safe_unique_paths(
        *,
        root_model_id: str,
        relations: tuple[RelationSpec, ...],
    ) -> tuple[dict[str, tuple[str, ...]], set[str]]:
        adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for relation in relations:
            if relation.cardinality is Cardinality.ONE_TO_ONE:
                adjacency[relation.left_model_id].append((relation.right_model_id, relation.id))
                adjacency[relation.right_model_id].append((relation.left_model_id, relation.id))
            elif relation.cardinality is Cardinality.MANY_TO_ONE:
                adjacency[relation.left_model_id].append((relation.right_model_id, relation.id))
            elif relation.cardinality is Cardinality.ONE_TO_MANY:
                adjacency[relation.right_model_id].append((relation.left_model_id, relation.id))
        for edges in adjacency.values():
            edges.sort(key=lambda item: item[1])

        # BFS 给出最短路径,同时数出最短路径的条数。歧义的判据是"存在等长的
        # 另一条最短路径"(同一对模型间有两条外键、或两条同长度的绕行),而不是
        # "存在任何其它路径"。一条严格更长的绕行是另一种派生关系,不是对同一
        # 关系的竞争解释;把它当歧义会让实体整体退出作用域。
        queue: deque[str] = deque([root_model_id])
        distance = {root_model_id: 0}
        shortest_ways = {root_model_id: 1}
        predecessor: dict[str, tuple[str, str]] = {}
        while queue:
            current = queue.popleft()
            for target, relation_id in adjacency.get(current, ()):
                if target not in distance:
                    distance[target] = distance[current] + 1
                    shortest_ways[target] = shortest_ways[current]
                    predecessor[target] = (current, relation_id)
                    queue.append(target)
                elif distance[target] == distance[current] + 1:
                    shortest_ways[target] += shortest_ways[current]

        paths: dict[str, tuple[str, ...]] = {}
        ambiguous: set[str] = set()
        for target in sorted(set(distance) - {root_model_id}):
            if shortest_ways[target] > 1:
                ambiguous.add(target)
                continue
            path_edges: list[tuple[str, str, str]] = []
            current = target
            while current != root_model_id:
                previous, relation_id = predecessor[current]
                path_edges.append((previous, current, relation_id))
                current = previous
            path_edges.reverse()
            paths[target] = tuple(edge[2] for edge in path_edges)
        return paths, ambiguous

    @staticmethod
    def _reachable_without_edge(
        *,
        root_model_id: str,
        target_model_id: str,
        adjacency: dict[str, list[tuple[str, str]]],
        excluded_edge: tuple[str, str, str],
    ) -> bool:
        queue: deque[str] = deque([root_model_id])
        visited = {root_model_id}
        while queue:
            current = queue.popleft()
            for target, relation_id in adjacency.get(current, ()):
                if (current, target, relation_id) == excluded_edge or target in visited:
                    continue
                if target == target_model_id:
                    return True
                visited.add(target)
                queue.append(target)
        return False
