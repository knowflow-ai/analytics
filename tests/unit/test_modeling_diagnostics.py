from __future__ import annotations

from knowflow_analytics.contracts import Cardinality
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.diagnostics import ModelingDiagnosticsAnalyzer


def _revision(release):
    return ModelingRevision(
        id="revision_diagnostics",
        project_id=release.project_id,
        schema_snapshot_hash="sha256:diagnostics",
        etag=3,
        semantic_spec=release,
    )


def test_diagnostics_explain_fanout_without_blocking_a_valid_candidate(sales_release):
    report = ModelingDiagnosticsAnalyzer().analyze(_revision(sales_release))

    assert report.ready is True
    assert report.blocking_count == 0
    assert any(
        item.diagnostic_code == "RELATION_FANOUT_REVIEW_REQUIRED"
        and item.resource_kind == "relation"
        and not item.blocking
        for item in report.diagnostics
    )


def test_diagnostics_block_many_to_many_before_query_execution(sales_release):
    relation = sales_release.relations[0].model_copy(
        update={"cardinality": Cardinality.MANY_TO_MANY}
    )
    release = sales_release.model_copy(
        update={"relations": (relation, *sales_release.relations[1:])}
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    assert report.ready is False
    assert any(
        item.diagnostic_code == "RELATION_MANY_TO_MANY_REQUIRES_BRIDGE" and item.blocking
        for item in report.diagnostics
    )


def test_diagnostics_report_dataset_name_conflicts_with_resource_ids(sales_release):
    metrics = tuple(
        metric.model_copy(update={"aliases": ("区域",)}) if metric.id == "net_revenue" else metric
        for metric in sales_release.metrics
    )
    release = sales_release.model_copy(update={"metrics": metrics})

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    conflict = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "DATASET_SEMANTIC_NAME_AMBIGUOUS"
    )
    assert "sales_dataset" in conflict.affected_resource_ids
    assert "net_revenue" in conflict.affected_resource_ids
    assert "region" in conflict.affected_resource_ids


def test_diagnostics_explain_missing_dataset_as_next_modeling_step(sales_release):
    terms = tuple(term.model_copy(update={"dataset_ids": ()}) for term in sales_release.terms)
    release = sales_release.model_copy(update={"datasets": (), "terms": terms})

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    diagnostic = next(
        item for item in report.diagnostics if item.diagnostic_code == "DATASET_REQUIRED"
    )
    assert report.ready is False
    assert diagnostic.blocking is True
    assert diagnostic.resource_kind == "dataset"
    assert diagnostic.decision_kind == "configure_dataset_scope"
    assert diagnostic.title == "尚未创建问数数据集"
    assert "先确认维度和指标" in diagnostic.recommended_action
    assert "semantic revision" not in diagnostic.message


def test_diagnostics_report_cross_scope_metric_name_collision(sales_release):
    from knowflow_analytics.contracts import (
        Aggregation,
        DatasetSpec,
        DimensionSpec,
        FieldKind,
        FieldSpec,
        MetricSpec,
        ModelSpec,
    )

    base_report = ModelingDiagnosticsAnalyzer().analyze(_revision(sales_release))
    assert not any(
        item.diagnostic_code == "CROSS_SCOPE_METRIC_NAME_SHARED" for item in base_report.diagnostics
    )

    release = sales_release.model_copy(
        update={
            "models": (
                *sales_release.models,
                ModelSpec(id="stores", name="门店", schema_name="analytics_v0", table="stores"),
            ),
            "fields": (
                *sales_release.fields,
                FieldSpec(
                    id="stores.net_amount",
                    model_id="stores",
                    name="净收入金额",
                    column="net_amount",
                    data_type="numeric",
                    kind=FieldKind.MEASURE,
                ),
                FieldSpec(
                    id="stores.city",
                    model_id="stores",
                    name="城市",
                    column="city",
                    kind=FieldKind.DIMENSION,
                ),
            ),
            "metrics": (
                *sales_release.metrics,
                MetricSpec(
                    id="store_net_revenue",
                    name="净收入",
                    model_id="stores",
                    field_id="stores.net_amount",
                    aggregation=Aggregation.SUM,
                ),
            ),
            "dimensions": (
                *sales_release.dimensions,
                DimensionSpec(
                    id="store_city", name="城市", model_id="stores", field_id="stores.city"
                ),
            ),
            "datasets": (
                *sales_release.datasets,
                DatasetSpec(
                    id="store_dataset",
                    name="门店经营",
                    model_ids=("stores",),
                    metric_ids=("store_net_revenue",),
                    dimension_ids=("store_city",),
                    default_limit=100,
                    max_limit=1_000,
                ),
            ),
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    conflict = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "CROSS_SCOPE_METRIC_NAME_SHARED"
    )
    assert conflict.blocking is False
    assert conflict.decision_kind == "resolve_semantic_name_conflict"
    assert "net_revenue" in conflict.affected_resource_ids
    assert "store_net_revenue" in conflict.affected_resource_ids
    assert "净收入" in conflict.message


def test_cross_scope_collision_diagnostic_is_bounded_for_a_large_catalog(
    sales_release,
) -> None:
    from knowflow_analytics.contracts import (
        Aggregation,
        DatasetSpec,
        FieldKind,
        FieldSpec,
        MetricSpec,
        ModelSpec,
    )

    pair_count = 501
    names = tuple(f"{index:04d}{'长名称' * 80}" for index in range(pair_count))
    order_metrics = tuple(
        MetricSpec(
            id=f"orders_shared_{index}",
            name=name,
            model_id="orders",
            field_id="orders.net_amount",
            aggregation=Aggregation.SUM,
        )
        for index, name in enumerate(names)
    )
    store_metrics = tuple(
        MetricSpec(
            id=f"stores_shared_{index}",
            name=name,
            model_id="stores",
            field_id="stores.net_amount",
            aggregation=Aggregation.SUM,
        )
        for index, name in enumerate(names)
    )
    sales_dataset = sales_release.datasets[0].model_copy(
        update={
            "metric_ids": (
                *sales_release.datasets[0].metric_ids,
                *(item.id for item in order_metrics),
            )
        }
    )
    release = sales_release.model_copy(
        update={
            "models": (
                *sales_release.models,
                ModelSpec(id="stores", name="门店", schema_name="analytics_v0", table="stores"),
            ),
            "fields": (
                *sales_release.fields,
                FieldSpec(
                    id="stores.net_amount",
                    model_id="stores",
                    name="净收入金额",
                    column="net_amount",
                    data_type="numeric",
                    kind=FieldKind.MEASURE,
                ),
            ),
            "metrics": (*sales_release.metrics, *order_metrics, *store_metrics),
            "datasets": (
                sales_dataset,
                DatasetSpec(
                    id="store_dataset",
                    name="门店经营",
                    model_ids=("stores",),
                    metric_ids=tuple(item.id for item in store_metrics),
                    dimension_ids=(),
                ),
            ),
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))
    conflict = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "CROSS_SCOPE_METRIC_NAME_SHARED"
    )

    assert len(conflict.message) <= 4_000
    assert len(conflict.affected_resource_ids) <= 1_000
    assert f"共 {pair_count} 个共享说法" in conflict.message


def test_diagnostics_report_cross_scope_dimension_name_collision(sales_release):
    from knowflow_analytics.contracts import (
        DatasetSpec,
        DimensionSpec,
        FieldKind,
        FieldSpec,
        ModelSpec,
    )

    base_report = ModelingDiagnosticsAnalyzer().analyze(_revision(sales_release))
    assert not any(
        item.diagnostic_code == "CROSS_SCOPE_DIMENSION_NAME_SHARED"
        for item in base_report.diagnostics
    )

    # 图书馆表的 名称 列被 AI 借名为「城市名称」——与 orders 的 region 维度改名
    # 同名后，跨模型撞名必须在发布前可见。
    dimensions = tuple(
        item.model_copy(update={"name": "城市名称"}) if item.id == "region" else item
        for item in sales_release.dimensions
    )
    release = sales_release.model_copy(
        update={
            "models": (
                *sales_release.models,
                ModelSpec(id="library", name="图书馆", schema_name="analytics_v0", table="library"),
            ),
            "fields": (
                *sales_release.fields,
                FieldSpec(
                    id="library.name",
                    model_id="library",
                    name="城市名称",
                    column="名称",
                    kind=FieldKind.DIMENSION,
                ),
            ),
            "dimensions": (
                *dimensions,
                DimensionSpec(
                    id="library_name",
                    name="城市名称",
                    model_id="library",
                    field_id="library.name",
                ),
            ),
            "datasets": (
                *(
                    item.model_copy(update={"dimension_ids": tuple(item.dimension_ids)})
                    for item in sales_release.datasets
                ),
                DatasetSpec(
                    id="library_dataset",
                    name="图书馆分析",
                    model_ids=("library",),
                    metric_ids=(),
                    dimension_ids=("library_name",),
                    default_limit=100,
                    max_limit=1_000,
                ),
            ),
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    conflict = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "CROSS_SCOPE_DIMENSION_NAME_SHARED"
    )
    assert conflict.blocking is False
    assert "region" in conflict.affected_resource_ids
    assert "library_name" in conflict.affected_resource_ids
    assert "城市名称" in conflict.message


def test_diagnostics_flag_a_field_borrowing_another_entity_name(sales_release):
    from knowflow_analytics.contracts import FieldKind, FieldSpec, ModelSpec

    # 图书馆表自己的 名称 列被命名为「城市名称」——非标识列携带其它实体名，
    # 且物理列名里没有该实体（不是合法的反规范化列）。
    release = sales_release.model_copy(
        update={
            "models": (
                *sales_release.models,
                ModelSpec(id="city", name="城市", schema_name="analytics_v0", table="city"),
                ModelSpec(id="library", name="图书馆", schema_name="analytics_v0", table="library"),
            ),
            "fields": (
                *sales_release.fields,
                FieldSpec(
                    id="library.name",
                    model_id="library",
                    name="城市名称",
                    column="名称",
                    kind=FieldKind.DIMENSION,
                ),
                # 合法反规范化：物理列名本身就叫 城市，不告警。
                FieldSpec(
                    id="library.city",
                    model_id="library",
                    name="所在城市",
                    column="城市",
                    kind=FieldKind.DIMENSION,
                ),
            ),
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    borrow = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "FIELD_NAME_BORROWS_ENTITY_NAME"
    )
    assert borrow.blocking is False
    assert "library.name" in borrow.affected_resource_ids
    assert "城市名称" in borrow.message
    assert not any(
        "library.city" in item.affected_resource_ids
        for item in report.diagnostics
        if item.diagnostic_code == "FIELD_NAME_BORROWS_ENTITY_NAME"
    )


def test_diagnostics_surface_unresolved_entity_name_candidates(sales_release):
    from knowflow_analytics.modeling.catalog_contracts import (
        IdentifierContract,
        IdentifierType,
        ModelContract,
        ModelDefineType,
        ModelDetailContract,
        ModelDimensionContract,
        ModelDimensionType,
        ModelFieldContract,
        SemanticCatalog,
    )

    catalog = SemanticCatalog(
        project_id=sales_release.project_id,
        revision_id="revision_diagnostics",
        models=(
            ModelContract(
                id="shop",
                name="门店",
                biz_name="shop",
                model_detail=ModelDetailContract(
                    query_type=ModelDefineType.TABLE_QUERY,
                    table_query="public.shop",
                    fields=(
                        ModelFieldContract(field_name="词条id", data_type="text"),
                        ModelFieldContract(field_name="名称", data_type="text"),
                        ModelFieldContract(field_name="name", data_type="text"),
                    ),
                    identifiers=(
                        IdentifierContract(
                            name="门店ID", type=IdentifierType.PRIMARY, biz_name="词条id"
                        ),
                    ),
                    dimensions=(
                        ModelDimensionContract(
                            name="门店叫法",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="名称",
                            biz_name="名称",
                            data_type="text",
                        ),
                        ModelDimensionContract(
                            name="英文名",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="name",
                            biz_name="name",
                            data_type="text",
                        ),
                    ),
                ),
            ),
        ),
    )
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog

    revision = ModelingRevision(
        id="revision_diagnostics",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:diagnostics",
        etag=3,
        semantic_spec=compile_semantic_catalog(catalog),
        semantic_catalog=catalog,
    )

    report = ModelingDiagnosticsAnalyzer().analyze(revision)

    unresolved = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "ENTITY_NAME_DIMENSION_UNRESOLVED"
    )
    assert unresolved.blocking is False
    assert "shop" in unresolved.affected_resource_ids
    assert "门店" in unresolved.message


def test_diagnostics_report_a_model_outside_every_query_scope(sales_release):
    """整张表进不了任何 Scope 时，它的字段全部不可问——必须在发布前说出来。

    音乐六表实测（2026-08-28）：台湾金曲奖 既无主标识也无业务度量，按"根 = 业务
    度量拥有者 ∪ 主标识实体"两头不沾，不生成 Scope，届数/具体奖项 因此不属于任何
    作用域。问「各届数的获奖数量」时零证据，系统把全部 5 个作用域列出来让用户选，
    而选哪个都答不了。建模期七条警告里没有一条提到这件事。
    """
    from knowflow_analytics.contracts import FieldKind, FieldSpec, ModelSpec

    release = sales_release.model_copy(
        update={
            "models": (
                *sales_release.models,
                ModelSpec(id="awards", name="金曲奖", schema_name="analytics_v0", table="awards"),
            ),
            "fields": (
                *sales_release.fields,
                FieldSpec(
                    id="awards.session",
                    model_id="awards",
                    name="届数",
                    column="session",
                    kind=FieldKind.DIMENSION,
                ),
            ),
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    orphan = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "MODEL_OUTSIDE_EVERY_QUERY_SCOPE"
    )
    assert orphan.blocking is False
    assert orphan.affected_resource_ids == ("awards",)
    assert "金曲奖" in orphan.message
    # 已经进了 Scope 的模型不告警。
    assert all(
        "orders" not in item.affected_resource_ids
        for item in report.diagnostics
        if item.diagnostic_code == "MODEL_OUTSIDE_EVERY_QUERY_SCOPE"
    )


def test_diagnostics_report_entities_a_scope_cannot_group_by(sales_release):
    """事实根连得到、但路由到不了的实体：该作用域按它分组的问题必然失败。

    音乐六表实测：翻唱歌曲 经三条路径可达 歌手（直连、经歌曲原唱、经歌曲专辑），
    "路径唯一"不变量把 歌手 整体排除出该 Scope，「各歌手的翻唱评分」因此无解，
    而建模期毫无提示。
    """
    from knowflow_analytics.contracts import AnalysisTopicRouteSpec

    # sales_dataset 覆盖 orders/customers/order_items，但只冻结到 customers 的路由。
    release = sales_release.model_copy(
        update={
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(dataset_id="sales_dataset", root_model_id="orders"),
            )
        }
    )

    report = ModelingDiagnosticsAnalyzer().analyze(_revision(release))

    unreachable = next(
        item
        for item in report.diagnostics
        if item.diagnostic_code == "SCOPE_ENTITY_NOT_REACHABLE"
    )
    assert unreachable.blocking is False
    assert "sales_dataset" in unreachable.affected_resource_ids
    assert "客户" in unreachable.message or "订单明细" in unreachable.message
