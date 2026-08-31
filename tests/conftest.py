from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    Cardinality,
    DatasetSpec,
    DimensionSpec,
    DimensionValueSpec,
    FieldKind,
    FieldSpec,
    MetricKind,
    MetricSpec,
    ModelSpec,
    RelationCondition,
    RelationSpec,
    SemanticRelease,
    TermSpec,
)
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetContract,
    DataSetDetailContract,
    DataSetModelConfigContract,
    DimensionContract,
    FieldParamContract,
    IdentifierContract,
    IdentifierType,
    JoinConditionContract,
    MeasureContract,
    MetricContract,
    MetricDefineByFieldParamsContract,
    MetricDefineByMeasureParamsContract,
    MetricDefineByMetricParamsContract,
    MetricDefineType,
    MetricParamContract,
    ModelContract,
    ModelDefineType,
    ModelDetailContract,
    ModelDimensionContract,
    ModelDimensionType,
    ModelFieldContract,
    ModelRelationContract,
    SemanticCatalog,
)
from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


class DeterministicEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = tuple(self._vector(text) for text in texts)
        return EmbeddingBatch(model_id="test-embedding-v1", dimension=8, vectors=vectors)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        values = [0.0] * 8
        for index, character in enumerate(text):
            values[(ord(character) + index) % len(values)] += 1.0
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return tuple(value / norm for value in values)


@pytest.fixture
def schema_snapshot() -> SchemaSnapshot:
    return SchemaSnapshot.create(
        database_name="analytics",
        captured_at=datetime(2026, 8, 13, tzinfo=UTC),
        tables=(
            TableSnapshot(
                schema_name="sales",
                name="customers",
                columns=(
                    SchemaColumnSnapshot(
                        name="id",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=0,
                        primary_key=True,
                    ),
                    SchemaColumnSnapshot(
                        name="segment",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=1,
                    ),
                ),
            ),
            TableSnapshot(
                schema_name="sales",
                name="orders",
                columns=(
                    SchemaColumnSnapshot(
                        name="id",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=0,
                        primary_key=True,
                    ),
                    SchemaColumnSnapshot(
                        name="customer_id",
                        data_type="BIGINT",
                        nullable=False,
                        ordinal_position=1,
                    ),
                    SchemaColumnSnapshot(
                        name="region",
                        data_type="TEXT",
                        nullable=False,
                        ordinal_position=2,
                    ),
                    SchemaColumnSnapshot(
                        name="net_amount",
                        data_type="NUMERIC(18,2)",
                        nullable=False,
                        ordinal_position=3,
                    ),
                    SchemaColumnSnapshot(
                        name="order_date",
                        data_type="DATE",
                        nullable=False,
                        ordinal_position=4,
                    ),
                ),
                foreign_keys=(
                    ForeignKeySnapshot(
                        name="orders_customer_fk",
                        constrained_columns=("customer_id",),
                        referred_schema="sales",
                        referred_table="customers",
                        referred_columns=("id",),
                    ),
                ),
            ),
        ),
    )


@pytest.fixture
def sales_release() -> SemanticRelease:
    return SemanticRelease(
        id="release_sales_v1",
        project_id="sales",
        spec_hash="fixture-v1",
        models=(
            ModelSpec(id="orders", name="订单", schema_name="analytics_v0", table="orders"),
            ModelSpec(id="customers", name="客户", schema_name="analytics_v0", table="customers"),
            ModelSpec(
                id="order_items", name="订单明细", schema_name="analytics_v0", table="order_items"
            ),
        ),
        fields=(
            FieldSpec(
                id="orders.id",
                model_id="orders",
                name="订单ID",
                column="id",
                kind=FieldKind.IDENTIFIER,
            ),
            FieldSpec(
                id="orders.customer_id",
                model_id="orders",
                name="客户ID",
                column="customer_id",
                kind=FieldKind.IDENTIFIER,
            ),
            FieldSpec(
                id="orders.region",
                model_id="orders",
                name="区域",
                column="region",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="orders.channel",
                model_id="orders",
                name="渠道",
                column="channel",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="orders.net_amount",
                model_id="orders",
                name="净收入金额",
                column="net_amount",
                data_type="numeric",
                kind=FieldKind.MEASURE,
            ),
            FieldSpec(
                id="orders.refund_amount",
                model_id="orders",
                name="退款金额",
                column="refund_amount",
                data_type="numeric",
                kind=FieldKind.MEASURE,
            ),
            FieldSpec(
                id="orders.order_date",
                model_id="orders",
                name="下单日期",
                column="order_date",
                data_type="date",
                kind=FieldKind.TIME,
            ),
            FieldSpec(
                id="customers.id",
                model_id="customers",
                name="客户ID",
                column="id",
                kind=FieldKind.IDENTIFIER,
            ),
            FieldSpec(
                id="customers.segment",
                model_id="customers",
                name="客户分层",
                column="segment",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="order_items.id",
                model_id="order_items",
                name="明细ID",
                column="id",
                kind=FieldKind.IDENTIFIER,
            ),
            FieldSpec(
                id="order_items.order_id",
                model_id="order_items",
                name="订单ID",
                column="order_id",
                kind=FieldKind.IDENTIFIER,
            ),
            FieldSpec(
                id="order_items.product",
                model_id="order_items",
                name="商品",
                column="product",
                kind=FieldKind.DIMENSION,
            ),
        ),
        relations=(
            RelationSpec(
                id="orders_customer",
                left_model_id="orders",
                right_model_id="customers",
                cardinality=Cardinality.MANY_TO_ONE,
                conditions=(
                    RelationCondition(
                        left_field_id="orders.customer_id", right_field_id="customers.id"
                    ),
                ),
            ),
            RelationSpec(
                id="orders_items",
                left_model_id="orders",
                right_model_id="order_items",
                cardinality=Cardinality.ONE_TO_MANY,
                conditions=(
                    RelationCondition(
                        left_field_id="orders.id", right_field_id="order_items.order_id"
                    ),
                ),
            ),
        ),
        dimensions=(
            DimensionSpec(id="region", name="区域", model_id="orders", field_id="orders.region"),
            DimensionSpec(id="channel", name="渠道", model_id="orders", field_id="orders.channel"),
            DimensionSpec(
                id="order_date",
                name="下单日期",
                model_id="orders",
                field_id="orders.order_date",
                semantic_type="time",
            ),
            DimensionSpec(
                id="customer_segment",
                name="客户分层",
                model_id="customers",
                field_id="customers.segment",
            ),
            DimensionSpec(
                id="product", name="商品", model_id="order_items", field_id="order_items.product"
            ),
        ),
        metrics=(
            MetricSpec(
                id="net_revenue",
                name="净收入",
                model_id="orders",
                field_id="orders.net_amount",
                aggregation=Aggregation.SUM,
            ),
            MetricSpec(
                id="refund_amount",
                name="退款金额",
                model_id="orders",
                field_id="orders.refund_amount",
                aggregation=Aggregation.SUM,
            ),
            MetricSpec(
                id="gross_after_refund",
                name="扣减退款后收入",
                model_id="orders",
                kind=MetricKind.DERIVED,
                formula="{net_revenue} - {refund_amount}",
            ),
            MetricSpec(
                id="order_count",
                name="订单数",
                model_id="orders",
                field_id="orders.id",
                aggregation=Aggregation.COUNT_DISTINCT,
            ),
        ),
        datasets=(
            DatasetSpec(
                id="sales_dataset",
                name="销售经营",
                model_ids=("orders", "customers", "order_items"),
                metric_ids=(
                    "net_revenue",
                    "refund_amount",
                    "gross_after_refund",
                    "order_count",
                ),
                dimension_ids=(
                    "region",
                    "channel",
                    "order_date",
                    "customer_segment",
                    "product",
                ),
                default_limit=100,
                max_limit=1_000,
            ),
        ),
        terms=(
            TermSpec(
                id="sales_amount_term",
                name="销售额",
                description="净收入",
                aliases=("销售金额",),
                dataset_ids=("sales_dataset",),
                metric_ids=("net_revenue",),
            ),
        ),
        dimension_values=(
            DimensionValueSpec(
                id="region_east",
                dimension_id="region",
                value="华东",
                display_name="华东",
                aliases=("东区",),
            ),
            DimensionValueSpec(
                id="region_south",
                dimension_id="region",
                value="华南",
                display_name="华南",
                aliases=("南区",),
            ),
        ),
    )


@pytest.fixture
def sales_index(sales_release):
    return SemanticIndexBuilder(DeterministicEmbeddingGateway()).build(sales_release)


@pytest.fixture
def sales_catalog(sales_release: SemanticRelease) -> SemanticCatalog:
    """Catalog-authoritative equivalent of the query-pipeline sales fixture."""

    fields_by_model: dict[str, list[FieldSpec]] = {}
    fields_by_id = {item.id: item for item in sales_release.fields}
    for field in sales_release.fields:
        fields_by_model.setdefault(field.model_id, []).append(field)

    metrics_by_model: dict[str, list[str]] = {}
    dimensions_by_model: dict[str, list[str]] = {}
    for metric in sales_release.metrics:
        metrics_by_model.setdefault(metric.model_id, []).append(metric.id)
    for dimension in sales_release.dimensions:
        dimensions_by_model.setdefault(dimension.model_id, []).append(dimension.id)

    models: list[ModelContract] = []
    for model in sales_release.models:
        model_fields = fields_by_model[model.id]
        measures: list[MeasureContract] = []
        for metric in sales_release.metrics:
            if metric.model_id != model.id or metric.kind is not MetricKind.ATOMIC:
                continue
            field = fields_by_id[metric.field_id]
            if field.kind is not FieldKind.MEASURE:
                continue
            measures.append(
                MeasureContract(
                    name=metric.name,
                    agg=metric.aggregation.value.upper(),
                    expr=field.column,
                    biz_name=field.column,
                )
            )
        models.append(
            ModelContract(
                id=model.id,
                name=model.name,
                biz_name=model.id,
                model_detail=ModelDetailContract(
                    query_type=ModelDefineType.TABLE_QUERY,
                    table_query=f"{model.schema_name}.{model.table}",
                    fields=tuple(
                        ModelFieldContract(field_name=item.column, data_type=item.data_type)
                        for item in model_fields
                    ),
                    identifiers=tuple(
                        IdentifierContract(
                            name=item.name,
                            type=(
                                IdentifierType.PRIMARY
                                if item.column == "id"
                                else IdentifierType.FOREIGN
                            ),
                            biz_name=item.column,
                        )
                        for item in model_fields
                        if item.kind is FieldKind.IDENTIFIER
                    ),
                    dimensions=tuple(
                        ModelDimensionContract(
                            name=item.name,
                            type=(
                                ModelDimensionType.TIME
                                if item.kind is FieldKind.TIME
                                else ModelDimensionType.CATEGORICAL
                            ),
                            expr=item.column,
                            biz_name=item.column,
                            data_type=item.data_type,
                        )
                        for item in model_fields
                        if item.kind in {FieldKind.DIMENSION, FieldKind.TIME}
                    ),
                    measures=tuple(measures),
                ),
            )
        )

    dimensions = tuple(
        DimensionContract(
            id=item.id,
            name=item.name,
            biz_name=item.id,
            model_id=item.model_id,
            type=("time" if item.semantic_type == "time" else "categorical"),
            semantic_type=("DATE" if item.semantic_type == "time" else "CATEGORY"),
            expr=fields_by_id[item.field_id].column,
            alias=",".join(item.aliases) or None,
            description=item.description,
        )
        for item in sales_release.dimensions
    )

    metrics: list[MetricContract] = []
    for item in sales_release.metrics:
        if item.kind is MetricKind.DERIVED:
            dependency_ids = tuple(
                dependency_id
                for dependency_id in ("net_revenue", "refund_amount")
                if f"{{{dependency_id}}}" in (item.formula or "")
            )
            metrics.append(
                MetricContract(
                    id=item.id,
                    name=item.name,
                    biz_name=item.id,
                    model_id=item.model_id,
                    metric_define_type=MetricDefineType.METRIC,
                    metric_define_by_metric_params=MetricDefineByMetricParamsContract(
                        expr=(item.formula or "").replace("{", "").replace("}", ""),
                        metrics=tuple(
                            MetricParamContract(id=dependency_id, biz_name=dependency_id)
                            for dependency_id in dependency_ids
                        ),
                    ),
                )
            )
            continue
        field = fields_by_id[item.field_id]
        if field.kind is FieldKind.MEASURE:
            measure = next(
                measure
                for model in models
                if model.id == item.model_id
                for measure in model.model_detail.measures
                if measure.biz_name == field.column
            )
            metrics.append(
                MetricContract(
                    id=item.id,
                    name=item.name,
                    biz_name=item.id,
                    model_id=item.model_id,
                    metric_define_type=MetricDefineType.MEASURE,
                    metric_define_by_measure_params=MetricDefineByMeasureParamsContract(
                        expr=measure.biz_name,
                        measures=(measure,),
                    ),
                )
            )
        else:
            metrics.append(
                MetricContract(
                    id=item.id,
                    name=item.name,
                    biz_name=item.id,
                    model_id=item.model_id,
                    metric_define_type=MetricDefineType.FIELD,
                    metric_define_by_field_params=MetricDefineByFieldParamsContract(
                        expr=(
                            f"COUNT(DISTINCT {field.column})"
                            if item.aggregation is Aggregation.COUNT_DISTINCT
                            else f"{item.aggregation.value.upper()}({field.column})"
                        ),
                        fields=(FieldParamContract(field_name=field.column),),
                    ),
                )
            )

    catalog = SemanticCatalog(
        project_id=sales_release.project_id,
        revision_id="revision-preview",
        models=tuple(models),
        model_relations=tuple(
            ModelRelationContract(
                id=item.id,
                from_model_id=item.left_model_id,
                to_model_id=item.right_model_id,
                join_type=item.join_type.value,
                knowflow_cardinality=item.cardinality,
                join_conditions=tuple(
                    JoinConditionContract(
                        left_field=fields_by_id[condition.left_field_id].column,
                        right_field=fields_by_id[condition.right_field_id].column,
                    )
                    for condition in item.conditions
                ),
            )
            for item in sales_release.relations
        ),
        dimensions=dimensions,
        metrics=tuple(metrics),
        data_sets=tuple(
            DataSetContract(
                id=item.id,
                name=item.name,
                biz_name=item.id,
                data_set_detail=DataSetDetailContract(
                    data_set_model_configs=tuple(
                        DataSetModelConfigContract(
                            id=model_id,
                            dimensions=tuple(dimensions_by_model.get(model_id, ())),
                            metrics=tuple(metrics_by_model.get(model_id, ())),
                        )
                        for model_id in item.model_ids
                    )
                ),
            )
            for item in sales_release.datasets
        ),
        terms=sales_release.terms,
        dimension_values=sales_release.dimension_values,
    )
    compiled = compile_semantic_catalog(catalog)
    assert {item.id for item in compiled.metrics} == {item.id for item in sales_release.metrics}
    assert {item.id for item in compiled.dimensions} == {
        item.id for item in sales_release.dimensions
    }
    return catalog
