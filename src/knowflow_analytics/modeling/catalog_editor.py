from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from knowflow_analytics.contracts import FieldKind
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_compiler import (
    compile_semantic_catalog,
    replace_catalog_item,
)
from knowflow_analytics.modeling.catalog_contracts import (
    DimensionContract,
    IdentifierContract,
    IdentifierType,
    JoinConditionContract,
    MeasureContract,
    MetricContract,
    MetricDefineByMeasureParamsContract,
    MetricDefineType,
    ModelContract,
    ModelDimensionContract,
    ModelDimensionType,
    ModelRelationContract,
    SemanticCatalog,
)
from knowflow_analytics.modeling.contracts import SuggestionPatch
from knowflow_analytics.modeling.rule_modeller import stable_id


def upsert_model_aggregate(
    catalog: SemanticCatalog,
    model: ModelContract,
) -> SemanticCatalog:
    """Save a model plus its isCreateDimension/isCreateMetric side effects.

    Parity: ``ModelServiceImpl.createModel/updateModel`` persists ModelDetail and
    then calls ``ModelConverter.convertDimensionList/convertMetricList`` followed
    by the two ``alter*Batch`` methods. Those methods create missing resources by
    model-local bizName without overwriting independently governed resources.
    """

    updated = replace_catalog_item(catalog, collection="models", item=model)
    projection = compile_semantic_catalog(updated)
    fields = {item.column: item for item in projection.fields if item.model_id == model.id}
    dimensions = list(updated.dimensions)
    metrics = list(updated.metrics)
    dimension_keys = {(item.model_id, item.biz_name) for item in dimensions}
    metric_keys = {(item.model_id, item.biz_name) for item in metrics}

    for identifier in model.model_detail.identifiers:
        if not identifier.is_create_dimension or not identifier.name.strip():
            continue
        key = (model.id, identifier.biz_name)
        if key in dimension_keys:
            continue
        field = fields.get(identifier.biz_name)
        if field is None:
            continue
        dimensions.append(
            DimensionContract(
                id=stable_id("dimension", field.id),
                name=identifier.name,
                biz_name=identifier.biz_name,
                description=identifier.name,
                model_id=model.id,
                type=(
                    ModelDimensionType.PRIMARY_KEY.value
                    if identifier.type is IdentifierType.PRIMARY
                    else ModelDimensionType.FOREIGN_KEY.value
                ),
                expr=identifier.biz_name,
                semantic_type="CATEGORY",
                data_type=field.data_type,
            )
        )
        dimension_keys.add(key)

    for dimension in model.model_detail.dimensions:
        if not dimension.is_create_dimension or not dimension.name.strip():
            continue
        key = (model.id, dimension.biz_name)
        if key in dimension_keys:
            continue
        dimensions.append(
            DimensionContract(
                id=stable_id("dimension", fields[dimension.expr].id)
                if dimension.expr in fields
                else stable_id("dimension", model.id, dimension.biz_name),
                name=dimension.name,
                biz_name=dimension.biz_name,
                description=dimension.description or dimension.name,
                model_id=model.id,
                type=dimension.type.value,
                expr=dimension.expr,
                semantic_type=(
                    "DATE"
                    if dimension.type
                    in {ModelDimensionType.TIME, ModelDimensionType.PARTITION_TIME}
                    else "CATEGORY"
                ),
                data_type=dimension.data_type,
                type_params=dimension.type_params,
                ext={"dateFormat": dimension.date_format},
            )
        )
        dimension_keys.add(key)

    for measure in model.model_detail.measures:
        if not measure.is_create_metric or not measure.name.strip():
            continue
        key = (model.id, measure.biz_name)
        if key in metric_keys:
            continue
        field = fields.get(measure.expr)
        metrics.append(
            MetricContract(
                id=(
                    stable_id("metric", field.id)
                    if field is not None
                    else stable_id("metric", model.id, measure.biz_name)
                ),
                name=measure.name,
                biz_name=measure.biz_name,
                description=measure.name,
                model_id=model.id,
                metric_define_type=MetricDefineType.MEASURE,
                metric_define_by_measure_params=MetricDefineByMeasureParamsContract(
                    expr=measure.expr,
                    measures=(measure,),
                ),
            )
        )
        metric_keys.add(key)

    return SemanticCatalog.model_validate(
        updated.model_copy(
            update={"dimensions": tuple(dimensions), "metrics": tuple(metrics)}
        ).model_dump(mode="python")
    )


def apply_catalog_suggestion(
    catalog: SemanticCatalog,
    suggestion: SuggestionPatch,
) -> SemanticCatalog:
    """Apply one reviewed suggestion directly to Catalog, never to projection."""

    projection = compile_semantic_catalog(catalog)
    if suggestion.target_kind == "model":
        model = _require_model(catalog, suggestion.target_id)
        changes = suggestion.changes
        updated = model.model_copy(
            update={
                **({"name": changes["name"]} if "name" in changes else {}),
                **({"biz_name": changes["biz_name"]} if "biz_name" in changes else {}),
                **({"description": changes["description"]} if "description" in changes else {}),
                **({"alias": ",".join(changes["aliases"]) or None} if "aliases" in changes else {}),
            }
        )
        return upsert_model_aggregate(
            catalog,
            ModelContract.model_validate(updated.model_dump(mode="python")),
        )
    if suggestion.target_kind == "field":
        field = next(
            (item for item in projection.fields if item.id == suggestion.target_id),
            None,
        )
        if field is None:
            raise SemanticValidationError("suggestion field was not found", code="FIELD_NOT_FOUND")
        model = _require_model(catalog, field.model_id)
        updated = _apply_field_changes(model, field.column, field, suggestion.changes)
        return upsert_model_aggregate(catalog, updated)

    relation = next(
        (item for item in catalog.model_relations if item.id == suggestion.target_id),
        None,
    )
    if relation is None:
        raise SemanticValidationError(
            "suggestion relation was not found",
            code="RELATION_NOT_FOUND",
        )
    changes = suggestion.changes
    fields_by_id = {item.id: item for item in projection.fields}
    conditions = relation.join_conditions
    if "conditions" in changes:
        conditions = tuple(
            JoinConditionContract(
                left_field=fields_by_id[item["left_field_id"]].column,
                right_field=fields_by_id[item["right_field_id"]].column,
                operator="=",
            )
            for item in changes["conditions"]
        )
    join_type = str(changes.get("join_type", relation.join_type))
    if not join_type.casefold().endswith(" join"):
        join_type = f"{join_type} join"
    updated_relation = ModelRelationContract(
        id=relation.id,
        domain_id=relation.domain_id,
        from_model_id=str(changes.get("left_model_id", relation.from_model_id)),
        to_model_id=str(changes.get("right_model_id", relation.to_model_id)),
        join_type=join_type,
        join_conditions=conditions,
        knowflow_cardinality=changes.get(
            "cardinality",
            relation.knowflow_cardinality,
        ),
    )
    return replace_catalog_item(
        catalog,
        collection="model_relations",
        item=updated_relation,
    )


def _apply_field_changes(
    model: ModelContract,
    column: str,
    field: Any,
    changes: Mapping[str, Any],
) -> ModelContract:
    old_identifier = next(
        (item for item in model.model_detail.identifiers if item.biz_name == column),
        None,
    )
    old_dimension = next(
        (item for item in model.model_detail.dimensions if item.biz_name == column),
        None,
    )
    old_measure = next(
        (item for item in model.model_detail.measures if item.biz_name == column),
        None,
    )
    identifiers = [item for item in model.model_detail.identifiers if item.biz_name != column]
    dimensions = [item for item in model.model_detail.dimensions if item.biz_name != column]
    measures = [item for item in model.model_detail.measures if item.biz_name != column]
    kind = FieldKind(changes.get("kind", field.kind.value))
    name = str(changes.get("name", field.name))
    description = str(changes.get("description", field.description))

    if kind is FieldKind.IDENTIFIER:
        identifiers.append(
            IdentifierContract(
                name=name,
                type=IdentifierType(
                    changes.get(
                        "identifier_type",
                        field.identifier_type
                        or (old_identifier.type.value if old_identifier else "primary"),
                    )
                ),
                biz_name=column,
                is_create_dimension=int(changes.get("create_dimension", field.create_dimension)),
            )
        )
    elif kind in {FieldKind.DIMENSION, FieldKind.TIME}:
        dimension_type = changes.get("dimension_type") or field.dimension_type
        if dimension_type is None:
            dimension_type = "time" if kind is FieldKind.TIME else "categorical"
        dimensions.append(
            ModelDimensionContract(
                name=name,
                type=ModelDimensionType(dimension_type),
                expr=column,
                date_format=old_dimension.date_format if old_dimension else "yyyy-MM-dd",
                data_type=field.data_type,
                type_params=old_dimension.type_params if old_dimension else None,
                is_create_dimension=int(changes.get("create_dimension", field.create_dimension)),
                biz_name=column,
                description=description,
            )
        )
    elif kind is FieldKind.MEASURE:
        aggregation = changes.get("aggregation") or (
            field.default_aggregation.value if field.default_aggregation else None
        )
        if aggregation is None:
            aggregation = old_measure.agg if old_measure else "SUM"
        measures.append(
            MeasureContract(
                name=name,
                agg=str(aggregation).upper(),
                expr=column,
                biz_name=column,
                is_create_metric=int(changes.get("create_metric", field.create_metric)),
                constraint=old_measure.constraint if old_measure else None,
                alias=old_measure.alias if old_measure else None,
                unit=changes.get("unit", field.unit),
            )
        )

    detail = model.model_detail.model_copy(
        update={
            "identifiers": tuple(identifiers),
            "dimensions": tuple(dimensions),
            "measures": tuple(measures),
        }
    )
    return ModelContract.model_validate(
        model.model_copy(update={"model_detail": detail}).model_dump(mode="python")
    )


def _require_model(catalog: SemanticCatalog, model_id: str) -> ModelContract:
    model = next((item for item in catalog.models if item.id == model_id), None)
    if model is None:
        raise SemanticValidationError("model was not found", code="MODEL_NOT_FOUND")
    return model
