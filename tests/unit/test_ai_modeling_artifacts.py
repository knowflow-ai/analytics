from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicRouteSpec,
    FieldKind,
    SemanticContextEntry,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.ai_artifacts import (
    OneClickModelingArtifactService,
    apply_semantic_alias_drafts,
    ensure_default_count_metrics,
    validate_ai_modeling_completeness,
)
from knowflow_analytics.modeling.analysis_topics import AnalysisTopicProposer
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.contracts import AiModelingArtifact, SemanticAliasDraft


def test_semantic_context_is_bound_into_the_reviewed_ai_artifact_hash():
    context = SemanticContextEntry(
        id="ctx-project-currency",
        target_type="project",
        target_id="sales",
        kind="convention",
        text="金额统一使用人民币。",
        source_type="human_convention",
    )
    artifact = AiModelingArtifact.create(
        base_semantic_spec_hash="sha256:base",
        alias_drafts=(),
        dimension_values=(),
        default_count_metrics=(),
        analysis_topic_datasets=(),
        analysis_topic_routes=(),
        semantic_context=(context,),
    )

    assert artifact.semantic_context == (context,)
    changed = artifact.model_copy(
        update={"semantic_context": (context.model_copy(update={"text": "金额统一使用美元。"}),)}
    )
    with pytest.raises(ValueError, match="artifact hash"):
        AiModelingArtifact.model_validate(changed.model_dump(mode="python"))


def test_default_count_metric_is_derived_from_confirmed_primary_identifier(
    sales_catalog,
):
    catalog = sales_catalog.model_copy(
        update={
            "metrics": (),
            "data_sets": (),
            "terms": (),
            "dimension_values": (),
            "analysis_topic_routes": (),
        }
    )

    updated, generated = ensure_default_count_metrics(catalog)
    release = compile_semantic_catalog(updated)

    assert len(generated) == 3
    generated_ids = {item.id for item in generated}
    for metric in release.metrics:
        assert metric.id in generated_ids
        primary = next(
            item
            for item in release.fields
            if item.model_id == metric.model_id
            and item.kind is FieldKind.IDENTIFIER
            and item.identifier_type == "primary"
        )
        assert metric.field_id == primary.id
        assert metric.aggregation is Aggregation.COUNT
    assert all(item.ext["knowflow"]["role"] == "default_count" for item in generated)


def test_default_count_membership_is_invariant_to_names_and_catalog_order(
    sales_catalog,
):
    base = sales_catalog.model_copy(
        update={
            "metrics": (),
            "data_sets": (),
            "terms": (),
            "dimension_values": (),
            "analysis_topic_routes": (),
        }
    )
    renamed = base.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"name": f"实体 {index}", "biz_name": f"entity_{index}"})
                for index, item in enumerate(reversed(base.models))
            )
        }
    )

    _, original_metrics = ensure_default_count_metrics(base)
    _, renamed_metrics = ensure_default_count_metrics(renamed)

    assert {item.id for item in renamed_metrics} == {item.id for item in original_metrics}
    assert {item.ext["knowflow"]["sourceFieldId"] for item in renamed_metrics} == {
        item.ext["knowflow"]["sourceFieldId"] for item in original_metrics
    }

    completed, first = ensure_default_count_metrics(base)
    unchanged, second = ensure_default_count_metrics(completed)
    assert unchanged == completed
    assert second == first


def test_reserved_default_count_id_rejects_a_changed_execution_contract(sales_catalog):
    base = sales_catalog.model_copy(update={"data_sets": (), "terms": (), "dimension_values": ()})
    completed, metrics = ensure_default_count_metrics(base)
    target = metrics[0]
    params = target.metric_define_by_field_params
    assert params is not None
    corrupted = target.model_copy(
        update={
            "metric_define_by_field_params": params.model_copy(
                update={"expr": f"{params.expr} + 1"}
            )
        }
    )
    completed = completed.model_copy(
        update={
            "metrics": tuple(
                corrupted if item.id == corrupted.id else item for item in completed.metrics
            )
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        ensure_default_count_metrics(completed)

    assert raised.value.code == "DEFAULT_COUNT_METRIC_CONFLICT"


def test_no_measure_entity_receives_a_count_topic_with_a_bound_default_metric(
    sales_catalog,
):
    catalog = sales_catalog.model_copy(
        update={
            "metrics": (),
            "data_sets": (),
            "terms": (),
            "dimension_values": (),
            "analysis_topic_routes": (),
        }
    )
    catalog, _ = ensure_default_count_metrics(catalog)

    proposals = AnalysisTopicProposer().propose(compile_semantic_catalog(catalog))

    assert proposals
    for proposal in proposals:
        assert proposal.dataset.metric_ids == (proposal.route.default_count_metric_id,)


def test_alias_drafts_update_dimensions_metrics_and_dimension_values(
    sales_catalog,
):
    dimension = sales_catalog.dimensions[0]
    metric = sales_catalog.metrics[0]
    value = sales_catalog.dimension_values[0]

    updated = apply_semantic_alias_drafts(
        sales_catalog,
        (
            SemanticAliasDraft(
                resource_type="dimension",
                resource_id=dimension.id,
                aliases=("区域", "大区"),
            ),
            SemanticAliasDraft(
                resource_type="metric",
                resource_id=metric.id,
                aliases=("净收入", "收入净额"),
            ),
            SemanticAliasDraft(
                resource_type="dimension_value",
                resource_id=value.id,
                display_name=value.display_name,
                aliases=("核心客户",),
            ),
        ),
    )

    assert updated.dimensions[0].alias == "区域,大区"
    assert updated.metrics[0].alias == "净收入,收入净额"
    assert updated.dimension_values[0].aliases == ("核心客户",)


def test_ai_modeling_completeness_rejects_topic_without_default_count(
    sales_release,
):
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == "orders.id" else item
        for item in sales_release.fields
    )
    route = AnalysisTopicRouteSpec(
        dataset_id=sales_release.datasets[0].id,
        root_model_id="orders",
    )
    incomplete = sales_release.model_copy(
        update={"fields": fields, "analysis_topic_routes": (route,)}
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_ai_modeling_completeness(incomplete)

    assert raised.value.code == "AI_MODELING_DEFAULT_COUNT_REQUIRED"


def test_ai_modeling_completeness_rejects_primary_entity_without_topic(
    sales_release,
):
    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"}) if item.id == "orders.id" else item
        for item in sales_release.fields
    )
    incomplete = sales_release.model_copy(update={"fields": fields, "analysis_topic_routes": ()})

    with pytest.raises(SemanticValidationError) as raised:
        validate_ai_modeling_completeness(incomplete)

    assert raised.value.code == "AI_MODELING_TOPIC_COVERAGE_INCOMPLETE"


def test_ai_modeling_completeness_rejects_queryable_resource_without_alias_review(
    sales_release,
):
    fields = tuple(
        item.model_copy(
            update={
                "identifier_type": ("primary" if item.id == "orders.id" else item.identifier_type)
            }
        )
        for item in sales_release.fields
    )
    route = AnalysisTopicRouteSpec(
        dataset_id=sales_release.datasets[0].id,
        root_model_id="orders",
        default_count_metric_id="order_count",
    )
    release = sales_release.model_copy(update={"fields": fields, "analysis_topic_routes": (route,)})
    reviewed = tuple(
        SemanticAliasDraft(
            resource_type="dimension",
            resource_id=item.id,
            resource_name=item.name,
        )
        for item in release.dimensions
    ) + tuple(
        SemanticAliasDraft(
            resource_type="dimension_value",
            resource_id=item.id,
            resource_name=item.display_name,
            display_name=item.display_name,
        )
        for item in release.dimension_values
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_ai_modeling_completeness(release, alias_drafts=reviewed)

    assert raised.value.code == "AI_MODELING_ALIAS_REVIEW_INCOMPLETE"


def test_primary_entity_reachable_from_another_scope_still_requires_its_own_scope(
    sales_release,
):
    """Reviewed QueryScope contract: entity counts keep their own fact grain."""

    from knowflow_analytics.contracts import (
        AnalysisTopicPathSpec,
        AnalysisTopicRouteSpec,
    )
    from knowflow_analytics.modeling.ai_artifacts import (
        validate_ai_modeling_completeness,
    )

    fields = tuple(
        item.model_copy(update={"identifier_type": "primary"})
        if item.id in {"orders.id", "customers.id"}
        else item
        for item in sales_release.fields
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        default_count_metric_id="order_count",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
            ),
        ),
    )
    release = sales_release.model_copy(update={"fields": fields, "analysis_topic_routes": (route,)})

    with pytest.raises(SemanticValidationError) as raised:
        validate_ai_modeling_completeness(release)

    assert raised.value.code == "AI_MODELING_TOPIC_COVERAGE_INCOMPLETE"


def test_ai_modeling_completeness_accepts_a_metric_scope_without_primary_or_default_count(
    sales_release,
):
    proposal = AnalysisTopicProposer().propose(sales_release)[0]
    release = sales_release.model_copy(
        update={
            "datasets": (proposal.dataset,),
            "analysis_topic_routes": (proposal.route,),
        }
    )

    validate_ai_modeling_completeness(release)

    assert proposal.route.default_count_metric_id is None


def test_ai_modeling_completeness_requires_every_business_metric_in_a_scope(sales_release):
    proposal = AnalysisTopicProposer().propose(sales_release)[0]
    omitted_metric_id = proposal.dataset.metric_ids[-1]
    incomplete_dataset = proposal.dataset.model_copy(
        update={
            "metric_ids": tuple(
                metric_id
                for metric_id in proposal.dataset.metric_ids
                if metric_id != omitted_metric_id
            )
        }
    )
    release = sales_release.model_copy(
        update={
            "datasets": (incomplete_dataset,),
            "analysis_topic_routes": (proposal.route,),
        }
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_ai_modeling_completeness(release)

    assert raised.value.code == "AI_MODELING_METRIC_COVERAGE_INCOMPLETE"


def test_scope_application_preserves_context_without_replacement_and_accepts_explicit_one(
    sales_catalog,
):
    reviewed_route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        ai_context="reviewed context",
    )
    catalog = sales_catalog.model_copy(update={"analysis_topic_routes": (reviewed_route,)})
    projection = compile_semantic_catalog(catalog)
    proposal = next(
        item
        for item in AnalysisTopicProposer().propose(projection)
        if item.route.root_model_id == "orders"
    )

    preserved = OneClickModelingArtifactService._apply_topics(
        catalog=catalog,
        projection=projection,
        datasets=(proposal.dataset,),
        routes=(proposal.route.model_copy(update={"ai_context": ""}),),
    )
    replaced = OneClickModelingArtifactService._apply_topics(
        catalog=catalog,
        projection=projection,
        datasets=(proposal.dataset,),
        routes=(proposal.route.model_copy(update={"ai_context": "replacement context"}),),
    )

    assert preserved.analysis_topic_routes[0].ai_context == reviewed_route.ai_context
    assert replaced.analysis_topic_routes[0].ai_context == "replacement context"


def test_query_scope_recompile_removes_only_obsolete_compiler_outputs(sales_catalog):
    counted, generated = ensure_default_count_metrics(sales_catalog)
    proposals = AnalysisTopicProposer().propose(compile_semantic_catalog(counted))
    compiled = OneClickModelingArtifactService._apply_topics(
        catalog=counted,
        projection=compile_semantic_catalog(counted),
        datasets=tuple(item.dataset for item in proposals),
        routes=tuple(item.route for item in proposals),
    )
    customer_count = next(item for item in generated if item.model_id == "customers")
    customer_dataset_id = next(
        item.dataset_id
        for item in compiled.analysis_topic_routes
        if item.root_model_id == "customers"
    )
    old_compiler_dataset_ids = {item.dataset_id for item in compiled.analysis_topic_routes}
    models_without_customer_primary = tuple(
        item.model_copy(
            update={"model_detail": item.model_detail.model_copy(update={"identifiers": ()})}
        )
        if item.id == "customers"
        else item
        for item in compiled.models
    )
    changed = compiled.model_copy(update={"models": models_without_customer_primary})

    incremental_counts, _ = ensure_default_count_metrics(changed)
    incremental_proposals = AnalysisTopicProposer().propose(
        compile_semantic_catalog(incremental_counts)
    )
    incremental = OneClickModelingArtifactService._apply_topics(
        catalog=incremental_counts,
        projection=compile_semantic_catalog(incremental_counts),
        datasets=tuple(item.dataset for item in incremental_proposals),
        routes=tuple(item.route for item in incremental_proposals),
    )

    compiler_metric_ids = {
        item.id
        for item in changed.metrics
        if isinstance(item.ext.get("knowflow"), dict)
        and item.ext["knowflow"].get("role") == "default_count"
    }
    clean_input = changed.model_copy(
        update={
            "metrics": tuple(
                item for item in changed.metrics if item.id not in compiler_metric_ids
            ),
            "data_sets": tuple(
                item for item in changed.data_sets if item.id not in old_compiler_dataset_ids
            ),
            "analysis_topic_routes": (),
            "terms": (),
        }
    )
    clean_counts, _ = ensure_default_count_metrics(clean_input)
    clean_proposals = AnalysisTopicProposer().propose(compile_semantic_catalog(clean_counts))
    clean = OneClickModelingArtifactService._apply_topics(
        catalog=clean_counts,
        projection=compile_semantic_catalog(clean_counts),
        datasets=tuple(item.dataset for item in clean_proposals),
        routes=tuple(item.route for item in clean_proposals),
    )

    assert customer_count.id not in {item.id for item in incremental.metrics}
    assert "customers" not in {item.root_model_id for item in incremental.analysis_topic_routes}
    assert customer_dataset_id not in {item.id for item in incremental.data_sets}
    assert incremental.metrics == clean.metrics

    def scope_manifest(catalog):
        datasets = {item.id: item for item in catalog.data_sets}
        return {
            route.root_model_id: {
                "members": tuple(
                    (
                        item.id,
                        item.metrics,
                        item.dimensions,
                    )
                    for item in datasets[route.dataset_id].data_set_detail.data_set_model_configs
                ),
                "default_count_metric_id": route.default_count_metric_id,
                "paths": tuple((item.target_model_id, item.relation_ids) for item in route.paths),
            }
            for route in catalog.analysis_topic_routes
        }

    assert scope_manifest(incremental) == scope_manifest(clean)


def _metric(metric_id: str, name: str, model_id: str, *, aliases: tuple[str, ...] = ()):
    from knowflow_analytics.contracts import MetricSpec

    return MetricSpec(
        id=metric_id,
        name=name,
        model_id=model_id,
        field_id=f"field:{metric_id}",
        aggregation=Aggregation.SUM,
        aliases=aliases,
    )


def _model(model_id: str, name: str):
    from knowflow_analytics.contracts import ModelSpec

    return ModelSpec(id=model_id, name=name, table=model_id, schema_name="public")


def _draft(metric_id: str, name: str, aliases: tuple[str, ...]) -> SemanticAliasDraft:
    return SemanticAliasDraft(
        resource_type="metric",
        resource_id=metric_id,
        resource_name=name,
        aliases=aliases,
    )


def test_cross_model_metric_name_collision_gains_a_qualified_alias():
    from knowflow_analytics.modeling.ai_artifacts import qualify_cross_model_metric_aliases

    metrics = (
        _metric("metric:merchant:turnover", "交易额", "model:merchant"),
        _metric("metric:platform:turnover", "交易额", "model:platform"),
        _metric("metric:platform:merchants", "参加活动商家数量", "model:platform"),
    )
    models = (
        _model("model:merchant", "商家交易额"),
        _model("model:platform", "电商活动交易额"),
    )
    drafts = (
        _draft("metric:merchant:turnover", "交易额", ("成交额", "销售额")),
        _draft("metric:platform:turnover", "交易额", ("成交额", "营业收入")),
        _draft("metric:platform:merchants", "参加活动商家数量", ("参与商家数",)),
    )

    qualified = qualify_cross_model_metric_aliases(drafts, metrics=metrics, models=models)

    by_id = {item.resource_id: item for item in qualified}
    # 指标名是模型名的子串时，直接用模型名作限定别名。
    assert by_id["metric:merchant:turnover"].aliases == ("成交额", "销售额", "商家交易额")
    assert by_id["metric:platform:turnover"].aliases == (
        "成交额",
        "营业收入",
        "电商活动交易额",
    )
    # 未重名的指标草稿保持原样。
    assert by_id["metric:platform:merchants"].aliases == ("参与商家数",)


def test_metric_name_not_contained_in_model_name_gets_prefixed_alias():
    from knowflow_analytics.modeling.ai_artifacts import qualify_cross_model_metric_aliases

    metrics = (
        _metric("metric:store:sales", "销售额", "model:store"),
        _metric("metric:online:sales", "销售额", "model:online"),
    )
    models = (_model("model:store", "门店"), _model("model:online", "线上渠道"))
    drafts = (
        _draft("metric:store:sales", "销售额", ()),
        _draft("metric:online:sales", "销售额", ()),
    )

    qualified = qualify_cross_model_metric_aliases(drafts, metrics=metrics, models=models)

    by_id = {item.resource_id: item for item in qualified}
    assert by_id["metric:store:sales"].aliases == ("门店销售额",)
    assert by_id["metric:online:sales"].aliases == ("线上渠道销售额",)


def test_qualified_alias_that_still_collides_is_not_written():
    from knowflow_analytics.modeling.ai_artifacts import qualify_cross_model_metric_aliases

    metrics = (
        _metric("metric:merchant:turnover", "交易额", "model:merchant"),
        _metric("metric:platform:turnover", "交易额", "model:platform"),
        # 第三个模型已有名叫「商家交易额」的指标，抢占了 merchant 的限定名。
        _metric("metric:other:merchant_turnover", "商家交易额", "model:other"),
    )
    models = (
        _model("model:merchant", "商家"),
        _model("model:platform", "平台"),
        _model("model:other", "其它"),
    )
    drafts = (
        _draft("metric:merchant:turnover", "交易额", ()),
        _draft("metric:platform:turnover", "交易额", ()),
        _draft("metric:other:merchant_turnover", "商家交易额", ()),
    )

    qualified = qualify_cross_model_metric_aliases(drafts, metrics=metrics, models=models)

    by_id = {item.resource_id: item for item in qualified}
    # merchant 的限定名「商家交易额」与既有指标名冲突，不写入，交由诊断暴露。
    assert by_id["metric:merchant:turnover"].aliases == ()
    assert by_id["metric:platform:turnover"].aliases == ("平台交易额",)
    assert by_id["metric:other:merchant_turnover"].aliases == ()


def test_existing_qualified_alias_is_not_duplicated():
    from knowflow_analytics.modeling.ai_artifacts import qualify_cross_model_metric_aliases

    metrics = (
        _metric("metric:merchant:turnover", "交易额", "model:merchant"),
        _metric("metric:platform:turnover", "交易额", "model:platform"),
    )
    models = (
        _model("model:merchant", "商家交易额"),
        _model("model:platform", "电商活动交易额"),
    )
    drafts = (
        _draft("metric:merchant:turnover", "交易额", ("商家交易额",)),
        _draft("metric:platform:turnover", "交易额", ()),
    )

    qualified = qualify_cross_model_metric_aliases(drafts, metrics=metrics, models=models)

    by_id = {item.resource_id: item for item in qualified}
    assert by_id["metric:merchant:turnover"].aliases == ("商家交易额",)
    assert by_id["metric:platform:turnover"].aliases == ("电商活动交易额",)


def test_renamed_resources_keep_their_source_column_as_alias():
    from knowflow_analytics.contracts import DimensionSpec, FieldSpec
    from knowflow_analytics.modeling.ai_artifacts import preserve_source_column_aliases

    fields = (
        FieldSpec(
            id="field:staff.headcount",
            model_id="model:staff",
            name="在编职工人数",
            column="在编职工数量",
            kind=FieldKind.MEASURE,
        ),
        FieldSpec(
            id="field:library.name",
            model_id="model:library",
            name="图书馆名称",
            column="名称",
            kind=FieldKind.DIMENSION,
        ),
        FieldSpec(
            id="field:orders.net",
            model_id="model:orders",
            name="净收入",
            column="net_amount",
            kind=FieldKind.MEASURE,
        ),
    )
    metrics = (
        _metric("metric:staff.headcount", "在编职工人数", "model:staff"),
        _metric("metric:orders.net", "净收入", "model:orders"),
    )
    metrics = tuple(
        item.model_copy(
            update={
                "field_id": (
                    "field:staff.headcount" if item.id == "metric:staff.headcount"
                    else "field:orders.net"
                )
            }
        )
        for item in metrics
    )
    dimensions = (
        DimensionSpec(
            id="dim:library.name",
            name="图书馆名称",
            model_id="model:library",
            field_id="field:library.name",
        ),
    )
    drafts = (
        _draft("metric:staff.headcount", "在编职工人数", ("在编人数",)),
        _draft("metric:orders.net", "净收入", ()),
        SemanticAliasDraft(
            resource_type="dimension",
            resource_id="dim:library.name",
            resource_name="图书馆名称",
            aliases=("馆名",),
        ),
    )

    kept = preserve_source_column_aliases(
        drafts, metrics=metrics, dimensions=dimensions, fields=fields
    )

    by_id = {item.resource_id: item for item in kept}
    # 改名的度量：物理列名（含中文的用户会说的词）保留为别名。
    assert by_id["metric:staff.headcount"].aliases == ("在编人数", "在编职工数量")
    # 英文技术列名不是用户说法，不塞进别名。
    assert by_id["metric:orders.net"].aliases == ()
    # 通用词物理名（名称/名字/name…）不成为别名——那是所有实体共享的噪音。
    assert by_id["dim:library.name"].aliases == ("馆名",)


def _entity_model(model_id, model_name, *, dimension_name, extra_columns=(), with_primary=True):
    from knowflow_analytics.modeling.catalog_contracts import (
        IdentifierContract,
        IdentifierType,
        ModelContract,
        ModelDefineType,
        ModelDetailContract,
        ModelDimensionContract,
        ModelDimensionType,
        ModelFieldContract,
    )

    columns = ("词条id", "名称", *extra_columns)
    return ModelContract(
        id=model_id,
        name=model_name,
        biz_name=model_id,
        model_detail=ModelDetailContract(
            query_type=ModelDefineType.TABLE_QUERY,
            table_query=f"public.{model_id}",
            fields=tuple(
                ModelFieldContract(field_name=column, data_type="text") for column in columns
            ),
            identifiers=(
                (
                    IdentifierContract(
                        name=f"{model_name}ID",
                        type=IdentifierType.PRIMARY,
                        biz_name="词条id",
                    ),
                )
                if with_primary
                else ()
            ),
            dimensions=(
                ModelDimensionContract(
                    name=dimension_name,
                    type=ModelDimensionType.CATEGORICAL,
                    expr="名称",
                    biz_name="名称",
                    data_type="text",
                ),
                *(
                    ModelDimensionContract(
                        name=column,
                        type=ModelDimensionType.CATEGORICAL,
                        expr=column,
                        biz_name=column,
                        data_type="text",
                    )
                    for column in extra_columns
                ),
            ),
        ),
    )


def _entity_catalog(models, dimensions):
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

    return SemanticCatalog(
        project_id="prj_entity",
        revision_id="rev_entity",
        models=tuple(models),
        dimensions=tuple(dimensions),
    )


def _governed_dimension(dimension_id, name, model_id, expr="名称"):
    from knowflow_analytics.modeling.catalog_contracts import DimensionContract

    return DimensionContract(
        id=dimension_id,
        name=name,
        biz_name=dimension_id,
        model_id=model_id,
        type="categorical",
        semantic_type="CATEGORY",
        expr=expr,
    )


def test_entity_name_dimension_is_compiler_derived_from_the_primary_entity():
    from knowflow_analytics.modeling.ai_artifacts import ensure_entity_name_dimensions

    catalog = _entity_catalog(
        (
            _entity_model("city", "城市", dimension_name="城市名称"),
            # 图书馆自己的 名称 列被 AI 借名成了「城市名称」。
            _entity_model("library", "图书馆", dimension_name="城市名称"),
        ),
        (
            _governed_dimension("dim:city:name", "城市名称", "city"),
            _governed_dimension("dim:library:name", "城市名称", "library"),
        ),
    )

    updated, resolutions = ensure_entity_name_dimensions(catalog)

    by_model = {item.id: item for item in updated.models}
    assert by_model["library"].model_detail.dimensions[0].name == "图书馆名称"
    assert by_model["city"].model_detail.dimensions[0].name == "城市名称"
    governed = {item.id: item.name for item in updated.dimensions}
    assert governed["dim:library:name"] == "图书馆名称"
    assert governed["dim:city:name"] == "城市名称"
    status_by_model = {item.model_id: item.status for item in resolutions}
    assert status_by_model["library"] == "applied"
    assert status_by_model["city"] == "already_named"

    # 幂等：再跑一遍不再改名。
    twice, second = ensure_entity_name_dimensions(updated)
    assert twice == updated
    assert all(item.status == "already_named" for item in second)


def test_entity_name_derivation_never_guesses_without_a_clear_candidate():
    from knowflow_analytics.modeling.ai_artifacts import ensure_entity_name_dimensions

    catalog = _entity_catalog(
        (
            # 无主标识：不属于确认实体，不派生。
            _entity_model("fact", "员工数量", dimension_name="名称", with_primary=False),
            # 两个候选名列（名称 + name）：不猜，报 multiple_candidates。
            _entity_model("shop", "门店", dimension_name="门店叫法", extra_columns=("name",)),
        ),
        (),
    )

    updated, resolutions = ensure_entity_name_dimensions(catalog)

    assert updated == catalog
    status_by_model = {item.model_id: item.status for item in resolutions}
    assert "fact" not in status_by_model
    assert status_by_model["shop"] == "multiple_candidates"


def test_entity_name_dimension_gains_the_bare_entity_noun_as_alias():
    """用户说「各图书馆的X」时，实体本名必须能召回实体名称维度。

    AI 生成的别名总是「馆名/图书馆名」这类名字变体，从不含裸「图书馆」——它
    听起来像实体而不像名字列。但按实体分组正是最高频句型，这个召回词是
    schema 可推导事实，与维度名本身同属编译器职责。
    """
    from knowflow_analytics.modeling.ai_artifacts import ensure_entity_name_dimensions

    catalog = _entity_catalog(
        (
            _entity_model("city", "城市", dimension_name="城市名称"),
            _entity_model("library", "图书馆", dimension_name="城市名称"),
        ),
        (
            _governed_dimension("dim:city:name", "城市名称", "city"),
            _governed_dimension("dim:library:name", "城市名称", "library"),
        ),
    )

    updated, _resolutions = ensure_entity_name_dimensions(catalog)

    aliases = {
        item.id: [part for part in (item.alias or "").split(",") if part]
        for item in updated.dimensions
    }
    assert "图书馆" in aliases["dim:library:name"]
    # 已经叫对名字的实体同样补别名（它此前也没有裸实体名）。
    assert "城市" in aliases["dim:city:name"]

    # 幂等：重复执行不产生重复别名。
    twice, _second = ensure_entity_name_dimensions(updated)
    twice_aliases = {
        item.id: [part for part in (item.alias or "").split(",") if part]
        for item in twice.dimensions
    }
    assert twice_aliases["dim:library:name"].count("图书馆") == 1
    assert twice == updated
