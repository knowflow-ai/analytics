from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetTimeDefaultConfig,
    DimensionValueSpec,
    JoinType,
    MetricKind,
    SemanticQuery,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_compiler import (
    catalog_dataset_from_topic_command,
    compile_semantic_catalog,
    replace_catalog_item,
    validate_m0_publishable,
)
from knowflow_analytics.modeling.catalog_contracts import (
    DimensionContract,
    FieldParamContract,
    MeasureContract,
    MetricDefineType,
    ModelDefineType,
    ModelFieldContract,
    SemanticCatalog,
    TimeDefaultConfigContract,
)
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    SuggestionDecision,
    SuggestionPatch,
    SuggestionSource,
    semantic_context_content_hash,
)
from knowflow_analytics.modeling.coverage import (
    ProductChainEvidence,
    build_modeling_coverage_report,
)
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.modeling.rule_modeller import stable_id
from knowflow_analytics.semantic.translator import SemanticTranslator

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def test_contract_fixture_round_trips_without_losing_metadata():
    catalog = _catalog()
    payload = catalog.canonical_payload()
    round_tripped = SemanticCatalog.model_validate(payload)

    assert round_tripped == catalog
    assert "upstreamCommit" not in payload
    assert payload["models"][0]["modelDetail"]["queryType"] == "table_query"
    assert payload["models"][2]["modelDetail"]["queryType"] == "sql_query"
    assert payload["models"][2]["modelDetail"]["sqlVariables"][0] == {
        "name": "channel",
        "valueType": "STRING",
        "defaultValues": ["线上"],
    }
    assert {item.metric_define_type for item in catalog.metrics} == {
        MetricDefineType.FIELD,
        MetricDefineType.MEASURE,
        MetricDefineType.METRIC,
    }
    assert catalog.models[0].model_detail.query_type is ModelDefineType.TABLE_QUERY


def test_dimension_values_must_reference_a_dimension_in_the_same_catalog():
    payload = _catalog().model_dump(mode="python")
    payload["dimension_values"] = (
        DimensionValueSpec(
            id="dimension_value_orphan",
            dimension_id="dimension_missing",
            value="east",
            display_name="华东",
        ),
    )

    with pytest.raises(ValueError, match="unknown dimension value"):
        SemanticCatalog.model_validate(payload)


def test_catalog_compiles_to_one_deterministic_query_projection():
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)

    assert release.modeling_catalog == catalog.canonical_payload()
    assert release.revision_id == catalog.revision_id
    assert release.spec_hash.startswith("sha256:")
    assert {
        (item.id, item.query_type, item.schema_name, item.table) for item in release.models
    } == {
        ("model_orders", "table_query", "analytics_v0", "orders"),
        ("model_customers", "table_query", "analytics_v0", "customers"),
        ("model_order_sql_contract", "sql_query", None, None),
    }
    metrics = {item.id: item for item in release.metrics}
    assert metrics["metric_order_count"].aggregation is Aggregation.COUNT_DISTINCT
    assert metrics["metric_revenue"].aggregation is Aggregation.SUM
    assert metrics["metric_avg_order_value"].kind is MetricKind.DERIVED
    assert metrics["metric_avg_order_value"].formula == ("{metric_revenue} / {metric_order_count}")
    assert release.relations[0].join_type is JoinType.LEFT
    assert release.datasets[0].model_ids == ("model_orders", "model_customers")
    assert release.datasets[0].biz_name == "sales_analysis"
    assert release.datasets[0].default_limit == 200
    assert release.datasets[0].max_limit == 500
    assert release.datasets[0].default_time_dimension_id == "dimension_order_time"
    assert release.datasets[0].detail_time_default.model_dump() == {
        "unit": 1,
        "period": "DAY",
        "time_mode": "LAST",
    }
    assert release.datasets[0].aggregate_time_default.model_dump() == {
        "unit": 7,
        "period": "DAY",
        "time_mode": "RECENT",
    }


def test_semantic_context_round_trips_and_changes_release_hash():
    from knowflow_analytics.contracts import SemanticContextEntry

    catalog = _catalog()
    context = SemanticContextEntry(
        id="ctx-project-currency",
        target_type="project",
        target_id=catalog.project_id,
        kind="convention",
        text="金额统一使用人民币。",
        source_type="human_convention",
    )
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(update={"semantic_context": (context,)}).model_dump(mode="python")
    )

    round_tripped = SemanticCatalog.model_validate(changed.canonical_payload())
    original_release = compile_semantic_catalog(catalog)
    release = compile_semantic_catalog(round_tripped)

    assert round_tripped.semantic_context == (context,)
    assert release.semantic_context == (context,)
    assert release.spec_hash != original_release.spec_hash


def test_semantic_context_rejects_unknown_targets_and_unbound_documents():
    from knowflow_analytics.contracts import SemanticContextEntry

    catalog = _catalog()
    with pytest.raises(ValueError, match="source reference"):
        SemanticContextEntry(
            id="ctx-document",
            target_type="project",
            target_id=catalog.project_id,
            kind="definition",
            text="文档口径",
            source_type="knowledge_document",
        )

    with pytest.raises(ValueError, match="pattern"):
        SemanticContextEntry(
            id="ctx-document-url",
            target_type="project",
            target_id=catalog.project_id,
            kind="definition",
            text="文档口径",
            source_type="knowledge_document",
            source_ref="https://example.test/document?token=secret",
        )

    unknown = SemanticContextEntry(
        id="ctx-unknown-metric",
        target_type="metric",
        target_id="missing-metric",
        kind="definition",
        text="不存在的指标",
        source_type="human_convention",
    )
    with pytest.raises(ValueError, match="unknown metric"):
        SemanticCatalog.model_validate(
            catalog.model_copy(update={"semantic_context": (unknown,)}).model_dump(
                mode="python"
            )
        )


def test_semantic_context_cannot_publish_without_artifact_bound_human_review():
    from knowflow_analytics.contracts import SemanticContextEntry

    catalog = _catalog()
    context = SemanticContextEntry(
        id="ctx-reviewed",
        target_type="project",
        target_id=catalog.project_id,
        kind="convention",
        text="金额统一使用人民币。",
        source_type="human_convention",
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(update={"semantic_context": (context,)}).model_dump(mode="python")
    )
    revision = ModelingRevision(
        id=catalog.revision_id,
        project_id=catalog.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_catalog=catalog,
        semantic_spec=compile_semantic_catalog(catalog),
    )

    with pytest.raises(SemanticValidationError) as raised:
        RevisionEditor().validate_for_publish(revision)
    assert raised.value.code == "UNREVIEWED_SEMANTIC_CONTEXT"

    reviewed = revision.model_copy(
        update={
            "semantic_context_review_hash": semantic_context_content_hash((context,)),
            "semantic_context_reviewed_by": "reviewer-1",
            "semantic_context_reviewed_at": datetime(2026, 8, 27, tzinfo=UTC),
        }
    )
    ModelingRevision.model_validate(reviewed.model_dump(mode="python"))


def test_semantic_context_has_a_per_scope_publish_prompt_budget():
    from knowflow_analytics.contracts import SemanticContextEntry

    catalog = _catalog()
    entries = tuple(
        SemanticContextEntry(
            id=f"ctx-{index}",
            target_type="project",
            target_id=catalog.project_id,
            kind="convention",
            text="x",
            source_type="human_convention",
        )
        for index in range(101)
    )
    oversized = SemanticCatalog.model_validate(
        catalog.model_copy(update={"semantic_context": entries}).model_dump(mode="python")
    )

    with pytest.raises(SemanticValidationError) as raised:
        validate_m0_publishable(oversized)

    assert raised.value.code == "SEMANTIC_CONTEXT_SCOPE_LIMIT_EXCEEDED"


def test_new_dataset_projection_preserves_business_name_and_query_config():
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    dataset = release.datasets[0].model_copy(
        update={
            "id": "dataset_custom_sales",
            "name": "自定义销售主题",
            "biz_name": "custom_sales_topic",
            "default_limit": 123,
            "max_limit": 456,
            "detail_time_default": DatasetTimeDefaultConfig(
                unit=2,
                period="MONTH",
                time_mode="LAST",
            ),
            "aggregate_time_default": DatasetTimeDefaultConfig(
                unit=3,
                period="WEEK",
                time_mode="RECENT",
            ),
        }
    )
    empty_catalog = catalog.model_copy(update={"data_sets": ()})

    catalog_dataset = catalog_dataset_from_topic_command(dataset, release, None)
    synchronized = replace_catalog_item(
        empty_catalog,
        collection="data_sets",
        item=catalog_dataset,
    )
    recompiled = compile_semantic_catalog(synchronized)

    assert synchronized.data_sets[0].biz_name == "custom_sales_topic"
    assert synchronized.data_sets[0].query_config.detail_type_default_config.limit == 456
    assert synchronized.data_sets[0].query_config.aggregate_type_default_config.limit == 123
    assert recompiled.datasets[0].biz_name == "custom_sales_topic"
    assert recompiled.datasets[0].detail_time_default == dataset.detail_time_default
    assert recompiled.datasets[0].aggregate_time_default == dataset.aggregate_time_default


def test_existing_dataset_projection_applies_reviewed_business_name_change():
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    dataset = release.datasets[0].model_copy(update={"biz_name": "reviewed_sales_topic"})

    catalog_dataset = catalog_dataset_from_topic_command(
        dataset,
        release,
        catalog.data_sets[0],
    )
    synchronized = replace_catalog_item(
        catalog,
        collection="data_sets",
        item=catalog_dataset,
    )
    recompiled = compile_semantic_catalog(synchronized)

    assert synchronized.data_sets[0].biz_name == "reviewed_sales_topic"
    assert recompiled.datasets[0].biz_name == "reviewed_sales_topic"


def test_explicit_dataset_scope_does_not_expand_after_field_materialization():
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    customer_id = next(
        field
        for field in release.fields
        if field.model_id == "model_orders" and field.column == "customer_id"
    )
    suggestion = SuggestionPatch(
        id="suggestion:orders:customer-id",
        target_kind="field",
        target_id=customer_id.id,
        changes={
            "kind": "identifier",
            "identifier_type": "foreign",
            "create_dimension": True,
            "create_metric": False,
        },
        source=SuggestionSource.DATABASE_CONSTRAINT,
        confidence=1.0,
        reason="database foreign key",
        high_impact=True,
    )
    revision = ModelingRevision(
        id=release.revision_id or release.id,
        project_id=release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_spec=release,
        semantic_catalog=catalog,
        suggestions=(suggestion,),
    )

    decided = RevisionEditor().apply_decisions(
        revision,
        expected_etag=1,
        expected_schema_snapshot_hash="sha256:schema",
        decisions=(SuggestionDecision(suggestion_id=suggestion.id, accept=True),),
    )

    materialized_id = stable_id("dimension", customer_id.id)
    assert materialized_id in {item.id for item in decided.semantic_spec.dimensions}
    assert materialized_id not in decided.semantic_spec.datasets[0].dimension_ids
    orders_config = next(
        item
        for item in decided.semantic_catalog.data_sets[0].data_set_detail.data_set_model_configs
        if item.id == "model_orders"
    )
    assert orders_config.includes_all is False
    assert materialized_id not in orders_config.dimensions


def test_includes_all_dataset_scope_expands_after_field_materialization():
    catalog = _catalog()
    dataset = catalog.data_sets[0]
    configs = tuple(
        config.model_copy(update={"includes_all": True, "metrics": (), "dimensions": ()})
        if config.id == "model_orders"
        else config
        for config in dataset.data_set_detail.data_set_model_configs
    )
    catalog = catalog.model_copy(
        update={
            "data_sets": (
                dataset.model_copy(
                    update={
                        "data_set_detail": dataset.data_set_detail.model_copy(
                            update={"data_set_model_configs": configs}
                        )
                    }
                ),
            )
        }
    )
    release = compile_semantic_catalog(catalog)
    customer_id = next(
        field
        for field in release.fields
        if field.model_id == "model_orders" and field.column == "customer_id"
    )
    suggestion = SuggestionPatch(
        id="suggestion:orders:customer-id",
        target_kind="field",
        target_id=customer_id.id,
        changes={
            "kind": "identifier",
            "identifier_type": "foreign",
            "create_dimension": True,
            "create_metric": False,
        },
        source=SuggestionSource.DATABASE_CONSTRAINT,
        confidence=1.0,
        reason="database foreign key",
        high_impact=True,
    )
    revision = ModelingRevision(
        id=release.revision_id or release.id,
        project_id=release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_spec=release,
        semantic_catalog=catalog,
        suggestions=(suggestion,),
    )

    decided = RevisionEditor().apply_decisions(
        revision,
        expected_etag=1,
        expected_schema_snapshot_hash="sha256:schema",
        decisions=(SuggestionDecision(suggestion_id=suggestion.id, accept=True),),
    )

    materialized_id = stable_id("dimension", customer_id.id)
    assert materialized_id in decided.semantic_spec.datasets[0].dimension_ids
    orders_config = next(
        item
        for item in decided.semantic_catalog.data_sets[0].data_set_detail.data_set_model_configs
        if item.id == "model_orders"
    )
    assert orders_config.includes_all is True
    assert orders_config.dimensions == ()


def test_catalog_write_preserves_model_scoped_duplicate_dimension_names():
    """Match DataSetServiceImpl save/update and SqlQueryParser model scoping."""
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    conflicting_dimensions = tuple(
        item.model_copy(update={"name": "渠道"}) if item.id == "dimension_customer_name" else item
        for item in catalog.dimensions
    )
    conflicting_catalog = SemanticCatalog.model_validate(
        catalog.model_copy(update={"dimensions": conflicting_dimensions}).model_dump(mode="python")
    )
    revision = ModelingRevision(
        id=release.revision_id or release.id,
        project_id=release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_spec=release,
        semantic_catalog=catalog,
    )

    updated = RevisionEditor().replace_semantic_catalog(
        revision,
        expected_etag=1,
        expected_schema_snapshot_hash="sha256:schema",
        semantic_catalog=conflicting_catalog,
    )

    duplicate_dimensions = [
        item for item in updated.semantic_spec.dimensions if item.name == "渠道"
    ]
    dataset_dimension_ids = set(updated.semantic_spec.datasets[0].dimension_ids)
    assert len(duplicate_dimensions) == 2
    assert len({item.id for item in duplicate_dimensions}) == 2
    assert len({item.model_id for item in duplicate_dimensions}) == 2
    assert {item.id for item in duplicate_dimensions}.issubset(dataset_dimension_ids)


def test_sql_model_is_publishable_through_the_same_model_contract():
    catalog = _catalog()

    # SQL_QUERY models render as governed subqueries;
    # the Python publication gate must therefore accept the same ModelDetail.
    validate_m0_publishable(catalog)

    table_only = SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": catalog.models[:2]}).model_dump(mode="python")
    )
    validate_m0_publishable(table_only)


def test_m0_publish_gate_requires_explicit_relation_cardinality():
    catalog = _catalog()
    relation = catalog.model_relations[0].model_copy(update={"knowflow_cardinality": None})
    unconfirmed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": catalog.models[:2],
                "model_relations": (relation,),
            }
        ).model_dump(mode="python")
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        validate_m0_publishable(unconfirmed)

    assert exc_info.value.code == "RELATION_CARDINALITY_REQUIRED"


def test_m1_model_filter_is_compiled_to_a_governed_physical_predicate():
    catalog = _catalog()
    model = catalog.models[0].model_copy(update={"filter_sql": "customer_id > 0"})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": (model, catalog.models[1])}).model_dump(mode="python")
    )

    validate_m0_publishable(changed)
    release = compile_semantic_catalog(changed)
    compiled = next(item for item in release.models if item.id == "model_orders")

    assert compiled.filter_sql == "(customer_id > 0)"
    assert len(compiled.filters) == 1
    assert compiled.filters[0].operator.value == "gt"
    assert (
        next(item for item in release.fields if item.id == compiled.filters[0].field_id).column
        == "customer_id"
    )
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="dataset_sales",
            metric_ids=("metric_revenue",),
        ),
    )
    assert 'WHERE ("customer_id" > :p0)' in physical.sql
    assert physical.parameters == {"p0": 0}


def test_m1_fixed_filter_rejects_unknown_fields_and_arbitrary_sql():
    catalog = _catalog()
    model = catalog.models[0].model_copy(update={"filter_sql": "tenant_id = 1"})
    unknown_field = SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": (model, catalog.models[1])}).model_dump(mode="python")
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        compile_semantic_catalog(unknown_field)

    assert exc_info.value.code == "FIXED_FILTER_FIELD_NOT_FOUND"

    unsafe_model = catalog.models[0].model_copy(
        update={"filter_sql": "customer_id = 1 OR amount > 0"}
    )
    unsafe = SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": (unsafe_model, catalog.models[1])}).model_dump(
            mode="python"
        )
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        compile_semantic_catalog(unsafe)

    assert exc_info.value.code == "FIXED_FILTER_EXPRESSION_UNSUPPORTED"


def test_m0_publish_gate_rejects_non_equality_relation_operators():
    catalog = _catalog()
    condition = catalog.model_relations[0].join_conditions[0].model_copy(update={"operator": ">"})
    relation = catalog.model_relations[0].model_copy(update={"join_conditions": (condition,)})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": catalog.models[:2],
                "model_relations": (relation,),
            }
        ).model_dump(mode="python")
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        validate_m0_publishable(changed)

    assert exc_info.value.code == "RELATION_OPERATOR_UNSUPPORTED"


def test_measure_expression_preserves_the_pinned_complete_expression_wrapping():
    catalog = _catalog()
    revenue = catalog.metrics[1]
    params = revenue.metric_define_by_measure_params.model_copy(update={"expr": "amount * 1.13"})
    changed_revenue = revenue.model_copy(update={"metric_define_by_measure_params": params})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": catalog.models[:2],
                "metrics": (catalog.metrics[0], changed_revenue, catalog.metrics[2]),
            }
        ).model_dump(mode="python")
    )

    release = compile_semantic_catalog(changed)
    compiled = next(item for item in release.metrics if item.id == "metric_revenue")
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="dataset_sales",
            metric_ids=("metric_revenue",),
        ),
    )

    assert compiled.kind is MetricKind.DERIVED
    assert compiled.formula == "amount * 1.13"
    assert tuple(item.name for item in compiled.expression_sources) == ("amount",)
    assert 'SUM("m0"."amount" * 1.13) * 1.13' in physical.sql


def test_field_metric_expression_preserves_all_selected_fields_and_aggregates():
    """Match MetricFieldFormTable and MetricExpressionParser FIELD behavior."""

    catalog = _catalog()
    orders = catalog.models[0]
    detail = orders.model_detail.model_copy(
        update={
            "fields": (
                *orders.model_detail.fields,
                ModelFieldContract(field_name="refund_amount", data_type="numeric"),
            )
        }
    )
    orders = orders.model_copy(update={"model_detail": detail})
    metric = catalog.metrics[0]
    params = metric.metric_define_by_field_params.model_copy(
        update={
            "expr": "SUM(amount) - SUM(refund_amount)",
            "fields": (
                FieldParamContract(field_name="amount"),
                FieldParamContract(field_name="refund_amount"),
            ),
        }
    )
    metric = metric.model_copy(update={"metric_define_by_field_params": params})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": (orders, catalog.models[1]),
                "metrics": (metric, catalog.metrics[1], catalog.metrics[2]),
            }
        ).model_dump(mode="python")
    )

    release = compile_semantic_catalog(changed)
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="dataset_sales",
            metric_ids=("metric_order_count",),
        ),
    )

    assert 'SUM("m0"."amount") - SUM("m0"."refund_amount")' in physical.sql


def test_measure_metric_expression_preserves_the_pinned_nesting_behavior():
    """Parity: every measure wraps the complete expression in the pinned source."""

    catalog = _catalog()
    orders = catalog.models[0]
    refund_measure = MeasureContract(
        name="退款金额",
        agg="SUM",
        expr="refund_amount",
        biz_name="refund_amount",
        unit="元",
    )
    detail = orders.model_detail.model_copy(
        update={
            "fields": (
                *orders.model_detail.fields,
                ModelFieldContract(field_name="refund_amount", data_type="numeric"),
            ),
            "measures": (*orders.model_detail.measures, refund_measure),
        }
    )
    orders = orders.model_copy(update={"model_detail": detail})
    metric = catalog.metrics[1]
    params = metric.metric_define_by_measure_params.model_copy(
        update={
            "expr": "amount - refund_amount",
            "measures": (*metric.metric_define_by_measure_params.measures, refund_measure),
        }
    )
    metric = metric.model_copy(update={"metric_define_by_measure_params": params})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": (orders, catalog.models[1]),
                "metrics": (catalog.metrics[0], metric, catalog.metrics[2]),
            }
        ).model_dump(mode="python")
    )

    release = compile_semantic_catalog(changed)
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="dataset_sales",
            metric_ids=("metric_revenue",),
        ),
    )

    assert (
        'SUM("m0"."amount" - "m0"."refund_amount") - SUM("m0"."amount" - "m0"."refund_amount")'
    ) in physical.sql


def test_measure_metric_executes_the_model_measure_definition_as_source_of_truth():
    """Parity: MetricExpressionParser reads agg/expr from ModelDetail.measures."""

    catalog = _catalog()
    orders = catalog.models[0]
    amount = orders.model_detail.measures[0].model_copy(update={"agg": "MAX"})
    orders = orders.model_copy(
        update={"model_detail": orders.model_detail.model_copy(update={"measures": (amount,)})}
    )
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": (orders, *catalog.models[1:])}).model_dump(
            mode="python"
        )
    )

    release = compile_semantic_catalog(changed)
    compiled = next(item for item in release.metrics if item.id == "metric_revenue")

    assert compiled.aggregation is Aggregation.MAX


def test_computed_dimension_expression_is_used_for_projection_and_grouping():
    """Match DimExpressionParser's dimension bizName-to-expression replacement."""

    catalog = _catalog()
    dimension = catalog.dimensions[0].model_copy(update={"expr": "DATE_TRUNC('month', order_time)"})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": catalog.models[:2],
                "dimensions": (dimension, *catalog.dimensions[1:]),
            }
        ).model_dump(mode="python")
    )

    release = compile_semantic_catalog(changed)
    physical = SemanticTranslator().translate(
        release=release,
        query=SemanticQuery(
            dataset_id="dataset_sales",
            metric_ids=("metric_revenue",),
            dimension_ids=("dimension_order_time",),
        ),
    )

    assert 'DATE_TRUNC(\'MONTH\', "m0"."order_time")' in physical.sql
    assert 'GROUP BY DATE_TRUNC(\'MONTH\', "m0"."order_time")' in physical.sql


def test_dataset_limit_edit_round_trips_through_query_config():
    """The browser form must round-trip the dataset QueryConfig without loss."""

    catalog = _catalog()
    projection = compile_semantic_catalog(catalog)
    original = catalog.data_sets[0].query_config
    dataset = projection.datasets[0].model_copy(update={"default_limit": 321, "max_limit": 654})
    catalog_dataset = catalog_dataset_from_topic_command(
        dataset,
        projection,
        catalog.data_sets[0],
    )
    updated_catalog = replace_catalog_item(
        catalog,
        collection="data_sets",
        item=catalog_dataset,
    )
    query_config = updated_catalog.data_sets[0].query_config
    recompiled = compile_semantic_catalog(updated_catalog)

    assert query_config.aggregate_type_default_config.limit == 321
    assert query_config.detail_type_default_config.limit == 654
    assert (
        query_config.aggregate_type_default_config.time_default_config
        == original.aggregate_type_default_config.time_default_config
    )
    assert (
        query_config.detail_type_default_config.time_default_config
        == original.detail_type_default_config.time_default_config
    )
    assert recompiled.datasets[0].default_limit == 321
    assert recompiled.datasets[0].max_limit == 654


def test_dataset_time_default_unit_minus_one_disables_automatic_time_filter():
    """``unit = -1`` is the explicit "no automatic time filter" switch."""

    catalog = _catalog()
    dataset = catalog.data_sets[0]
    query_config = dataset.query_config
    detail = query_config.detail_type_default_config.model_copy(
        update={"time_default_config": TimeDefaultConfigContract(unit=-1)}
    )
    aggregate = query_config.aggregate_type_default_config.model_copy(
        update={"time_default_config": TimeDefaultConfigContract(unit=-1)}
    )
    dataset = dataset.model_copy(
        update={
            "query_config": query_config.model_copy(
                update={
                    "detail_type_default_config": detail,
                    "aggregate_type_default_config": aggregate,
                }
            )
        }
    )
    release = compile_semantic_catalog(catalog.model_copy(update={"data_sets": (dataset,)}))

    assert release.datasets[0].default_time_dimension_id == "dimension_order_time"
    assert release.datasets[0].detail_time_default is None
    assert release.datasets[0].aggregate_time_default is None


def test_dataset_time_default_rejects_zero_but_accepts_minus_one():
    assert TimeDefaultConfigContract(unit=-1).unit == -1
    with pytest.raises(ValueError):
        TimeDefaultConfigContract(unit=0)


def test_dimension_alias_save_replaces_the_reviewed_snapshot_instead_of_unioning_it():
    """The alias submitted through the dimension form is authoritative."""

    catalog = _catalog()
    original = catalog.dimensions[0]
    first = DimensionContract.model_validate(
        original.model_copy(update={"alias": "north,east"}).model_dump(mode="python")
    )
    second = DimensionContract.model_validate(
        original.model_copy(update={"alias": "central"}).model_dump(mode="python")
    )

    catalog = replace_catalog_item(catalog, collection="dimensions", item=first)
    catalog = replace_catalog_item(catalog, collection="dimensions", item=second)

    saved = next(item for item in catalog.dimensions if item.id == original.id)
    assert saved.alias == "central"


@pytest.mark.parametrize(
    ("join_type", "expected"),
    (
        ("inner join", JoinType.INNER),
        ("left outer join", JoinType.LEFT),
        ("right outer join", JoinType.RIGHT),
        ("full outer join", JoinType.FULL),
    ),
)
def test_join_variants_are_normalized_without_prompt_or_dataset_rules(
    join_type: str,
    expected: JoinType,
):
    catalog = _catalog()
    relation = catalog.model_relations[0].model_copy(update={"join_type": join_type})
    changed = SemanticCatalog.model_validate(
        catalog.model_copy(update={"model_relations": (relation,)}).model_dump(mode="python")
    )

    assert compile_semantic_catalog(changed).relations[0].join_type is expected


def test_modeling_coverage_report_requires_every_product_gate():
    digest = "sha256:" + "a" * 64
    report = build_modeling_coverage_report(
        fixture_payload=json.loads(_FIXTURE.read_text(encoding="utf-8")),
        product_chain=ProductChainEvidence(
            api_sequence=(
                "schema_snapshot.create",
                "revision.create_empty",
                "model.create_from_table",
                "identifier.review",
                "model_dimension.upsert",
                "measure.upsert",
                "relation.review",
                "dimension.upsert",
                "metric.FIELD.upsert",
                "metric.MEASURE.upsert",
                "metric.METRIC.upsert",
                "dataset.upsert",
                "term.upsert",
                "dimension_value.upsert",
                "revision.validate",
                "revision.publish",
                "release.reload",
            ),
            human_decisions=(
                "table_source",
                "field_classification",
                "identifier_subtype",
                "time_parameters",
                "measure_aggregation",
                "relation_join_and_cardinality",
                "metric_definitions",
                "dataset_scope",
                "business_dictionary",
                "publish_confirmation",
            ),
            revision_spec_hash=digest,
            revision_catalog_hash=digest,
            release_spec_hash=digest,
            reloaded_catalog_hash=digest,
            authenticated_http_only=True,
            real_postgresql=True,
            restarted_and_reloaded=True,
            published_release_id="release_sales",
            executed_release_id="release_sales",
            executed_spec_hash=digest,
            query_state="COMPLETED",
        ),
    )

    assert report.gate_passed is True
    assert report.unreviewed_behaviors == ()
    assert len(report.object_coverage) == 10
    assert {item.layer: item.covered for item in report.layer_coverage} == {
        "contract": True,
        "ui": True,
        "publish": True,
        "execute": True,
    }
    assert report.datasource_scope == "single-postgresql-datasource"


def test_compiled_semantic_expression_survives_sql_parsing():
    """Compiled semantic_expr is parsed as SQL when a revision is validated. A
    digit-leading physical name such as 500强排名 reads as a number and resolves
    to no governed field, so compilation must emit a quoted identifier."""

    from knowflow_analytics.modeling.catalog_compiler import _quote_identifier

    assert _quote_identifier("500强排名") == '"500强排名"'
    assert _quote_identifier("平均房价（万）") == '"平均房价（万）"'
    assert _quote_identifier("net_amount") == '"net_amount"'
    assert _quote_identifier('"已加引号"') == '"已加引号"'


def test_catalog_persisted_before_upstream_commit_was_retired_still_loads():
    """Releases store the catalog verbatim, so an older payload must still load.

    The contract is ``extra="forbid"``; without the retirement shim a revision
    published before the key was dropped would fail to reload.
    """

    legacy = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    legacy["upstreamCommit"] = "a" * 40

    catalog = SemanticCatalog.model_validate(legacy)

    assert "upstreamCommit" not in catalog.canonical_payload()
    assert catalog == SemanticCatalog.model_validate(
        json.loads(_FIXTURE.read_text(encoding="utf-8"))
    )


# ---- 存量 Revision 兼容:退役键不阻断加载 ---------------------------------------


def _bound_revision():
    catalog = _catalog()
    release = compile_semantic_catalog(catalog)
    return ModelingRevision(
        id=release.revision_id or release.id,
        project_id=release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_spec=release,
        semantic_catalog=catalog,
    )


def test_a_revision_stored_before_a_key_was_retired_still_loads():
    """真实事故:去掉 upstreamCommit 字段后,catalog 侧有 drop_retired_keys 兜着,
    但投影比较拿原始存储字典做全等 → 所有历史 Revision 拒载 → 全线 INTERNAL_ERROR。
    两侧必须过同一合同归一化,退役键对称丢弃。"""

    payload = _bound_revision().model_dump(mode="json")
    payload["semantic_catalog"]["upstreamCommit"] = "af08d869c4609bf8d48d64e78c61427f"
    payload["semantic_spec"]["modeling_catalog"]["upstreamCommit"] = (
        "af08d869c4609bf8d48d64e78c61427f"
    )

    loaded = ModelingRevision.model_validate(payload)

    assert loaded.id == payload["id"]
    # 退役键保留在内存投影里无害:比较已归一化,下一次正常写入会自然收敛。


def test_a_genuinely_diverged_projection_is_still_refused():
    import pydantic

    payload = _bound_revision().model_dump(mode="json")
    payload["semantic_spec"]["modeling_catalog"]["models"][0]["name"] = "被改过的名字"

    try:
        ModelingRevision.model_validate(payload)
    except pydantic.ValidationError as exc:
        assert "projection differ" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("diverged projection must be refused")


def test_a_wrapped_field_expression_is_not_a_simple_metric():
    """「恰好一列」不等于「实参就是裸列」。

    SUM(net_amount * 2) 也只有一列,此前被判成 simple 并编译成裸 SUM(net_amount)
    —— * 2 被静默丢掉,数字对半错(实测 300 对 600)。带包装的表达式必须走完整
    FIELD 编译。存量 42 个 FIELD 指标扫描确认全为真裸列形态,无受损数据。
    """

    from knowflow_analytics.modeling.semantic_expression import simple_field_metric

    assert simple_field_metric("SUM(net_amount * 2)") is None
    assert simple_field_metric("SUM(COALESCE(net_amount, 0))") is None
    # 裸列(含中文列名)与 COUNT(DISTINCT 裸列) 仍是退化形态。
    assert simple_field_metric("SUM(net_amount)") == ("net_amount", "sum")
    assert simple_field_metric("COUNT(词条id)") == ("词条id", "count")
    assert simple_field_metric("COUNT(DISTINCT customer_id)") == ("customer_id", "count_distinct")
