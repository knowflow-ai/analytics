from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from knowflow_analytics.contracts import (
    DEFAULT_DATE_FORMAT,
    Aggregation,
    DatasetSpec,
    DatasetTimeDefaultConfig,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    HierarchySpec,
    JoinType,
    MetricExpressionSource,
    MetricKind,
    MetricSpec,
    ModelSpec,
    NonAdditiveDimension,
    QueryRuleSpec,
    RelationCondition,
    RelationSpec,
    SemanticRelease,
    TimeGranularity,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import semantic_release_hash
from knowflow_analytics.modeling.catalog_contracts import (
    AggregateTypeDefaultConfigContract,
    DataSetContract,
    DataSetDetailContract,
    DataSetModelConfigContract,
    DatePeriod,
    DetailTypeDefaultConfigContract,
    DimensionContract,
    DimensionTimeTypeParamsContract,
    IdentifierContract,
    MeasureContract,
    MetricContract,
    MetricDefineType,
    ModelContract,
    ModelDefineType,
    ModelDimensionContract,
    ModelDimensionType,
    ModelFieldContract,
    ModelRelationContract,
    QueryConfigContract,
    SemanticCatalog,
    TimeDefaultConfigContract,
    TimeMode,
)
from knowflow_analytics.modeling.filter_expression import (
    combine_filter_sql,
    compile_fixed_filters,
)
from knowflow_analytics.modeling.semantic_expression import (
    simple_field_metric,
    validate_dimension_expression,
    validate_field_metric_expression,
    validate_measure_metric_expression,
    validate_metric_metric_expression,
)


def compile_semantic_catalog(catalog: SemanticCatalog) -> SemanticRelease:
    """Compile the governed modeling catalog into the deterministic query projection."""

    models: list[ModelSpec] = []
    fields: list[FieldSpec] = []
    dimensions: dict[str, DimensionSpec] = {}
    metrics: dict[str, MetricSpec] = {}
    fields_by_model_and_name: dict[tuple[str, str], FieldSpec] = {}
    measures_by_model = {model.id: model.model_detail.measures for model in catalog.models}

    for model in catalog.models:
        projection_model, model_fields, generated_dimensions, generated_metrics = _compile_model(
            model
        )
        models.append(projection_model)
        fields.extend(model_fields)
        for field in model_fields:
            fields_by_model_and_name[(model.id, field.column)] = field
        dimensions.update((item.id, item) for item in generated_dimensions)
        metrics.update((item.id, item) for item in generated_metrics)

    for dimension in catalog.dimensions:
        compiled = _compile_dimension(dimension, fields_by_model_and_name)
        dimensions[compiled.id] = compiled

    for metric in catalog.metrics:
        compiled = _compile_metric(
            metric,
            fields_by_model_and_name,
            model_measures=measures_by_model.get(metric.model_id, ()),
        )
        compiled = _apply_non_additive_dimension(compiled, metric, dimensions)
        compiled = _apply_agg_time_dimension(compiled, metric, dimensions)
        metrics[compiled.id] = compiled

    relations = tuple(
        _compile_relation(item, fields_by_model_and_name) for item in catalog.model_relations
    )
    datasets = tuple(
        _compile_dataset(
            item,
            tuple(dimensions.values()),
            tuple(metrics.values()),
            tuple(fields),
        )
        for item in catalog.data_sets
    )
    datasets = _synthesize_metric_time_axes(datasets, dimensions, metrics)
    release = SemanticRelease(
        id=catalog.revision_id,
        project_id=catalog.project_id,
        spec_hash="pending",
        models=tuple(models),
        fields=tuple(fields),
        relations=relations,
        dimensions=tuple(dimensions.values()),
        hierarchies=_compile_hierarchies(catalog, dimensions),
        metrics=tuple(metrics.values()),
        datasets=datasets,
        terms=catalog.terms,
        dimension_values=catalog.dimension_values,
        semantic_context=catalog.semantic_context,
        analysis_topic_routes=catalog.analysis_topic_routes,
        query_rules=tuple(
            QueryRuleSpec.model_validate(item.model_dump(mode="python"))
            for item in catalog.query_rules
        ),
        modeling_catalog=catalog.canonical_payload(),
        revision_id=catalog.revision_id,
    )
    return release.model_copy(update={"spec_hash": semantic_release_hash(release)})


def validate_m0_publishable(catalog: SemanticCatalog) -> None:
    """Validate the executable subset of the semantic query language."""
    missing_cardinality = [
        item.id for item in catalog.model_relations if item.knowflow_cardinality is None
    ]
    if missing_cardinality:
        raise SemanticValidationError(
            f"model relations require confirmed cardinality: {missing_cardinality[:5]}",
            code="RELATION_CARDINALITY_REQUIRED",
        )
    unsupported_joins = [
        item.id
        for item in catalog.model_relations
        if _join_type(item.join_type) not in {JoinType.LEFT, JoinType.INNER, JoinType.RIGHT}
    ]
    if unsupported_joins:
        raise SemanticValidationError(
            f"model relations use an unsupported M0 join type: {unsupported_joins[:5]}",
            code="RELATION_JOIN_TYPE_UNSUPPORTED",
        )
    unsupported_join_operators = [
        item.id
        for item in catalog.model_relations
        if any(condition.operator != "=" for condition in item.join_conditions)
    ]
    if unsupported_join_operators:
        raise SemanticValidationError(
            "model relation operators other than equality are not executable in M0: "
            f"{unsupported_join_operators[:5]}",
            code="RELATION_OPERATOR_UNSUPPORTED",
        )
    for data_set in catalog.data_sets:
        model_ids = {item.id for item in data_set.data_set_detail.data_set_model_configs}
        metric_ids = {
            metric_id
            for item in data_set.data_set_detail.data_set_model_configs
            for metric_id in item.metrics
        }
        dimension_ids = {
            dimension_id
            for item in data_set.data_set_detail.data_set_model_configs
            for dimension_id in item.dimensions
        }
        relevant = tuple(
            item
            for item in catalog.semantic_context
            if (
                (item.target_type == "project" and item.target_id == catalog.project_id)
                or (item.target_type == "query_scope" and item.target_id == data_set.id)
                or (item.target_type == "model" and item.target_id in model_ids)
                or (item.target_type == "metric" and item.target_id in metric_ids)
                or (item.target_type == "dimension" and item.target_id in dimension_ids)
            )
        )
        if len(relevant) > 100 or sum(len(item.text) for item in relevant) > 40_000:
            raise SemanticValidationError(
                f"semantic context exceeds the prompt budget for query scope {data_set.id}",
                code="SEMANTIC_CONTEXT_SCOPE_LIMIT_EXCEEDED",
            )


def catalog_table_model(
    *,
    model_id: str,
    schema_name: str,
    table_name: str,
    description: str,
    fields: Iterable[ModelFieldContract],
    identifiers: Iterable[IdentifierContract] = (),
) -> ModelContract:
    return ModelContract(
        id=model_id,
        name=table_name,
        biz_name=table_name,
        description=description,
        model_detail={
            "queryType": ModelDefineType.TABLE_QUERY.value,
            "tableQuery": f"{schema_name}.{table_name}",
            "identifiers": tuple(identifiers),
            "fields": tuple(fields),
        },
    )


def replace_catalog_item(
    catalog: SemanticCatalog,
    *,
    collection: str,
    item: Any,
) -> SemanticCatalog:
    item_id = item.id
    values = []
    replaced = False
    for existing in getattr(catalog, collection):
        if existing.id == item_id:
            values.append(item)
            replaced = True
        else:
            values.append(existing)
    if not replaced:
        values.append(item)
    return SemanticCatalog.model_validate(
        catalog.model_copy(update={collection: tuple(values)}).model_dump(mode="python")
    )


def replace_model_detail_item(
    catalog: SemanticCatalog,
    *,
    model_id: str,
    collection: str,
    item: object,
    identity_field: str,
) -> SemanticCatalog:
    models: list[ModelContract] = []
    found = False
    identity = getattr(item, identity_field)
    for model in catalog.models:
        if model.id != model_id:
            models.append(model)
            continue
        found = True
        current = getattr(model.model_detail, collection)
        values = []
        replaced = False
        for value in current:
            if getattr(value, identity_field) == identity:
                values.append(item)
                replaced = True
            else:
                values.append(value)
        if not replaced:
            values.append(item)
        detail = model.model_detail.model_copy(update={collection: tuple(values)})
        models.append(model.model_copy(update={"model_detail": detail}))
    if not found:
        raise SemanticValidationError("model was not found", code="MODEL_NOT_FOUND")
    return SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": tuple(models)}).model_dump(mode="python")
    )


def _compile_model(
    model: ModelContract,
) -> tuple[ModelSpec, tuple[FieldSpec, ...], tuple[DimensionSpec, ...], tuple[MetricSpec, ...]]:
    detail = model.model_detail
    if detail.query_type is ModelDefineType.TABLE_QUERY:
        schema_name, table_name = _table_source(detail.table_query or "")
        projection_model = ModelSpec(
            id=model.id,
            name=model.name,
            biz_name=model.biz_name,
            query_type=ModelDefineType.TABLE_QUERY.value,
            table=table_name,
            schema_name=schema_name,
            db_type=detail.db_type,
            filter_sql=detail.filter_sql or model.filter_sql,
            description=model.description,
            aliases=_aliases(model.alias),
        )
    else:
        projection_model = ModelSpec(
            id=model.id,
            name=model.name,
            biz_name=model.biz_name,
            query_type=ModelDefineType.SQL_QUERY.value,
            table=None,
            schema_name=None,
            db_type=detail.db_type,
            sql_query=detail.sql_query,
            filter_sql=detail.filter_sql or model.filter_sql,
            sql_variables=tuple(
                item.model_dump(mode="json", by_alias=True) for item in detail.sql_variables
            ),
            description=model.description,
            aliases=_aliases(model.alias),
        )
        schema_name, table_name = "sql", model.id

    identifiers = {item.biz_name: item for item in detail.identifiers}
    model_dimensions = {item.expr: item for item in detail.dimensions}
    measures = {item.expr: item for item in detail.measures}
    fields: list[FieldSpec] = []
    dimensions: list[DimensionSpec] = []
    metrics: list[MetricSpec] = []
    for physical in detail.fields:
        field_id = _stable_id("field", schema_name, table_name, physical.field_name)
        identifier = identifiers.get(physical.field_name)
        dimension = model_dimensions.get(physical.field_name)
        measure = measures.get(physical.field_name)
        if identifier is not None:
            kind = FieldKind.IDENTIFIER
            name = identifier.name
            create_dimension = bool(identifier.is_create_dimension)
            identifier_type = identifier.type.value
            dimension_type = None
            semantic_expr = _quote_identifier(physical.field_name)
            unit = None
            default_aggregation = None
        elif dimension is not None:
            kind = (
                FieldKind.TIME
                if dimension.type in {ModelDimensionType.TIME, ModelDimensionType.PARTITION_TIME}
                else FieldKind.DIMENSION
            )
            name = dimension.name
            create_dimension = bool(dimension.is_create_dimension)
            identifier_type = None
            dimension_type = dimension.type.value
            semantic_expr = _quote_identifier(dimension.expr)
            unit = None
            default_aggregation = None
        elif measure is not None:
            kind = FieldKind.MEASURE
            name = measure.name
            create_dimension = False
            identifier_type = None
            dimension_type = None
            semantic_expr = _quote_identifier(measure.expr)
            unit = measure.unit
            default_aggregation = _aggregation(measure.agg)
        else:
            kind = FieldKind.FIELD
            name = physical.field_name
            create_dimension = False
            identifier_type = None
            dimension_type = None
            semantic_expr = None
            unit = None
            default_aggregation = None
        field = FieldSpec(
            id=field_id,
            model_id=model.id,
            name=name,
            column=physical.field_name,
            data_type=physical.data_type,
            kind=kind,
            identifier_type=identifier_type,
            dimension_type=dimension_type,
            semantic_expr=semantic_expr,
            unit=unit,
            default_aggregation=default_aggregation,
            description=dimension.description if dimension is not None else "",
            create_dimension=create_dimension,
            create_metric=bool(measure and measure.is_create_metric),
        )
        fields.append(field)
        # `isCreateDimension` and `isCreateMetric` are creation intents on the
        # model request, not a substitute for the separately governed
        # Dimension/Metric resources. API commands materialize those resources
        # explicitly; loading a catalog must never invent hidden query elements.
    # DataModelNode reads ModelDetail.filterSql. The top-level Model filterSql is
    # part of the same Model DTO; combine the two without duplication.
    model_filter_sql = combine_filter_sql((detail.filter_sql or model.filter_sql,))
    projection_model = projection_model.model_copy(
        update={
            "filter_sql": model_filter_sql,
            "filters": compile_fixed_filters(
                (model_filter_sql,),
                model_id=model.id,
                fields=fields,
                allowed_qualifiers=(
                    model.biz_name,
                    (detail.table_query or "").rsplit(".", maxsplit=1)[-1],
                ),
            ),
        }
    )
    return projection_model, tuple(fields), tuple(dimensions), tuple(metrics)


def _dimension_date_format(dimension: DimensionContract) -> str:
    """时间列在库里的书写格式。

    上游写在 ``ext[time_format]``（``DimensionConstants.TIME_FORMAT``）,我们写的是
    ``ext["dateFormat"]``。两个键都读:键名只是历史差异,而已落库的 revision 不该
    因为改键名丢掉格式。缺失时用上游默认值 ``yyyy-MM-dd``。
    """

    ext = dimension.ext or {}
    for key in ("dateFormat", "time_format"):
        value = ext.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_DATE_FORMAT


def _compile_dimension(
    dimension: DimensionContract,
    fields: dict[tuple[str, str], FieldSpec],
) -> DimensionSpec:
    model_fields = {
        field.column: field
        for (model_id, _), field in fields.items()
        if model_id == dimension.model_id
    }
    referenced = validate_dimension_expression(
        _quote_identifier(dimension.expr),
        available_fields=model_fields,
    )
    expression_fields = tuple(model_fields[item] for item in referenced)
    field = expression_fields[0]
    semantic_type = dimension.semantic_type.casefold()
    if semantic_type == "date" or "time" in dimension.type.casefold():
        semantic_type = "time"
    elif semantic_type == "id":
        semantic_type = "identifier"
    else:
        semantic_type = "categorical"
    return DimensionSpec(
        id=dimension.id,
        name=dimension.name,
        model_id=dimension.model_id,
        field_id=field.id,
        # 维度未单独声明类型时回落物理列类型(对齐上游 DataSetSchemaBuilder)。
        data_type=dimension.data_type or field.data_type,
        date_format=(_dimension_date_format(dimension) if semantic_type == "time" else None),
        aliases=_aliases(dimension.alias),
        description=dimension.description,
        semantic_type=semantic_type,
        expression=(dimension.expr if dimension.expr != field.column else None),
        expression_field_ids=(
            tuple(item.id for item in expression_fields) if dimension.expr != field.column else ()
        ),
        default_values=dimension.default_values,
        # 上游 DimensionTimeTypeParams.timeGranularity 此前在这里被丢弃，导致
        # 运行期只能由模型从问句猜粒度。只有时间维度携带它。
        time_granularity=(
            _compile_time_granularity(dimension.type_params) if semantic_type == "time" else None
        ),
    )


def _compile_hierarchies(
    catalog: SemanticCatalog,
    dimensions: dict[str, DimensionSpec],
) -> tuple[HierarchySpec, ...]:
    """把层级编译进 Release，逐级校验。

    写错时显式失败：层级只有被送进模型的 schema 才有意义，一个指向不存在维度的
    层级会让「按地区」这类问题继续在几个维度里瞎猜，而用户以为已经配好了。
    """

    compiled: list[HierarchySpec] = []
    for item in catalog.hierarchies:
        for level in item.levels:
            dimension = dimensions.get(level)
            if dimension is None:
                raise SemanticValidationError(
                    f"hierarchy {item.id} references an unknown dimension: {level}",
                    code="HIERARCHY_LEVEL_INVALID",
                )
            if dimension.model_id != item.model_id:
                raise SemanticValidationError(
                    f"hierarchy {item.id} level {level} belongs to another model",
                    code="HIERARCHY_LEVEL_INVALID",
                )
        compiled.append(
            HierarchySpec(
                id=item.id,
                model_id=item.model_id,
                name=item.name,
                aliases=_aliases(item.alias),
                description=item.description,
                levels=item.levels,
            )
        )
    return tuple(compiled)


def _apply_agg_time_dimension(
    compiled: MetricSpec,
    metric: MetricContract,
    dimensions: dict[str, DimensionSpec],
) -> MetricSpec:
    """把指标的聚合时间轴解析成维度 id。

    写错时显式失败，理由和半可加声明一样：静默忽略会让用户以为已经设上了，
    而问数仍在用数据集默认时间维度，返回一个看起来正常的错数字。

    限制为「同模型的时间维度」有实义：跨模型分组要先 join，会改变指标的聚合
    粒度；非时间维度则根本无法承担时间轴的角色。
    """

    declared = metric.agg_time_dimension_id
    if declared is None:
        return compiled
    dimension = dimensions.get(declared)
    if dimension is None:
        raise SemanticValidationError(
            f"metric {metric.id} references an unknown aggregation time dimension: {declared}",
            code="AGG_TIME_DIMENSION_INVALID",
        )
    if dimension.model_id != metric.model_id:
        raise SemanticValidationError(
            f"metric {metric.id} aggregation time dimension {declared} belongs to another model",
            code="AGG_TIME_DIMENSION_INVALID",
        )
    if dimension.semantic_type != "time":
        raise SemanticValidationError(
            f"metric {metric.id} aggregation time dimension {declared} is not a time dimension",
            code="AGG_TIME_DIMENSION_INVALID",
        )
    try:
        return MetricSpec.model_validate(
            {**compiled.model_dump(), "agg_time_dimension_id": declared}
        )
    except (ValueError, ValidationError) as exc:
        raise SemanticValidationError(
            f"metric {metric.id} has an invalid aggregation time dimension: {exc}",
            code="AGG_TIME_DIMENSION_INVALID",
        ) from exc


def _apply_non_additive_dimension(
    compiled: MetricSpec,
    metric: MetricContract,
    dimensions: dict[str, DimensionSpec],
) -> MetricSpec:
    """从 MetricContract.ext 读出半可加声明。

    上游没有可加性概念，但 ``ext`` 是它自带的扩展位，用它就不必新造一个非对齐
    字段，也能让声明随 Catalog DTO 无损往返。写错时显式失败：静默忽略会让用户
    以为已经设上了，而问数仍在返回相加后的错误数字。
    """

    raw = metric.ext.get("nonAdditiveDimension") if metric.ext else None
    if raw is None:
        return compiled
    if not isinstance(raw, dict):
        raise SemanticValidationError(
            f"metric {metric.id} nonAdditiveDimension must be an object",
            code="NON_ADDITIVE_DIMENSION_INVALID",
        )
    dimension_id = str(raw.get("dimensionId") or "").strip()
    if not dimension_id:
        raise SemanticValidationError(
            f"metric {metric.id} nonAdditiveDimension requires dimensionId",
            code="NON_ADDITIVE_DIMENSION_INVALID",
        )
    if dimension_id not in dimensions:
        raise SemanticValidationError(
            f"metric {metric.id} nonAdditiveDimension references unknown dimension {dimension_id}",
            code="NON_ADDITIVE_DIMENSION_INVALID",
        )
    if dimensions[dimension_id].model_id != compiled.model_id:
        raise SemanticValidationError(
            f"metric {metric.id} nonAdditiveDimension must belong to the same model",
            code="NON_ADDITIVE_DIMENSION_INVALID",
        )
    window_raw = str(raw.get("windowChoice") or "max").strip().casefold()
    groupings = tuple(str(item) for item in raw.get("windowGroupings") or ())
    try:
        declaration = NonAdditiveDimension(
            dimension_id=dimension_id,
            window_choice=Aggregation(window_raw),
            window_groupings=groupings,
        )
        # 重新构造而不是 model_copy(update=...)，确保 MetricSpec 的校验器真正复核
        # "只有原子 SUM/COUNT 可携带该声明"这条规则。
        return MetricSpec.model_validate(
            {**compiled.model_dump(), "non_additive_dimension": declaration}
        )
    except (ValueError, ValidationError) as exc:
        raise SemanticValidationError(
            f"metric {metric.id} has an invalid nonAdditiveDimension: {exc}",
            code="NON_ADDITIVE_DIMENSION_INVALID",
        ) from exc


def _compile_time_granularity(
    type_params: DimensionTimeTypeParamsContract | None,
) -> TimeGranularity | None:
    """把上游 timeGranularity 映射到受治理粒度。

    上游是自由字符串，可能出现我们未治理的取值（如 ``hour``）。此时返回 None
    表示"未声明粒度"，行为与补齐前一致；不能因为一个陌生取值让整个 Revision
    编译失败。
    """

    if type_params is None:
        return None
    raw = (type_params.time_granularity or "").strip().casefold()
    try:
        return TimeGranularity(raw)
    except ValueError:
        return None


def _compile_metric(
    metric: MetricContract,
    fields: dict[tuple[str, str], FieldSpec],
    *,
    model_measures: tuple[MeasureContract, ...] = (),
) -> MetricSpec:
    params = _metric_params(metric)
    if metric.metric_define_type is MetricDefineType.MEASURE:
        measure_params = metric.metric_define_by_measure_params
        assert measure_params is not None
        selected_measure_names = tuple(item.biz_name for item in measure_params.measures)
        measure_names = validate_measure_metric_expression(
            measure_params.expr,
            selected_measures=selected_measure_names,
        )
        selected_measures_by_name = {
            item.biz_name.casefold(): item for item in measure_params.measures
        }
        governed_measures_by_name = {item.biz_name.casefold(): item for item in model_measures}
        model_fields = {
            field.column: field
            for (model_id, _), field in fields.items()
            if model_id == metric.model_id
        }
        sources: list[MetricExpressionSource] = []
        for measure_name in measure_names:
            selected_measure = selected_measures_by_name[measure_name.casefold()]
            measure = governed_measures_by_name.get(measure_name.casefold())
            if measure is None:
                raise SemanticValidationError(
                    f"MEASURE metric {metric.id} references an unknown model measure: "
                    f"{measure_name}",
                    code="MEASURE_METRIC_EXPRESSION_UNSUPPORTED",
                )
            referenced_fields = validate_dimension_expression(
                _quote_identifier(measure.expr),
                available_fields=model_fields,
            )
            source_fields = tuple(model_fields[item] for item in referenced_fields)
            sources.append(
                MetricExpressionSource(
                    name=measure.biz_name,
                    field_id=source_fields[0].id,
                    expression=(measure.expr if measure.expr != source_fields[0].column else None),
                    expression_field_ids=(
                        tuple(item.id for item in source_fields)
                        if measure.expr != source_fields[0].column
                        else ()
                    ),
                    aggregation=_aggregation(measure.agg),
                    raw_filter_sql=selected_measure.constraint,
                    filters=compile_fixed_filters(
                        (selected_measure.constraint,),
                        model_id=metric.model_id,
                        fields=fields.values(),
                    ),
                    alias=measure.alias,
                    unit=measure.unit,
                    is_create_metric=measure.is_create_metric,
                )
            )
        if (
            len(sources) == 1
            and measure_params.expr.strip().casefold() == sources[0].name.casefold()
            and sources[0].expression is None
        ):
            source = sources[0]
            filter_sql = combine_filter_sql((params.filter_sql, source.raw_filter_sql))
            return MetricSpec(
                id=metric.id,
                name=metric.name,
                # 建模期选定的展示格式(对齐上游 PromptHelper 的 FORMAT 'PERCENT'):
                # 不编译进来,prompt 的 format 读取点恒为 None,百分比指标易差 100 倍。
                format=metric.data_format_type,
                model_id=metric.model_id,
                kind=MetricKind.ATOMIC,
                field_id=source.field_id,
                aggregation=source.aggregation,
                define_type=metric.metric_define_type.value,
                raw_filter_sql=filter_sql,
                filters=compile_fixed_filters(
                    (params.filter_sql, source.raw_filter_sql),
                    model_id=metric.model_id,
                    fields=fields.values(),
                ),
                aliases=_aliases(metric.alias),
                description=metric.description,
                unit=source.unit,
            )
        return MetricSpec(
            id=metric.id,
            name=metric.name,
            # 建模期选定的展示格式(对齐上游 PromptHelper 的 FORMAT 'PERCENT'):
            # 不编译进来,prompt 的 format 读取点恒为 None,百分比指标易差 100 倍。
            format=metric.data_format_type,
            model_id=metric.model_id,
            kind=MetricKind.DERIVED,
            formula=measure_params.expr,
            define_type=metric.metric_define_type.value,
            raw_filter_sql=params.filter_sql,
            filters=compile_fixed_filters(
                (params.filter_sql,),
                model_id=metric.model_id,
                fields=fields.values(),
            ),
            aliases=_aliases(metric.alias),
            description=metric.description,
            expression_sources=tuple(sources),
        )
    if metric.metric_define_type is MetricDefineType.FIELD:
        selected_field_names = {
            item.field_name
            for item in metric.metric_define_by_field_params.fields  # type: ignore[union-attr]
        }
        referenced = validate_field_metric_expression(
            params.expr,
            available_fields=(column for model_id, column in fields if model_id == metric.model_id),
            selected_fields=selected_field_names,
        )
        simple = simple_field_metric(params.expr)
        if simple is not None:
            field_name, aggregation = simple
            field = _field_for_expr(fields, metric.model_id, field_name)
            return MetricSpec(
                id=metric.id,
                name=metric.name,
                # 建模期选定的展示格式(对齐上游 PromptHelper 的 FORMAT 'PERCENT'):
                # 不编译进来,prompt 的 format 读取点恒为 None,百分比指标易差 100 倍。
                format=metric.data_format_type,
                model_id=metric.model_id,
                kind=MetricKind.ATOMIC,
                field_id=field.id,
                aggregation=Aggregation(aggregation),
                define_type=metric.metric_define_type.value,
                raw_filter_sql=params.filter_sql,
                filters=compile_fixed_filters(
                    (params.filter_sql,),
                    model_id=metric.model_id,
                    fields=fields.values(),
                ),
                aliases=_aliases(metric.alias),
                description=metric.description,
            )
        return MetricSpec(
            id=metric.id,
            name=metric.name,
            # 建模期选定的展示格式(对齐上游 PromptHelper 的 FORMAT 'PERCENT'):
            # 不编译进来,prompt 的 format 读取点恒为 None,百分比指标易差 100 倍。
            format=metric.data_format_type,
            model_id=metric.model_id,
            kind=MetricKind.DERIVED,
            formula=params.expr,
            define_type=metric.metric_define_type.value,
            raw_filter_sql=params.filter_sql,
            filters=compile_fixed_filters(
                (params.filter_sql,),
                model_id=metric.model_id,
                fields=fields.values(),
            ),
            aliases=_aliases(metric.alias),
            description=metric.description,
            expression_sources=tuple(
                MetricExpressionSource(
                    name=field_name,
                    field_id=_field_for_expr(fields, metric.model_id, field_name).id,
                )
                for field_name in referenced
            ),
        )
    metric_params = metric.metric_define_by_metric_params
    if metric_params is None or not metric_params.metrics:
        # 上游 MetricCheckUtils:typeParams 与 metrics 列表都不可为空。
        raise SemanticValidationError(
            f"METRIC metric {metric.id} requires declared dependency metrics",
            code="METRIC_METRIC_EXPRESSION_INVALID",
        )
    # 依赖指标各自已带聚合,这里再包一层会展开成嵌套聚合;未声明的 token 会
    # 原样穿进物理 SQL。此前这是建模期唯一没有表达式校验的路径。
    validate_metric_metric_expression(
        metric_params.expr,
        dependencies=tuple(item.biz_name for item in metric_params.metrics),
    )
    formula = metric_params.expr
    dependencies = sorted(
        metric_params.metrics,
        key=lambda item: len(item.biz_name),
        reverse=True,
    )
    for dependency in dependencies:
        formula = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(dependency.biz_name)}(?![A-Za-z0-9_])",
            f"{{{dependency.id}}}",
            formula,
        )
    unresolved = [item.biz_name for item in dependencies if f"{{{item.id}}}" not in formula]
    if unresolved:
        raise SemanticValidationError(
            f"METRIC metric {metric.id} expression omits dependencies: {unresolved}",
            code="DERIVED_METRIC_EXPRESSION_INVALID",
        )
    return MetricSpec(
        id=metric.id,
        name=metric.name,
        # 建模期选定的展示格式(对齐上游 PromptHelper 的 FORMAT 'PERCENT'):
        # 不编译进来,prompt 的 format 读取点恒为 None,百分比指标易差 100 倍。
        format=metric.data_format_type,
        model_id=metric.model_id,
        kind=MetricKind.DERIVED,
        formula=formula,
        define_type=metric.metric_define_type.value,
        raw_filter_sql=params.filter_sql,
        filters=compile_fixed_filters(
            (params.filter_sql,),
            model_id=metric.model_id,
            fields=fields.values(),
        ),
        aliases=_aliases(metric.alias),
        description=metric.description,
    )


def _compile_relation(
    relation: ModelRelationContract,
    fields: dict[tuple[str, str], FieldSpec],
) -> RelationSpec:
    return RelationSpec(
        id=relation.id,
        left_model_id=relation.from_model_id,
        right_model_id=relation.to_model_id,
        join_type=_join_type(relation.join_type),
        cardinality=relation.knowflow_cardinality or relation_default_cardinality(),
        conditions=tuple(
            RelationCondition(
                left_field_id=_field_for_expr(
                    fields,
                    relation.from_model_id,
                    item.left_field,
                ).id,
                right_field_id=_field_for_expr(
                    fields,
                    relation.to_model_id,
                    item.right_field,
                ).id,
            )
            for item in relation.join_conditions
        ),
    )


def relation_default_cardinality():
    # Missing upstream cardinality is represented conservatively until a human
    # confirms it. Publication rejects the missing confirmation before querying.
    from knowflow_analytics.contracts import Cardinality

    return Cardinality.MANY_TO_MANY



_METRIC_TIME_AXIS_NAME = "统计时间"


def _synthesize_metric_time_axes(
    datasets: tuple[DatasetSpec, ...],
    dimensions: dict[str, DimensionSpec],
    metrics: dict[str, MetricSpec],
) -> tuple[DatasetSpec, ...]:
    """给「指标声明了多根时间轴」的数据集合成一根逻辑时间轴。

    没有它,「本月收入和订单数」这类问句只能共用一根物理时间列:跨月支付很
    常见,实测 2000 笔半年数据首月订单数偏差 -123 笔、末月 +95 笔,而 SQL 合法、
    趋势图上看不出异常。LLM 写 S2SQL 时只会写具体维度名,没有词汇表达「各按
    各的」——这根轴就是那个词。

    合成而不是让建模者编写:业务建模者不会意识到「这两个指标时间轴不同,得手
    工建一个 UNION ALL 对齐模型」。MetricFlow 的 metric_time 与 Cube 的
    multi-fact view 同样把它放在查询期,Cube 文档直接把手工绕开列为要消除的东西。
    建模合同不变,前端无需同步。
    """

    updated: list[DatasetSpec] = []
    for dataset in datasets:
        axes = {
            metric.agg_time_dimension_id
            for metric_id in dataset.metric_ids
            if (metric := metrics.get(metric_id)) is not None
            and metric.agg_time_dimension_id is not None
        }
        if len(axes) < 2:
            updated.append(dataset)
            continue
        anchor = dimensions.get(sorted(axes)[0])
        if anchor is None:
            updated.append(dataset)
            continue
        axis_id = _stable_id("dimension", "metric_time", dataset.id)
        if axis_id in dataset.dimension_ids:
            updated.append(dataset)
            continue
        # field_id 指向锚点轴,只作为「未声明轴的指标」的兜底列;翻译层按
        # metric_time_axis 逐指标改写成各自的轴,不会真的共用这一列。
        dimensions[axis_id] = DimensionSpec(
            id=axis_id,
            name=_METRIC_TIME_AXIS_NAME,
            model_id=anchor.model_id,
            field_id=anchor.field_id,
            semantic_type="time",
            data_type=anchor.data_type,
            date_format=anchor.date_format,
            time_granularity=anchor.time_granularity,
            description="各指标按各自声明的时间轴统计",
            metric_time_axis=True,
        )
        updated.append(
            dataset.model_copy(
                update={"dimension_ids": (*dataset.dimension_ids, axis_id)}
            )
        )
    return tuple(updated)

def _compile_dataset(
    data_set: DataSetContract,
    dimensions: tuple[DimensionSpec, ...],
    metrics: tuple[MetricSpec, ...],
    fields: tuple[FieldSpec, ...],
) -> DatasetSpec:
    dimensions_by_model: dict[str, list[str]] = {}
    metrics_by_model: dict[str, list[str]] = {}
    for dimension in dimensions:
        dimensions_by_model.setdefault(dimension.model_id, []).append(dimension.id)
    for metric in metrics:
        metrics_by_model.setdefault(metric.model_id, []).append(metric.id)
    model_ids: list[str] = []
    dimension_ids: list[str] = []
    metric_ids: list[str] = []
    for config in data_set.data_set_detail.data_set_model_configs:
        model_ids.append(config.id)
        if config.includes_all:
            dimension_ids.extend(dimensions_by_model.get(config.id, ()))
            metric_ids.extend(metrics_by_model.get(config.id, ()))
        else:
            dimension_ids.extend(config.dimensions)
            metric_ids.extend(config.metrics)
    aggregate_limit = data_set.query_config.aggregate_type_default_config.limit
    detail_limit = data_set.query_config.detail_type_default_config.limit
    fields_by_id = {item.id: item for item in fields}
    partition_dimensions = [
        item.id
        for item in dimensions
        if item.id in dimension_ids
        and fields_by_id[item.field_id].dimension_type == "partition_time"
    ]
    default_time_dimension_id = partition_dimensions[0] if len(partition_dimensions) == 1 else None
    detail_time = data_set.query_config.detail_type_default_config.time_default_config
    aggregate_time = data_set.query_config.aggregate_type_default_config.time_default_config
    return DatasetSpec(
        id=data_set.id,
        name=data_set.name,
        biz_name=data_set.biz_name,
        model_ids=tuple(dict.fromkeys(model_ids)),
        metric_ids=tuple(dict.fromkeys(metric_ids)),
        dimension_ids=tuple(dict.fromkeys(dimension_ids)),
        default_limit=aggregate_limit,
        max_limit=max(aggregate_limit, detail_limit),
        aliases=_aliases(data_set.alias),
        description=data_set.description,
        default_time_dimension_id=default_time_dimension_id,
        detail_time_default=(
            DatasetTimeDefaultConfig(
                unit=detail_time.unit,
                period=detail_time.period.value,
                time_mode=detail_time.time_mode.value,
            )
            if default_time_dimension_id is not None and detail_time.unit != -1
            else None
        ),
        aggregate_time_default=(
            DatasetTimeDefaultConfig(
                unit=aggregate_time.unit,
                period=aggregate_time.period.value,
                time_mode=aggregate_time.time_mode.value,
            )
            if default_time_dimension_id is not None and aggregate_time.unit != -1
            else None
        ),
    )


def catalog_dataset_from_topic_command(
    data_set: DatasetSpec,
    projection: SemanticRelease,
    previous: DataSetContract | None,
) -> DataSetContract:
    """Map the analysis-topic form command to the DataSet DTO."""
    if previous is not None:
        previous_projection = _compile_dataset(
            previous,
            projection.dimensions,
            projection.metrics,
            projection.fields,
        )
        member_fields = (
            "model_ids",
            "metric_ids",
            "dimension_ids",
        )
        scalar_fields = (
            "default_limit",
            "max_limit",
            "default_time_dimension_id",
            "detail_time_default",
            "aggregate_time_default",
        )
        if all(
            set(getattr(previous_projection, field_name)) == set(getattr(data_set, field_name))
            for field_name in member_fields
        ) and all(
            getattr(previous_projection, field_name) == getattr(data_set, field_name)
            for field_name in scalar_fields
        ):
            return previous.model_copy(
                update={
                    "name": data_set.name,
                    "biz_name": data_set.biz_name or previous.biz_name,
                    "description": data_set.description,
                    "alias": ",".join(data_set.aliases) or None,
                }
            )
    dimensions = {item.id: item for item in projection.dimensions}
    metrics = {item.id: item for item in projection.metrics}
    configs = []
    for model_id in data_set.model_ids:
        configs.append(
            DataSetModelConfigContract(
                id=model_id,
                dimensions=tuple(
                    item_id
                    for item_id in data_set.dimension_ids
                    if dimensions[item_id].model_id == model_id
                ),
                metrics=tuple(
                    item_id
                    for item_id in data_set.metric_ids
                    if metrics[item_id].model_id == model_id
                ),
            )
        )
    previous_detail = (
        previous.query_config.detail_type_default_config if previous is not None else None
    )
    previous_aggregate = (
        previous.query_config.aggregate_type_default_config if previous is not None else None
    )
    # The DataSet owns two independent TimeDefaultConfig values.
    # Rebuild them from the normalized projection so the AnalysisTopic API does
    # not lose QueryConfig when it atomically writes DataSet + RouteSpec.
    # Audited source: headless/api/.../DataSetReq.java and QueryConfig.java.
    query_config = QueryConfigContract(
        detail_type_default_config=DetailTypeDefaultConfigContract(
            time_default_config=_catalog_time_default(
                data_set.detail_time_default,
                previous=(previous_detail.time_default_config if previous_detail else None),
            ),
            limit=data_set.max_limit,
        ),
        aggregate_type_default_config=AggregateTypeDefaultConfigContract(
            time_default_config=_catalog_time_default(
                data_set.aggregate_time_default,
                previous=(previous_aggregate.time_default_config if previous_aggregate else None),
            ),
            limit=data_set.default_limit,
        ),
    )
    return DataSetContract(
        id=data_set.id,
        name=data_set.name,
        biz_name=data_set.biz_name or (previous.biz_name if previous else data_set.name),
        description=data_set.description,
        status=previous.status if previous else None,
        type_enum=previous.type_enum if previous else None,
        sensitive_level=previous.sensitive_level if previous else 0,
        domain_id=previous.domain_id if previous else None,
        data_set_detail=DataSetDetailContract(data_set_model_configs=tuple(configs)),
        alias=",".join(data_set.aliases) or None,
        query_config=query_config,
        admins=previous.admins if previous else (),
        admin_orgs=previous.admin_orgs if previous else (),
    )


def _catalog_time_default(
    value: DatasetTimeDefaultConfig | None,
    *,
    previous: TimeDefaultConfigContract | None,
) -> TimeDefaultConfigContract:
    if value is None:
        if previous is not None:
            return previous.model_copy(update={"unit": -1})
        return TimeDefaultConfigContract(unit=-1)
    return TimeDefaultConfigContract(
        unit=value.unit,
        period=DatePeriod(value.period),
        time_mode=TimeMode(value.time_mode),
    )


def _identifier_dimension(
    model_id: str,
    field: FieldSpec,
    identifier: IdentifierContract,
) -> DimensionSpec:
    return DimensionSpec(
        id=_stable_id("dimension", field.id),
        name=identifier.name,
        model_id=model_id,
        field_id=field.id,
        semantic_type="identifier",
    )


def _model_dimension(
    model_id: str,
    field: FieldSpec,
    dimension: ModelDimensionContract,
) -> DimensionSpec:
    semantic_type = (
        "time"
        if dimension.type in {ModelDimensionType.TIME, ModelDimensionType.PARTITION_TIME}
        else "categorical"
    )
    return DimensionSpec(
        id=_stable_id("dimension", field.id),
        name=dimension.name,
        model_id=model_id,
        field_id=field.id,
        description=dimension.description,
        semantic_type=semantic_type,
    )


def _measure_metric(
    model_id: str,
    field: FieldSpec,
    measure: MeasureContract,
) -> MetricSpec:
    return MetricSpec(
        id=_stable_id("metric", field.id),
        name=measure.name,
        model_id=model_id,
        kind=MetricKind.ATOMIC,
        field_id=field.id,
        aggregation=_aggregation(measure.agg),
        define_type=MetricDefineType.MEASURE.value,
        raw_filter_sql=measure.constraint,
        aliases=_aliases(measure.alias),
        unit=measure.unit,
    )


def _metric_params(metric: MetricContract):
    if metric.metric_define_type is MetricDefineType.FIELD:
        assert metric.metric_define_by_field_params is not None
        return metric.metric_define_by_field_params
    if metric.metric_define_type is MetricDefineType.MEASURE:
        assert metric.metric_define_by_measure_params is not None
        return metric.metric_define_by_measure_params
    assert metric.metric_define_by_metric_params is not None
    return metric.metric_define_by_metric_params


def _field_for_expr(
    fields: dict[tuple[str, str], FieldSpec],
    model_id: str,
    expression: str,
) -> FieldSpec:
    field = fields.get((model_id, expression))
    if field is None:
        raise SemanticValidationError(
            f"unknown physical field {expression!r} in model {model_id}",
            code="MODELING_FIELD_NOT_FOUND",
        )
    return field


def _aggregation(raw: str | None) -> Aggregation:
    if raw is None:
        raise SemanticValidationError(
            "metric aggregation is required",
            code="METRIC_AGGREGATION_REQUIRED",
        )
    normalized = raw.strip().casefold().replace(" ", "_")
    aliases = {"countdistinct": "count_distinct", "distinct_count": "count_distinct"}
    normalized = aliases.get(normalized, normalized)
    try:
        return Aggregation(normalized)
    except ValueError as exc:
        raise SemanticValidationError(
            f"unsupported metric aggregation: {raw}",
            code="METRIC_AGGREGATION_UNSUPPORTED",
        ) from exc


def _join_type(raw: str) -> JoinType:
    normalized = " ".join(raw.strip().casefold().split())
    aliases = {
        "left join": JoinType.LEFT,
        "left outer join": JoinType.LEFT,
        "inner join": JoinType.INNER,
        "right join": JoinType.RIGHT,
        "right outer join": JoinType.RIGHT,
        "full join": JoinType.FULL,
        "full outer join": JoinType.FULL,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return JoinType(normalized)
    except ValueError as exc:
        raise SemanticValidationError(
            f"unsupported model relation join type: {raw}",
            code="RELATION_JOIN_TYPE_UNSUPPORTED",
        ) from exc


def _table_source(table_query: str) -> tuple[str, str]:
    parts = [item.strip().strip('"') for item in table_query.split(".") if item.strip()]
    if not parts:
        raise SemanticValidationError(
            "tableQuery is empty",
            code="MODEL_TABLE_QUERY_INVALID",
        )
    if len(parts) == 1:
        return "public", parts[0]
    return parts[-2], parts[-1]


def _aliases(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _stable_id(prefix: str, *parts: str) -> str:
    raw = ":".join((prefix, *parts))
    if len(raw) <= 128:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _quote_identifier(expression: str) -> str:
    """Render a physical column name as a parseable SQL identifier.

    ``semantic_expr`` is parsed as SQL when a revision is validated. A bare
    Chinese name parses, but one starting with a digit (``500强排名``) reads as a
    number and resolves to no governed field. Only a plain column name is
    quoted; a real expression is left untouched so computed dimensions and
    metric formulas keep their meaning.
    """

    name = expression.strip()
    if not name:
        return expression
    if name.startswith('"') and name.endswith('"') and len(name) > 1:
        return name
    if any(character in name for character in "()+-*/,'\" "):
        return name
    return '"' + name.replace('"', '""') + '"'
