"""Accuracy-oriented artifacts produced after the model schema stage.

The AI modeller stays the authority for table and column classification.  This
module turns that output into a complete, human-confirmable Candidate:

* aliases decorate governed Dimension/Metric/DimensionValue resources;
* a default entity-count metric is derived only from a confirmed primary key;
* completion checks require every primary entity to have a routed topic whose
  ``COUNT(*)`` binding is explicit.

No function in this module reads a natural-language question or changes the
online parser pipeline.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionValueSpec,
    FieldKind,
    FrozenModel,
    SemanticContextEntry,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.analysis_topics import (
    AnalysisTopicProposal,
    AnalysisTopicProposer,
    _canonical_name,
    default_count_metric_id,
    entity_name_dimension_name,
    fact_root_model_ids,
    scope_canonical_names,
    validate_analysis_topic_route,
)
from knowflow_analytics.modeling.catalog_compiler import (
    catalog_dataset_from_topic_command,
    compile_semantic_catalog,
    replace_catalog_item,
)
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetContract,
    FieldParamContract,
    IdentifierType,
    MetricContract,
    MetricDefineByFieldParamsContract,
    MetricDefineType,
    ModelDimensionType,
    SemanticCatalog,
)
from knowflow_analytics.modeling.contracts import (
    AiModelingArtifact,
    DimensionValueCandidate,
    ModelingRevision,
    QueryScopeCompilationDiagnostic,
    QueryScopeExclusionDiagnostic,
    SemanticAliasDraft,
    carry_semantic_context_review,
)
from knowflow_analytics.modeling.semantic_expression import quote_sql_identifier


class OneClickModelingArtifactService:
    """Complete one model-schema proposal inside the same product action."""

    _MAX_PARALLEL_RESOURCE_ALIASES = 3

    def __init__(
        self, *, ai_modeller, dimension_alias_suggester, max_concurrency: int | None = None
    ) -> None:
        self._ai_modeller = ai_modeller
        self._dimension_alias_suggester = dimension_alias_suggester
        self._max_concurrency = max(1, max_concurrency or self._MAX_PARALLEL_RESOURCE_ALIASES)

    def build(self, revision: ModelingRevision, *, tenant_id: str = "") -> AiModelingArtifact:
        catalog = self._catalog(revision)
        # 实体名称维度先于一切派生：计数指标、主题成员、别名建议都要建立在
        # 正确的实体名之上（顺序与 materialize 必须一致，产物才可复演）。
        catalog, _entity_names = ensure_entity_name_dimensions(catalog)
        counted_catalog, generated_counts = ensure_default_count_metrics(catalog)
        counted_release = compile_semantic_catalog(counted_catalog)
        proposals = AnalysisTopicProposer().propose(counted_release)
        datasets = tuple(item.dataset for item in proposals)
        routes = tuple(item.route for item in proposals)
        topic_catalog = self._apply_topics(
            catalog=counted_catalog,
            projection=counted_release,
            datasets=datasets,
            routes=routes,
        )
        topic_release = compile_semantic_catalog(topic_catalog)
        topic_revision = ModelingRevision.model_validate(
            revision.model_copy(
                update={
                    "semantic_catalog": topic_catalog,
                    "semantic_spec": topic_release,
                    **carry_semantic_context_review(revision, topic_release.semantic_context),
                }
            ).model_dump(mode="python")
        )
        semantic_context = _compile_semantic_context_drafts(topic_catalog)
        query_scope_diagnostics = _compile_query_scope_diagnostics(
            proposals,
            compile_semantic_catalog(topic_catalog),
        )
        artifact = AiModelingArtifact.create(
            base_semantic_spec_hash=revision.semantic_spec.spec_hash,
            alias_drafts=self._suggest_aliases(topic_revision, tenant_id=tenant_id),
            dimension_values=topic_catalog.dimension_values,
            default_count_metrics=generated_counts,
            analysis_topic_datasets=datasets,
            analysis_topic_routes=routes,
            semantic_context=semantic_context,
            query_scope_diagnostics=query_scope_diagnostics,
        )
        self.materialize(revision, artifact)
        return artifact

    def materialize(
        self,
        revision: ModelingRevision,
        artifact: AiModelingArtifact,
    ) -> ModelingRevision:
        catalog = self._catalog(revision)
        current_dimension_ids = {item.id for item in catalog.dimensions}
        frozen_dimension_ids = {item.dimension_id for item in artifact.dimension_values}
        if not frozen_dimension_ids.issubset(current_dimension_ids):
            self._stale("AI modeling dimension-value snapshot changed")
        catalog = SemanticCatalog.model_validate(
            catalog.model_copy(update={"dimension_values": artifact.dimension_values}).model_dump(
                mode="python"
            )
        )
        try:
            profiled_release = compile_semantic_catalog(catalog)
        except SemanticValidationError:
            self._stale("AI modeling dimension-value snapshot changed")
        if profiled_release.spec_hash != artifact.base_semantic_spec_hash:
            self._stale("AI modeling artifact belongs to another semantic Candidate")
        catalog, _entity_names = ensure_entity_name_dimensions(catalog)
        counted_catalog, generated_counts = ensure_default_count_metrics(catalog)
        if generated_counts != artifact.default_count_metrics:
            self._stale("AI modeling default-count artifact changed")
        counted_release = compile_semantic_catalog(counted_catalog)
        expected = AnalysisTopicProposer().propose(counted_release)
        if (
            tuple(item.dataset for item in expected) != artifact.analysis_topic_datasets
            or tuple(item.route for item in expected) != artifact.analysis_topic_routes
        ):
            self._stale("AI modeling analysis-topic artifact changed")
        catalog = self._apply_topics(
            catalog=counted_catalog,
            projection=counted_release,
            datasets=artifact.analysis_topic_datasets,
            routes=artifact.analysis_topic_routes,
        )
        expected_context = _compile_semantic_context_drafts(catalog)
        if expected_context != artifact.semantic_context:
            self._stale("AI modeling semantic-context artifact changed")
        if artifact.query_scope_compilation_hash is not None:
            expected_diagnostics = _compile_query_scope_diagnostics(
                expected,
                compile_semantic_catalog(catalog),
            )
            if expected_diagnostics != artifact.query_scope_diagnostics:
                self._stale("AI modeling query-scope diagnostics changed")
        catalog = SemanticCatalog.model_validate(
            catalog.model_copy(update={"semantic_context": artifact.semantic_context}).model_dump(
                mode="python"
            )
        )
        catalog = apply_semantic_alias_drafts(catalog, artifact.alias_drafts)
        release = compile_semantic_catalog(catalog)
        validate_ai_modeling_completeness(release, alias_drafts=artifact.alias_drafts)
        return ModelingRevision.model_validate(
            revision.model_copy(
                update={
                    "semantic_catalog": catalog,
                    "semantic_spec": release,
                    "ai_modeling_artifact_hash": artifact.artifact_hash,
                    # 新草稿进了上下文时旧评审失效；内容没变则原样沿用（同一条规则见
                    # RevisionEditor.replace_semantic_catalog）
                    **carry_semantic_context_review(revision, release.semantic_context),
                }
            ).model_dump(mode="python")
        )

    @staticmethod
    def _apply_topics(
        *,
        catalog: SemanticCatalog,
        projection: SemanticRelease,
        datasets: tuple[DatasetSpec, ...],
        routes: tuple[AnalysisTopicRouteSpec, ...],
        preserve_reviewed_dataset_metadata: bool = False,
    ) -> SemanticCatalog:
        previous_datasets = {item.id: item for item in catalog.data_sets}
        previous_by_dataset = {item.dataset_id: item for item in catalog.analysis_topic_routes}
        previous_compiler_dataset_ids = set(previous_by_dataset)
        previous_by_root: dict[str, list[AnalysisTopicRouteSpec]] = defaultdict(list)
        for item in catalog.analysis_topic_routes:
            previous_by_root[item.root_model_id].append(item)
        resolved_routes: list[AnalysisTopicRouteSpec] = []
        for route in routes:
            root_matches = tuple(previous_by_root.get(route.root_model_id, ()))
            if len(root_matches) > 1:
                raise SemanticValidationError(
                    f"query scope root has multiple existing scopes: {route.root_model_id}",
                    code="ANALYSIS_TOPIC_ROOT_CONFLICT",
                )
            previous = previous_by_dataset.get(route.dataset_id)
            if not route.ai_context:
                context_sources = (previous,) if previous is not None else root_matches
                reviewed_contexts = {item.ai_context for item in context_sources if item.ai_context}
                preserved_context = next(iter(reviewed_contexts), "")
                if preserved_context:
                    route = route.model_copy(update={"ai_context": preserved_context})
            resolved_routes.append(route)
        routed_ids = {item.dataset_id for item in resolved_routes}
        # Once a Catalog has entered compiler-owned QueryScope mode, every
        # Dataset is derived output.  Keeping an un-routed manual Dataset beside
        # compiled routes makes the query service disable global routing for the
        # entire Release.  A route-less legacy Catalog remains readable until it
        # is explicitly migrated; a fresh compiler projection atomically retires
        # all Dataset objects it did not produce.
        replaced_dataset_ids = (
            set(previous_datasets)
            if resolved_routes or catalog.analysis_topic_routes
            else previous_compiler_dataset_ids
        )
        obsolete_dataset_ids = replaced_dataset_ids - routed_ids
        compiled_datasets = tuple(
            _preserve_dataset_metadata(
                catalog_dataset_from_topic_command(
                    dataset,
                    projection,
                    previous_datasets.get(dataset.id),
                ),
                previous=previous_datasets.get(dataset.id),
                enabled=preserve_reviewed_dataset_metadata,
            )
            for dataset in datasets
        )
        compiled_dataset_ids = {item.id for item in compiled_datasets}
        reconciled_terms = []
        for term in catalog.terms:
            remaining_dataset_ids = tuple(
                dataset_id
                for dataset_id in term.dataset_ids
                if dataset_id not in obsolete_dataset_ids
            )
            if (
                term.dataset_ids
                and not remaining_dataset_ids
                and not (term.metric_ids or term.dimension_ids)
            ):
                continue
            reconciled_terms.append(
                term.model_copy(update={"dataset_ids": remaining_dataset_ids})
                if remaining_dataset_ids != term.dataset_ids
                else term
            )
        # Build the complete derived projection atomically.  Validating after
        # each Dataset replacement leaves a transient Catalog whose old Scope
        # still references a just-retired metric/dimension, preventing the very
        # reconciliation intended to repair it.
        updated = catalog.model_copy(
            update={
                "data_sets": (
                    *(
                        item
                        for item in catalog.data_sets
                        if item.id not in replaced_dataset_ids
                        and item.id not in compiled_dataset_ids
                    ),
                    *compiled_datasets,
                ),
                "analysis_topic_routes": tuple(resolved_routes),
                "query_rules": tuple(
                    item
                    for item in catalog.query_rules
                    if item.dataset_id not in obsolete_dataset_ids
                ),
                "terms": tuple(reconciled_terms),
                "semantic_context": tuple(
                    item
                    for item in catalog.semantic_context
                    if not (
                        item.target_type == "query_scope" and item.target_id in obsolete_dataset_ids
                    )
                ),
            }
        )
        updated = SemanticCatalog.model_validate(updated.model_dump(mode="python"))
        release = compile_semantic_catalog(updated)
        for route in resolved_routes:
            validate_analysis_topic_route(release, route)
        return updated

    def _suggest_aliases(
        self,
        revision: ModelingRevision,
        *,
        tenant_id: str = "",
    ) -> tuple[SemanticAliasDraft, ...]:
        if self._ai_modeller is None:
            raise SemanticValidationError(
                "AI alias suggestion is not configured",
                code="AI_MODELLER_DISABLED",
            )
        release = revision.semantic_spec
        catalog = self._catalog(revision)
        models = {item.id: item for item in release.models}
        fields = {item.id: item for item in release.fields}
        dimensions = {item.id: item for item in catalog.dimensions}
        metrics = {item.id: item for item in catalog.metrics}
        resources = tuple(
            sorted(
                (
                    *(
                        ("dimension", item)
                        for item in release.dimensions
                        if fields[item.field_id].kind is not FieldKind.IDENTIFIER
                    ),
                    *(("metric", item) for item in release.metrics),
                ),
                key=lambda value: (value[0], value[1].id),
            )
        )

        # Aliases depend only on a resource's own metadata, so one request per
        # metric and per dimension turns a realistic schema into hundreds of model
        # calls. Group a model's resources into a single request instead; the
        # surrounding business entity is also better context for consistent
        # wording. Models stay parallel so one slow entity cannot stall the rest.
        by_model: dict[str, list[tuple[str, object]]] = defaultdict(list)
        for resource_type, item in resources:
            by_model[item.model_id].append((resource_type, item))

        def suggest_model(model_id: str) -> list[SemanticAliasDraft]:
            group = by_model[model_id]
            outputs = self._ai_modeller.suggest_alias_batch(
                model_name=models[model_id].name,
                resources=tuple(
                    {
                        "resource_id": item.id,
                        "resource_type": resource_type,
                        "name": item.name,
                        "biz_name": (
                            dimensions[item.id].biz_name
                            if resource_type == "dimension"
                            else metrics[item.id].biz_name
                        ),
                        "description": item.description,
                        "existing_aliases": item.aliases,
                    }
                    for resource_type, item in group
                ),
                trace={
                    "revision_id": revision.id,
                    "model_id": model_id,
                    "revision_etag": str(revision.etag),
                    "one_click_modeling": "true",
                    "tenant_id": tenant_id,
                },
            )
            return [
                SemanticAliasDraft(
                    resource_type=resource_type,
                    resource_id=item.id,
                    resource_name=item.name,
                    aliases=tuple(dict.fromkeys((*item.aliases, *outputs[item.id].aliases))),
                )
                for resource_type, item in group
            ]

        drafts = []
        if by_model:
            model_ids = sorted(by_model)
            with ThreadPoolExecutor(
                max_workers=min(self._max_concurrency, len(model_ids)),
                thread_name_prefix="analytics-resource-alias",
            ) as executor:
                for group_drafts in executor.map(suggest_model, model_ids):
                    drafts.extend(group_drafts)
        drafts = list(
            qualify_cross_model_metric_aliases(
                tuple(drafts),
                metrics=release.metrics,
                models=release.models,
            )
        )
        drafts = list(
            preserve_source_column_aliases(
                tuple(drafts),
                metrics=release.metrics,
                dimensions=release.dimensions,
                fields=release.fields,
            )
        )
        drafts.extend(self._suggest_value_aliases(revision, tenant_id=tenant_id))
        return tuple(drafts)

    def _suggest_value_aliases(
        self,
        revision: ModelingRevision,
        *,
        tenant_id: str = "",
    ) -> tuple[SemanticAliasDraft, ...]:
        values = self._catalog(revision).dimension_values
        if not values:
            return ()
        if self._dimension_alias_suggester is None:
            raise SemanticValidationError(
                "AI dimension-value alias suggestion is not configured",
                code="AI_ALIAS_SUGGESTER_UNAVAILABLE",
            )
        candidates = tuple(
            DimensionValueCandidate(
                id=f"alias_{content_hash({'value_id': item.id}).removeprefix('sha256:')[:24]}",
                dimension_value_id=item.id,
                dimension_id=item.dimension_id,
                value=item.value,
                current=True,
                display_name=item.display_name,
                aliases=item.aliases,
                enabled=item.enabled,
            )
            for item in values
        )
        values_by_id = {item.id: item for item in values}
        suggestions = self._dimension_alias_suggester.suggest(
            revision=revision,
            candidates=candidates,
            tenant_id=tenant_id,
        )
        drafts = []
        for candidate in candidates:
            suggestion = suggestions.get(candidate.id, {})
            current = values_by_id[candidate.dimension_value_id]
            drafts.append(
                SemanticAliasDraft(
                    resource_type="dimension_value",
                    resource_id=current.id,
                    resource_name=current.display_name,
                    display_name=str(suggestion.get("display_name", current.display_name)),
                    aliases=tuple(
                        dict.fromkeys((*current.aliases, *tuple(suggestion.get("aliases", ()))))
                    ),
                )
            )
        return tuple(drafts)

    @staticmethod
    def _catalog(revision: ModelingRevision) -> SemanticCatalog:
        if revision.semantic_catalog is None:
            raise SemanticValidationError(
                "AI modeling requires an authoritative semantic Catalog",
                code="MODELING_CATALOG_REQUIRED",
            )
        return revision.semantic_catalog

    @staticmethod
    def _stale(message: str) -> None:
        raise SemanticValidationError(message, code="AI_MODELING_ARTIFACT_STALE")


def ensure_default_count_metrics(
    catalog: SemanticCatalog,
) -> tuple[SemanticCatalog, tuple[MetricContract, ...]]:
    """每个模型一个确定性的计数指标。

    评审决定：不让模型自己写裸 ``COUNT(*)``，计数必须绑定到受治理指标上。

    数什么由数据说了算——有数据库确认的主标识就数那把键（实体数），没有就数行
    （这张表有多少条记录）。**两者都不猜实体键**：没有主标识时，指标说的是行数，
    不声称自己是实体数。这是与 Cube 一致的条件性要求：独立一张表照样可查，只有
    参与一对多连接且带可加度量时才必须有主键。

    2026-09-16 之前只有主标识模型才有计数，连带后果是没有主标识的表整表问不到，
    于是建模者被逼着给每张表指一个主标识——而 AI 总能指出一个来。
    """

    # A one-click proposal is allowed to complete metrics before DataSet/Term
    # membership is rebuilt.  Compile only the model-owned resources here so stale
    # downstream references cannot prevent deterministic count construction.
    structural_catalog = catalog.model_copy(
        update={
            "data_sets": (),
            "terms": (),
            "dimension_values": (),
            "analysis_topic_routes": (),
            # QueryRule is scoped by Dataset.  This compile exists only to
            # derive model-owned default counts, so keeping otherwise-valid
            # rules after temporarily removing every Dataset would make the
            # structural projection invalid.  The authoritative rules remain
            # on ``catalog`` and are restored by the final Scope reconcile.
            "query_rules": (),
        }
    )
    release = compile_semantic_catalog(structural_catalog)
    models = {item.id: item for item in catalog.models}
    fields_by_id = {item.id: item for item in release.fields}
    primary_by_model: dict[str, list] = {}
    for field in release.fields:
        if field.kind is FieldKind.IDENTIFIER and field.identifier_type == "primary":
            primary_by_model.setdefault(field.model_id, []).append(field)

    # 每个模型都要有：主标识决定数键还是数行，不决定有没有计数。
    desired_by_model = {model_id: default_count_metric_id(model_id) for model_id in models}
    owned_by_model: dict[str, list[MetricContract]] = defaultdict(list)
    for item in catalog.metrics:
        if _is_default_count_metric(item):
            owned_by_model[item.model_id].append(item)
    # 按模型认领：同一模型只留一个默认计数。ID 不对的（旧格式带列名、或早先按
    # 别的主标识派生的）改名到只跟模型走的 ID，名字、别名、描述、上下文全部跟着走；
    # 模型没有主标识了才算失效。
    renamed_default_ids: dict[str, str] = {}
    stale_default_ids: set[str] = set()
    for model_id, owned in owned_by_model.items():
        wanted = desired_by_model.get(model_id)
        if wanted is None:
            stale_default_ids.update(item.id for item in owned)
            continue
        kept = next((item for item in owned if item.id == wanted), None)
        if kept is None:
            kept = sorted(owned, key=lambda item: item.id)[0]
            renamed_default_ids[kept.id] = wanted
        stale_default_ids.update(item.id for item in owned if item is not kept)
    if stale_default_ids:
        for metric in catalog.metrics:
            params = metric.metric_define_by_metric_params
            if params is not None and any(item.id in stale_default_ids for item in params.metrics):
                raise SemanticValidationError(
                    f"derived metric {metric.id} depends on a retired default count metric",
                    code="DEFAULT_COUNT_METRIC_IN_USE",
                )
        data_sets = tuple(
            item.model_copy(
                update={
                    "data_set_detail": item.data_set_detail.model_copy(
                        update={
                            "data_set_model_configs": tuple(
                                config.model_copy(
                                    update={
                                        "metrics": tuple(
                                            metric_id
                                            for metric_id in config.metrics
                                            if metric_id not in stale_default_ids
                                        )
                                    }
                                )
                                for config in item.data_set_detail.data_set_model_configs
                            )
                        }
                    )
                }
            )
            for item in catalog.data_sets
        )
        catalog = SemanticCatalog.model_validate(
            catalog.model_copy(
                update={
                    "metrics": tuple(
                        item for item in catalog.metrics if item.id not in stale_default_ids
                    ),
                    "data_sets": data_sets,
                    "terms": tuple(
                        item.model_copy(
                            update={
                                "metric_ids": tuple(
                                    metric_id
                                    for metric_id in item.metric_ids
                                    if metric_id not in stale_default_ids
                                )
                            }
                        )
                        for item in catalog.terms
                    ),
                    "semantic_context": tuple(
                        item
                        for item in catalog.semantic_context
                        if not (
                            item.target_type == "metric" and item.target_id in stale_default_ids
                        )
                    ),
                    "analysis_topic_routes": tuple(
                        item.model_copy(update={"default_count_metric_id": None})
                        if item.default_count_metric_id in stale_default_ids
                        else item
                        for item in catalog.analysis_topic_routes
                    ),
                }
            ).model_dump(mode="python")
        )

    if renamed_default_ids:
        catalog = _remap_metric_ids(catalog, renamed_default_ids)

    existing = {item.id: item for item in catalog.metrics}
    updated = catalog
    default_metrics: list[MetricContract] = []
    for model_id in sorted(models):
        primary = (
            sorted(primary_by_model[model_id], key=lambda item: item.id)[0]
            if model_id in primary_by_model
            else None
        )
        metric_id = desired_by_model[model_id]
        model = models[model_id]
        if primary is not None:
            physical = fields_by_id[primary.id]
            params = MetricDefineByFieldParamsContract(
                expr=f"COUNT({quote_sql_identifier(physical.column)})",
                fields=(FieldParamContract(field_name=physical.column),),
            )
            description = f"按{model.name}主标识统计的记录数量"
        else:
            # 没有主标识就只数行，不假装这张表的一行等于一个实体。
            params = MetricDefineByFieldParamsContract(expr="COUNT(*)", fields=())
            description = f"{model.name}的记录行数（该模型未配置主标识，数的是行不是实体）"
        # 名字两种情形保持一致：用户问「有多少条」的说法不随建模状态变。
        canonical = MetricContract(
            id=metric_id,
            name=f"{model.name}数量",
            biz_name=f"{model.biz_name[:220]}_count",
            description=description,
            model_id=model_id,
            metric_define_type=MetricDefineType.FIELD,
            metric_define_by_field_params=params,
            ext={
                "knowflow": {
                    "role": "default_count",
                    "sourceFieldId": primary.id if primary is not None else None,
                    "contractVersion": "governed-default-count-v1",
                }
            },
        )
        if metric_id in existing:
            metric = _rederive_default_count(
                existing[metric_id],
                canonical=canonical,
                primary_field_id=primary.id if primary is not None else None,
            )
            if metric is not existing[metric_id]:
                updated = _put_metric(updated, metric)
                existing[metric_id] = metric
            default_metrics.append(metric)
            continue
        metric = canonical
        updated = _put_metric(updated, metric)
        default_metrics.append(metric)
        existing[metric.id] = metric
    return updated, tuple(default_metrics)


def _put_metric(catalog: SemanticCatalog, metric: MetricContract) -> SemanticCatalog:
    """写入一个默认计数，不重新校验整份目录。

    与上面那次结构化编译同一个理由：这一步跑在目录编辑之后、作用域重编译之前，
    此时下游 Dataset 可能还指着已经删掉的指标。派生默认计数不该被那份待修复的
    引用挡住——修复它正是紧接着 `reconcile_query_scopes` 要做的事。
    """

    replaced = tuple(item if item.id != metric.id else metric for item in catalog.metrics)
    if all(item.id != metric.id for item in catalog.metrics):
        replaced = (*replaced, metric)
    return catalog.model_copy(update={"metrics": replaced})


def reconcile_query_scopes(catalog: SemanticCatalog) -> SemanticCatalog:
    """Recompile every compiler-owned QueryScope from authoritative Catalog facts.

    Scope membership and routes are derived output.  Compile the structural
    resources first so stale downstream Dataset/Route references cannot prevent
    reconciliation, then apply the complete deterministic proposal while
    preserving reviewed scope context by root.
    """

    counted, _default_counts = ensure_default_count_metrics(catalog)
    structural_recovery = False
    try:
        # A valid existing projection lets the proposer retain stable legacy
        # Dataset IDs while converting them into compiler-owned rooted Scopes.
        projection = compile_semantic_catalog(counted)
    except (KeyError, ValueError, SemanticValidationError):
        structural_recovery = True
        # A Scope-sensitive edit may retire a member before downstream Dataset
        # references are reconciled.  Structural compilation is the recovery
        # path; `_apply_topics` removes the compiler-owned stale projection.
        # 数据集清空后，指向它们的作用域上下文也要一起离开这份临时目录，否则
        # 校验器会在这里就以「targets an unknown query_scope」拒绝。上下文本身
        # 没有丢：`_apply_topics` 拿的是完整的 `counted`，按新作用域重新挂回去，
        # 只有事实根真的退役时才清掉。
        structural = counted.model_copy(
            update={
                "data_sets": (),
                "terms": (),
                "dimension_values": (),
                "analysis_topic_routes": (),
                "query_rules": (),
                "semantic_context": tuple(
                    item for item in counted.semantic_context if item.target_type != "query_scope"
                ),
            }
        )
        projection = compile_semantic_catalog(
            SemanticCatalog.model_validate(structural.model_dump(mode="python"))
        )
    proposals = _preserve_query_scope_identity(
        AnalysisTopicProposer().propose(projection),
        catalog=counted,
    )
    return OneClickModelingArtifactService._apply_topics(
        catalog=counted,
        projection=projection,
        datasets=tuple(item.dataset for item in proposals),
        routes=tuple(item.route for item in proposals),
        preserve_reviewed_dataset_metadata=structural_recovery,
    )


def _preserve_dataset_metadata(
    compiled: DataSetContract,
    *,
    previous: DataSetContract | None,
    enabled: bool,
) -> DataSetContract:
    """Keep reviewed non-derived fields while replacing Scope membership.

    During structural recovery the fresh proposal has compiler defaults because
    the stale Dataset could not enter the temporary projection.  Only
    ``data_set_detail`` is derived; every other persisted field remains the
    user's reviewed Catalog metadata.
    """

    if not enabled or previous is None:
        return compiled
    return compiled.model_copy(
        update={
            "name": previous.name,
            "biz_name": previous.biz_name,
            "description": previous.description,
            "status": previous.status,
            "type_enum": previous.type_enum,
            "sensitive_level": previous.sensitive_level,
            "domain_id": previous.domain_id,
            "alias": previous.alias,
            "query_config": previous.query_config,
            "admins": previous.admins,
            "admin_orgs": previous.admin_orgs,
        }
    )


def _preserve_query_scope_identity(
    proposals: tuple[AnalysisTopicProposal, ...],
    *,
    catalog: SemanticCatalog,
) -> tuple[AnalysisTopicProposal, ...]:
    """Keep a surviving fact root's reviewed Dataset ID during recovery.

    Structural recovery intentionally compiles without stale Dataset/Route
    objects.  Without this projection-only rebinding, a relation or member edit
    would replace an existing reviewed Scope ID with the compiler's fresh stable
    ID, orphaning QueryRules and context even though the fact root still exists.
    A root with zero or multiple historical routes is not guessed here; the
    normal conflict validation remains authoritative.
    """

    routes_by_root: dict[str, list[AnalysisTopicRouteSpec]] = defaultdict(list)
    dataset_ids = {item.id for item in catalog.data_sets}
    for route in catalog.analysis_topic_routes:
        if route.dataset_id in dataset_ids:
            routes_by_root[route.root_model_id].append(route)
    preserved: list[AnalysisTopicProposal] = []
    for proposal in proposals:
        existing = routes_by_root.get(proposal.route.root_model_id, ())
        if len(existing) != 1:
            preserved.append(proposal)
            continue
        dataset_id = existing[0].dataset_id
        if dataset_id == proposal.dataset.id:
            preserved.append(proposal)
            continue
        preserved.append(
            proposal.model_copy(
                update={
                    "dataset": proposal.dataset.model_copy(update={"id": dataset_id}),
                    "route": proposal.route.model_copy(update={"dataset_id": dataset_id}),
                }
            )
        )
    return tuple(preserved)


def _compile_semantic_context_drafts(
    catalog: SemanticCatalog,
) -> tuple[SemanticContextEntry, ...]:
    """Project existing catalog prose into reviewable, source-labeled context.

    This stage copies exact governed text only; it does not summarize, retrieve,
    infer a filter, or invent business meaning. The complete tuple is hashed in
    the one-click artifact and enters the Candidate only after that artifact is
    reviewed. Existing context wins for the same target layer.
    """

    entries = list(catalog.semantic_context)
    occupied = {(item.target_type, item.target_id, item.kind) for item in entries}
    candidates: list[tuple[str, str, str, str]] = []
    candidates.extend(
        ("model", item.id, "definition", item.description)
        for item in catalog.models
        if item.description.strip()
    )
    candidates.extend(
        ("metric", item.id, "definition", item.description)
        for item in catalog.metrics
        if item.description.strip()
    )
    candidates.extend(
        ("dimension", item.id, "definition", item.description)
        for item in catalog.dimensions
        if item.description.strip()
    )
    candidates.extend(
        ("query_scope", item.id, "scope", item.description)
        for item in catalog.data_sets
        if item.description.strip()
    )
    candidates.extend(
        ("query_scope", item.dataset_id, "convention", item.ai_context)
        for item in catalog.analysis_topic_routes
        if item.ai_context.strip()
    )
    for target_type, target_id, kind, raw_text in sorted(candidates):
        key = (target_type, target_id, kind)
        if key in occupied:
            continue
        text = raw_text.strip()
        digest = content_hash(
            {
                "target_type": target_type,
                "target_id": target_id,
                "kind": kind,
                "text": text,
            }
        ).removeprefix("sha256:")
        entries.append(
            SemanticContextEntry(
                id=f"context_{digest[:32]}",
                target_type=target_type,
                target_id=target_id,
                kind=kind,
                text=text,
                # Review state is tracked separately on ModelingRevision. Do
                # not relabel AI/catalog prose as a human-authored convention.
                source_type="catalog_description",
            )
        )
        occupied.add(key)
    return tuple(entries)


def _compile_query_scope_diagnostics(
    proposals: Iterable[AnalysisTopicProposal],
    release: SemanticRelease,
) -> tuple[QueryScopeCompilationDiagnostic, ...]:
    diagnostics = []
    for proposal in sorted(proposals, key=lambda item: item.dataset.id):
        route = proposal.route
        dataset = proposal.dataset
        diagnostics.append(
            QueryScopeCompilationDiagnostic(
                dataset_id=dataset.id,
                root_model_id=route.root_model_id,
                model_ids=dataset.model_ids,
                metric_ids=dataset.metric_ids,
                dimension_ids=dataset.dimension_ids,
                default_count_metric_id=route.default_count_metric_id,
                path_relation_ids=tuple(item.relation_ids for item in route.paths),
                canonical_names=dict(sorted(scope_canonical_names(release, route).items())),
                exclusions=tuple(
                    QueryScopeExclusionDiagnostic(
                        element_id=item.element_id,
                        reason_code=item.reason_code,
                    )
                    for item in proposal.exclusions
                ),
            )
        )
    return tuple(diagnostics)


def _with_entity_name_alias(dimension, *, noun: str | None):
    """Append the bare entity noun to a dimension's alias list, idempotently."""

    if not noun:
        return dimension
    existing = [item for item in (dimension.alias or "").split(",") if item.strip()]
    if any(_canonical_name(item) == _canonical_name(noun) for item in existing):
        return dimension
    return dimension.model_copy(update={"alias": ",".join((*existing, noun))})


class EntityNameResolution(FrozenModel):
    """One deterministic entity-name decision, reviewable and diagnosable."""

    model_id: str
    status: Literal[
        "applied",
        "already_named",
        "no_candidate",
        "multiple_candidates",
        "name_taken",
    ]
    column: str | None = None
    old_name: str | None = None
    new_name: str | None = None


def ensure_entity_name_dimensions(
    catalog: SemanticCatalog,
) -> tuple[SemanticCatalog, tuple[EntityNameResolution, ...]]:
    """Give every confirmed entity a compiler-named entity-name dimension.

    「各X的Y」是问数最高频句型，它依赖的正是实体名称维度；这类名字是 schema
    可推导事实，不该由 AI 自由发挥（城市/图书馆事故：图书馆.名称 被 AI 借名成
    「城市名称」，整个目录没有「图书馆名称」，9/12 题因此全错）。规则与
    ``ensure_default_count_metrics`` 完全对称：只在确认主标识的实体上执行；
    识别列确定性优先（非标识分类维度中物理列名命中 名称/name/title 恰好一个
    才采纳），命中即把维度规范名定为 ``{模型名}名称``；0 个或多个候选一律
    不猜，交给诊断暴露。只挂 AI proposal 动作，人工建模路径不被覆盖。
    """

    generic = {_canonical_name(item) for item in _GENERIC_NAME_COLUMNS}
    resolutions: list[EntityNameResolution] = []
    renames: dict[tuple[str, str], str] = {}
    # 裸实体名（「图书馆」）作为实体名称维度的召回词：AI 生成的别名永远是
    # 名字变体（馆名/图书馆名），而用户按实体分组时说的正是实体本名。这是
    # schema 可推导事实，与维度名本身同属编译器职责（r2 实测：滑窗+别名双缺
    # 导致 5 题静默错答）。
    entity_nouns: dict[tuple[str, str], str] = {}

    taken: dict[str, int] = defaultdict(int)
    for model in catalog.models:
        for dimension in model.model_detail.dimensions:
            taken[_canonical_name(dimension.name)] += 1
        for measure in model.model_detail.measures:
            taken[_canonical_name(measure.name)] += 1
    for dimension in catalog.dimensions:
        taken[_canonical_name(dimension.name)] += 1
    for metric in catalog.metrics:
        taken[_canonical_name(metric.name)] += 1

    for model in catalog.models:
        has_primary = any(
            item.type is IdentifierType.PRIMARY for item in model.model_detail.identifiers
        )
        if not has_primary:
            continue
        candidates = [
            item
            for item in model.model_detail.dimensions
            if item.type is ModelDimensionType.CATEGORICAL and _canonical_name(item.expr) in generic
        ]
        if not candidates:
            resolutions.append(EntityNameResolution(model_id=model.id, status="no_candidate"))
            continue
        if len(candidates) > 1:
            resolutions.append(
                EntityNameResolution(model_id=model.id, status="multiple_candidates")
            )
            continue
        candidate = candidates[0]
        target = entity_name_dimension_name(model.name)
        if _canonical_name(candidate.name) == _canonical_name(target):
            entity_nouns[(model.id, candidate.expr)] = model.name
            resolutions.append(
                EntityNameResolution(
                    model_id=model.id,
                    status="already_named",
                    column=candidate.expr,
                    old_name=candidate.name,
                    new_name=candidate.name,
                )
            )
            continue
        # 目标名被其它资源占用时不强改——冲突留给命名诊断，而不是制造新冲突。
        # 计数扣除同列受治理维度自身（改名会一并换掉它的旧名）。
        occupied = taken.get(_canonical_name(target), 0)
        if occupied > 0:
            resolutions.append(
                EntityNameResolution(
                    model_id=model.id,
                    status="name_taken",
                    column=candidate.expr,
                    old_name=candidate.name,
                    new_name=target,
                )
            )
            continue
        renames[(model.id, candidate.expr)] = target
        entity_nouns[(model.id, candidate.expr)] = model.name
        resolutions.append(
            EntityNameResolution(
                model_id=model.id,
                status="applied",
                column=candidate.expr,
                old_name=candidate.name,
                new_name=target,
            )
        )

    if not renames and not entity_nouns:
        return catalog, tuple(resolutions)

    updated_models = tuple(
        (
            model.model_copy(
                update={
                    "model_detail": model.model_detail.model_copy(
                        update={
                            "dimensions": tuple(
                                (
                                    item.model_copy(update={"name": renames[(model.id, item.expr)]})
                                    if (model.id, item.expr) in renames
                                    else item
                                )
                                for item in model.model_detail.dimensions
                            )
                        }
                    )
                }
            )
            if any(model_id == model.id for model_id, _column in renames)
            else model
        )
        for model in catalog.models
    )
    updated_dimensions = tuple(
        _with_entity_name_alias(
            (
                item.model_copy(update={"name": renames[(item.model_id, item.expr)]})
                if (item.model_id, item.expr) in renames
                else item
            ),
            noun=entity_nouns.get((item.model_id, item.expr)),
        )
        for item in catalog.dimensions
    )
    updated = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"models": updated_models, "dimensions": updated_dimensions}
        ).model_dump(mode="python")
    )
    return updated, tuple(resolutions)


_GENERIC_NAME_COLUMNS = frozenset({"名称", "名字", "name", "title", "姓名", "全称"})


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in value)


def preserve_source_column_aliases(
    drafts: tuple[SemanticAliasDraft, ...],
    *,
    metrics: Iterable,
    dimensions: Iterable,
    fields: Iterable,
) -> tuple[SemanticAliasDraft, ...]:
    """Keep the physical column wording recallable after an AI rename.

    「在编职工数量」被改名「在编职工人数」后，用户仍会按源列名提问；丢掉它
    等于把数据库里已经写对的说法从召回词表里删掉。规则：物理列名含中文、与
    业务名不同、且不是「名称/名字/name」这类所有实体共享的通用词时，确定性
    补进该资源的别名草稿。零模型调用，仍走人工审核。
    """

    fields_by_id = {item.id: item for item in fields}
    source_by_resource: dict[str, str] = {}
    for resource in (*metrics, *dimensions):
        field_id = getattr(resource, "field_id", None)
        if field_id is None:
            continue
        field = fields_by_id.get(field_id)
        if field is None:
            continue
        column = field.column.strip()
        if not column or not _has_cjk(column):
            continue
        if _canonical_name(column) in {_canonical_name(item) for item in _GENERIC_NAME_COLUMNS}:
            continue
        if _canonical_name(column) == _canonical_name(resource.name):
            continue
        source_by_resource[resource.id] = column
    if not source_by_resource:
        return drafts
    return tuple(
        (
            draft.model_copy(
                update={
                    "aliases": tuple(
                        dict.fromkeys((*draft.aliases, source_by_resource[draft.resource_id]))
                    )
                }
            )
            if draft.resource_id in source_by_resource
            else draft
        )
        for draft in drafts
    )


def qualify_cross_model_metric_aliases(
    drafts: tuple[SemanticAliasDraft, ...],
    *,
    metrics: Iterable,
    models: Iterable,
) -> tuple[SemanticAliasDraft, ...]:
    """Give cross-model duplicate metric names a discriminating alias draft.

    Per-model alias generation is structurally blind to sibling models, so two
    fact tables both propose a metric literally named the same word and every
    question containing it fail-closes into scope clarification. This pass is
    deterministic (no model call) and only edits reviewable alias drafts: each
    colliding metric gains ``<model.name>`` (when the metric name is already a
    substring of the model name) or ``<model.name><metric.name>``. A qualifier
    that another metric already owns as a name or alias is never written; the
    residual collision stays visible to the modeling diagnostics instead.
    """

    metric_list = tuple(metrics)
    models_by_id = {item.id: item for item in models}
    name_groups: dict[str, list] = defaultdict(list)
    for metric in metric_list:
        name_groups[_canonical_name(metric.name)].append(metric)
    colliding_metric_ids = {
        metric.id
        for group in name_groups.values()
        if len({item.model_id for item in group}) > 1
        for metric in group
    }
    if not colliding_metric_ids:
        return drafts

    owners: dict[str, set[str]] = defaultdict(set)
    for metric in metric_list:
        for spelling in (metric.name, *metric.aliases):
            owners[_canonical_name(spelling)].add(metric.id)
    for draft in drafts:
        if draft.resource_type != "metric":
            continue
        for spelling in draft.aliases:
            owners[_canonical_name(spelling)].add(draft.resource_id)

    metrics_by_id = {item.id: item for item in metric_list}
    qualifiers: dict[str, str] = {}
    for metric_id in sorted(colliding_metric_ids):
        metric = metrics_by_id[metric_id]
        model = models_by_id.get(metric.model_id)
        if model is None:
            continue
        qualified = (
            model.name
            if _canonical_name(metric.name) in _canonical_name(model.name)
            else f"{model.name}{metric.name}"
        )
        if owners[_canonical_name(qualified)] - {metric_id}:
            continue
        qualifiers[metric_id] = qualified
    qualifier_counts = Counter(_canonical_name(item) for item in qualifiers.values())
    qualifiers = {
        metric_id: qualified
        for metric_id, qualified in qualifiers.items()
        if qualifier_counts[_canonical_name(qualified)] == 1
    }

    return tuple(
        (
            draft.model_copy(
                update={
                    "aliases": tuple(dict.fromkeys((*draft.aliases, qualifiers[draft.resource_id])))
                }
            )
            if draft.resource_type == "metric" and draft.resource_id in qualifiers
            else draft
        )
        for draft in drafts
    )


def _is_default_count_metric(metric: MetricContract) -> bool:
    metadata = metric.ext.get("knowflow") if isinstance(metric.ext, dict) else None
    return isinstance(metadata, dict) and metadata.get("role") == "default_count"


def _remap_metric_ids(catalog: SemanticCatalog, mapping: dict[str, str]) -> SemanticCatalog:
    """把指标改名到新 ID，并改掉目录里每一处对它的引用。"""

    def one(metric_id: str) -> str:
        return mapping.get(metric_id, metric_id)

    metrics = []
    for item in catalog.metrics:
        params = item.metric_define_by_metric_params
        if params is not None and any(dep.id in mapping for dep in params.metrics):
            params = params.model_copy(
                update={
                    "metrics": tuple(
                        dep.model_copy(update={"id": one(dep.id)}) for dep in params.metrics
                    )
                }
            )
            item = item.model_copy(update={"metric_define_by_metric_params": params})
        if item.id in mapping:
            item = item.model_copy(update={"id": one(item.id)})
        metrics.append(item)
    data_sets = tuple(
        item.model_copy(
            update={
                "data_set_detail": item.data_set_detail.model_copy(
                    update={
                        "data_set_model_configs": tuple(
                            config.model_copy(
                                update={"metrics": tuple(one(m) for m in config.metrics)}
                            )
                            for config in item.data_set_detail.data_set_model_configs
                        )
                    }
                )
            }
        )
        for item in catalog.data_sets
    )
    terms = tuple(
        item.model_copy(update={"metric_ids": tuple(one(m) for m in item.metric_ids)})
        for item in catalog.terms
    )
    semantic_context = tuple(
        item.model_copy(update={"target_id": one(item.target_id)})
        if item.target_type == "metric"
        else item
        for item in catalog.semantic_context
    )
    routes = tuple(
        item.model_copy(update={"default_count_metric_id": one(item.default_count_metric_id)})
        if item.default_count_metric_id is not None
        else item
        for item in catalog.analysis_topic_routes
    )
    query_rules = tuple(
        item.model_copy(
            update={
                "parameters": tuple(
                    one(value) if isinstance(value, str) else value for value in item.parameters
                ),
                "outputs": tuple(one(value) for value in item.outputs),
            }
        )
        for item in catalog.query_rules
    )
    return SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "metrics": tuple(metrics),
                "data_sets": data_sets,
                "terms": terms,
                "semantic_context": semantic_context,
                "analysis_topic_routes": routes,
                "query_rules": query_rules,
            }
        ).model_dump(mode="python")
    )


def _rederive_default_count(
    metric: MetricContract,
    *,
    canonical: MetricContract,
    primary_field_id: str | None,
) -> MetricContract:
    """主标识换了列就按新列重派生表达式，名字、别名这些审过的东西不动。

    列没换则沿用原来的冲突校验：编译器保留的指标不许被改成别的东西。

    描述有一个例外：数键改成数行（或反过来）时必须跟着改。「按客户主标识统计的记录
    数量」在一个改成数行的指标上是一句关于数据的假话，而描述会进模型提示词。
    """

    metadata = metric.ext.get("knowflow") if isinstance(metric.ext, dict) else None
    if isinstance(metadata, dict) and metadata.get("sourceFieldId") != primary_field_id:
        basis_changed = (metadata.get("sourceFieldId") is None) != (primary_field_id is None)
        return metric.model_copy(
            update={
                "model_id": canonical.model_id,
                "metric_define_type": canonical.metric_define_type,
                "metric_define_by_field_params": canonical.metric_define_by_field_params,
                "description": canonical.description if basis_changed else metric.description,
                "ext": {**metric.ext, "knowflow": {**metadata, "sourceFieldId": primary_field_id}},
            }
        )
    _validate_existing_default_count(metric, canonical=canonical, primary_field_id=primary_field_id)
    return metric


def _validate_existing_default_count(
    metric: MetricContract,
    *,
    canonical: MetricContract,
    primary_field_id: str | None,
) -> None:
    params = metric.metric_define_by_field_params
    canonical_params = canonical.metric_define_by_field_params
    metadata = metric.ext.get("knowflow") if isinstance(metric.ext, dict) else None
    valid = (
        metric.model_id == canonical.model_id
        and metric.metric_define_type is MetricDefineType.FIELD
        and params is not None
        and canonical_params is not None
        and params.expr == canonical_params.expr
        and params.fields == canonical_params.fields
        and params.filter_sql is None
        and isinstance(metadata, dict)
        and metadata.get("role") == "default_count"
        and metadata.get("sourceFieldId") == primary_field_id
    )
    if not valid:
        raise SemanticValidationError(
            f"reserved default-count metric conflicts with its primary identifier: {metric.id}",
            code="DEFAULT_COUNT_METRIC_CONFLICT",
        )


def apply_semantic_alias_drafts(
    catalog: SemanticCatalog,
    drafts: Iterable[SemanticAliasDraft],
) -> SemanticCatalog:
    """Apply stored alias drafts without rerunning a model at commit time."""

    items = tuple(drafts)
    keys = [(item.resource_type, item.resource_id) for item in items]
    if len(keys) != len(set(keys)):
        raise SemanticValidationError(
            "AI modeling alias drafts contain duplicate resources",
            code="AI_MODELING_ALIAS_DRAFT_DUPLICATE",
        )
    updated = catalog
    for draft in items:
        if draft.resource_type == "dimension":
            current = next(
                (item for item in updated.dimensions if item.id == draft.resource_id),
                None,
            )
            if current is None:
                _missing_alias_resource(draft)
            item = current.model_copy(
                update={
                    "alias": _alias_csv(draft.aliases),
                    "ext": _alias_review_ext(current.ext),
                }
            )
            updated = replace_catalog_item(updated, collection="dimensions", item=item)
        elif draft.resource_type == "metric":
            current = next((item for item in updated.metrics if item.id == draft.resource_id), None)
            if current is None:
                _missing_alias_resource(draft)
            item = current.model_copy(
                update={
                    "alias": _alias_csv(draft.aliases),
                    "ext": _alias_review_ext(current.ext),
                }
            )
            updated = replace_catalog_item(updated, collection="metrics", item=item)
        else:
            current = next(
                (item for item in updated.dimension_values if item.id == draft.resource_id),
                None,
            )
            if current is None:
                _missing_alias_resource(draft)
            item = DimensionValueSpec(
                id=current.id,
                dimension_id=current.dimension_id,
                value=current.value,
                display_name=draft.display_name or current.display_name,
                aliases=draft.aliases,
                enabled=current.enabled,
            )
            updated = replace_catalog_item(updated, collection="dimension_values", item=item)
    return SemanticCatalog.model_validate(updated.model_dump(mode="python"))


def _queryable_resources(release: SemanticRelease) -> set[tuple[str, str]]:
    """可问的维度、指标、取值；AI 产物必须为它们每一个都带别名草稿。"""

    dimensions = {item.id: item for item in release.dimensions}
    queryable_dimensions = {
        ("dimension", item_id)
        for dataset in release.datasets
        for item_id in dataset.dimension_ids
        if dimensions[item_id].semantic_type != "identifier"
    }
    queryable_metrics = {
        ("metric", item_id) for dataset in release.datasets for item_id in dataset.metric_ids
    }
    queryable_values = {
        ("dimension_value", item.id)
        for item in release.dimension_values
        if ("dimension", item.dimension_id) in queryable_dimensions
    }
    return queryable_dimensions | queryable_metrics | queryable_values


def validate_ai_modeling_completeness(
    release: SemanticRelease,
    *,
    alias_drafts: Iterable[SemanticAliasDraft] | None = None,
) -> None:
    """Fail closed when the one-click modeling artifact is not query-complete.

    别名只在 AI 产物内部检查：产物必须覆盖每个可问资源。发布门不再看别名，
    有没有别名是召回好坏，不是能不能发（2026-09-16 用户评审）。
    """

    # 2026-08-27 评审：可达的实体不与别的事实粒度共用 COUNT 语义，它保有独立的
    # 根作用域。2026-09-16 起这条扩到每张有默认计数的实表——没有主标识的表同样有
    # 自己的作用域，只是它的默认计数数的是行不是实体。事实根的判据与编译作用域
    # 时用的是同一个函数。
    routed_roots = {item.root_model_id for item in release.analysis_topic_routes}
    missing_topics = sorted(fact_root_model_ids(release) - routed_roots)
    if missing_topics:
        raise SemanticValidationError(
            f"fact roots have no analysis topic: {missing_topics[:5]}",
            code="AI_MODELING_TOPIC_COVERAGE_INCOMPLETE",
        )
    # AI 产物必然跑过 ensure_default_count_metrics，所以每个作用域都该有默认计数：
    # 没有就意味着「有多少条」答不了，而那是用户最常问的一句。
    missing_counts = sorted(
        item.dataset_id
        for item in release.analysis_topic_routes
        if item.default_count_metric_id is None
    )
    if missing_counts:
        raise SemanticValidationError(
            f"analysis topics have no governed default count metric: {missing_counts[:5]}",
            code="AI_MODELING_DEFAULT_COUNT_REQUIRED",
        )

    primary_fields_by_model = {}
    for field in sorted(release.fields, key=lambda item: item.id):
        if field.kind is FieldKind.IDENTIFIER and field.identifier_type == "primary":
            primary_fields_by_model.setdefault(field.model_id, field)
    generated_count_ids = {
        default_count_metric_id(model_id) for model_id in primary_fields_by_model
    }
    business_metric_ids = {
        item.id for item in release.metrics if item.id not in generated_count_ids
    }
    metrics = {item.id: item for item in release.metrics}
    datasets = {item.id: item for item in release.datasets}
    exposed_metric_ids = {
        metric_id
        for route in release.analysis_topic_routes
        for metric_id in datasets[route.dataset_id].metric_ids
        if metrics[metric_id].model_id == route.root_model_id
    }
    missing_metrics = sorted(business_metric_ids - exposed_metric_ids)
    if missing_metrics:
        raise SemanticValidationError(
            f"business metrics have no query scope: {missing_metrics[:5]}",
            code="AI_MODELING_METRIC_COVERAGE_INCOMPLETE",
        )

    if alias_drafts is None:
        return
    drafted = {(item.resource_type, item.resource_id) for item in alias_drafts}
    missing = sorted(_queryable_resources(release) - drafted)
    if missing:
        raise SemanticValidationError(
            f"AI modeling artifact has no alias draft for queryable resources: {missing[:5]}",
            code="AI_MODELING_ALIAS_REVIEW_INCOMPLETE",
        )


def _alias_csv(aliases: tuple[str, ...]) -> str | None:
    values = tuple(dict.fromkeys(item.strip() for item in aliases if item.strip()))
    return ",".join(values) or None


def _stable_id(prefix: str, *parts: str) -> str:
    raw = ":".join((prefix, *parts))
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _alias_review_ext(current: dict) -> dict:
    return {
        **current,
        "knowflowAliasReview": {
            "status": "reviewed",
            "source": "ai_modeling_proposal",
            "contractVersion": "semantic-alias-review-v1",
        },
    }


def _missing_alias_resource(draft: SemanticAliasDraft) -> None:
    raise SemanticValidationError(
        f"AI modeling alias target does not exist: {draft.resource_type}:{draft.resource_id}",
        code="AI_MODELING_ALIAS_TARGET_NOT_FOUND",
    )
