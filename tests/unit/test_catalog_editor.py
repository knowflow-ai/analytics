from __future__ import annotations

import json
from pathlib import Path

from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    MeasureContract,
    ModelFieldContract,
    SemanticCatalog,
)
from knowflow_analytics.modeling.catalog_editor import (
    apply_catalog_suggestion,
    upsert_model_aggregate,
)
from knowflow_analytics.modeling.contracts import (
    SuggestionPatch,
    SuggestionSource,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def test_model_aggregate_materializes_only_missing_enabled_measure_metrics():
    """Match ModelServiceImpl -> ModelConverter -> alterMetricBatch semantics."""

    catalog = _catalog()
    model = catalog.models[0]
    detail = model.model_detail.model_copy(
        update={
            "fields": (
                *model.model_detail.fields,
                ModelFieldContract(field_name="gross_margin", data_type="numeric"),
            ),
            "measures": (
                *model.model_detail.measures,
                MeasureContract(
                    name="毛利",
                    agg="SUM",
                    expr="gross_margin",
                    biz_name="gross_margin",
                    is_create_metric=1,
                ),
            ),
        }
    )

    created = upsert_model_aggregate(
        catalog,
        model.model_copy(update={"model_detail": detail}),
    )
    metric = next(item for item in created.metrics if item.biz_name == "gross_margin")
    governed = metric.model_copy(update={"name": "已审核毛利", "alias": "确认毛利"})
    governed_catalog = created.model_copy(
        update={
            "metrics": tuple(governed if item.id == metric.id else item for item in created.metrics)
        }
    )

    saved_again = upsert_model_aggregate(governed_catalog, created.models[0])
    preserved = next(item for item in saved_again.metrics if item.id == metric.id)

    assert preserved.name == "已审核毛利"
    assert preserved.alias == "确认毛利"
    assert len([item for item in saved_again.metrics if item.biz_name == "gross_margin"]) == 1


def test_reviewed_field_suggestion_updates_catalog_then_recompiles_projection():
    """Field IDs are resolved from Catalog, not from dataset-specific names."""

    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    field = next(item for item in release.fields if item.column == "amount")
    suggestion = SuggestionPatch(
        id="suggestion:field-role",
        target_kind="field",
        target_id=field.id,
        changes={
            "name": "确认金额",
            "kind": "measure",
            "aggregation": "sum",
            "create_metric": True,
        },
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.9,
        reason="reviewed role proposal",
    )

    updated = apply_catalog_suggestion(catalog, suggestion)
    projection = compile_semantic_catalog(updated)
    updated_field = next(item for item in projection.fields if item.id == field.id)

    assert updated_field.name == "确认金额"
    assert updated_field.kind.value == "measure"
    assert any(
        item.model_id == field.model_id and item.biz_name == field.column
        for item in updated.metrics
    )
