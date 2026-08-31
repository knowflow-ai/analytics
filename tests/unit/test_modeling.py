from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    FieldKind,
    FieldSpec,
    FilterOperator,
    FixedFilter,
    MetricKind,
    MetricSpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.contracts import (
    RevisionState,
    SuggestionDecision,
    SuggestionState,
)
from knowflow_analytics.modeling.revision import RevisionConflictError, RevisionEditor
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller


def test_rule_modeler_keeps_schema_facts_separate_from_suggestions(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)

    assert len(result.semantic_spec.models) == 2
    assert len(result.semantic_spec.fields) == 7
    assert {field.kind for field in result.semantic_spec.fields} == {FieldKind.FIELD}
    assert result.semantic_spec.metrics == ()
    assert result.semantic_spec.dimensions == ()
    assert any(item.target_kind == "relation" for item in result.suggestions)
    assert all(item.state is SuggestionState.PENDING for item in result.suggestions)


def test_postgresql_quoted_identifiers_are_preserved():
    model = ModelSpec(id="model-1", name="销售明细", schema_name="业务 域", table="订单-明细")
    field = FieldSpec(
        id="field-1",
        model_id=model.id,
        name="客户名称",
        column="客户 名称",
    )

    assert model.schema_name == "业务 域"
    assert model.table == "订单-明细"
    assert field.column == "客户 名称"


def test_repeated_scan_creates_a_new_revision_for_the_same_semantic_content(schema_snapshot):
    modeller = RuleSemanticModeller()

    first = modeller.build(project_id="sales", snapshot=schema_snapshot)
    second = modeller.build(project_id="sales", snapshot=schema_snapshot)

    assert first.semantic_spec.id != second.semantic_spec.id
    assert first.semantic_spec.revision_id != second.semantic_spec.revision_id
    assert first.semantic_spec.spec_hash == second.semantic_spec.spec_hash


def test_unreviewed_suggestions_block_publish(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )

    with pytest.raises(Exception, match="unreviewed"):
        RevisionEditor().validate_for_publish(revision)


def test_publish_rejects_a_revision_without_an_explicit_dataset(sales_release):
    spec = sales_release.model_copy(update={"datasets": (), "terms": ()})
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:dataset-required",
        semantic_spec=spec,
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        RevisionEditor().validate_for_publish(revision)

    assert exc_info.value.code == "DATASET_REQUIRED"


def test_publish_rejects_a_dataset_without_queryable_elements(sales_release):
    empty_dataset = sales_release.datasets[0].model_copy(
        update={"metric_ids": (), "dimension_ids": ()}
    )
    spec = sales_release.model_copy(update={"datasets": (empty_dataset,)})
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:dataset-empty",
        semantic_spec=spec,
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        RevisionEditor().validate_for_publish(revision)

    assert exc_info.value.code == "DATASET_EMPTY"


def test_stale_schema_hash_blocks_suggestion_application(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )

    with pytest.raises(RevisionConflictError, match="schema snapshot changed"):
        RevisionEditor().apply_decisions(
            revision,
            expected_etag=revision.etag,
            expected_schema_snapshot_hash="sha256:stale",
            decisions=(),
        )


def test_duplicate_decisions_are_rejected(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    decision = SuggestionDecision(
        suggestion_id=revision.suggestions[0].id,
        accept=False,
    )

    with pytest.raises(RevisionConflictError, match="duplicate"):
        RevisionEditor().apply_decisions(
            revision,
            expected_etag=revision.etag,
            expected_schema_snapshot_hash=schema_snapshot.content_hash,
            decisions=(decision, decision),
        )


def test_published_revision_is_immutable(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = (
        RevisionEditor()
        .create(
            project_id="sales",
            schema_snapshot_hash=schema_snapshot.content_hash,
            semantic_spec=result.semantic_spec,
            suggestions=result.suggestions,
        )
        .model_copy(update={"state": RevisionState.PUBLISHED})
    )

    with pytest.raises(RevisionConflictError, match="published revisions are immutable"):
        RevisionEditor().apply_decisions(
            revision,
            expected_etag=revision.etag,
            expected_schema_snapshot_hash=schema_snapshot.content_hash,
            decisions=(),
        )


def test_derived_metric_dependencies_must_stay_on_the_declared_fact_model(sales_release):
    metrics = tuple(
        metric.model_copy(update={"model_id": "customers"})
        if metric.kind is MetricKind.DERIVED
        else metric
        for metric in sales_release.metrics
    )
    candidate = sales_release.model_copy(update={"metrics": metrics})

    with pytest.raises(SemanticValidationError, match="dependency belongs to another model"):
        SemanticRelease.model_validate(candidate.model_dump(mode="python"))


def test_metric_fixed_filter_must_stay_on_the_metric_fact_model(sales_release):
    customer_field = next(
        field for field in sales_release.fields if field.id == "customers.segment"
    )
    metrics = tuple(
        metric.model_copy(
            update={
                "filters": (
                    FixedFilter(
                        field_id=customer_field.id,
                        operator=FilterOperator.EQ,
                        value="重点客户",
                    ),
                )
            }
        )
        if metric.id == "net_revenue"
        else metric
        for metric in sales_release.metrics
    )
    candidate = sales_release.model_copy(update={"metrics": metrics})

    with pytest.raises(SemanticValidationError, match="fixed filter belongs to another model"):
        SemanticRelease.model_validate(candidate.model_dump(mode="python"))


@pytest.mark.parametrize("aggregation", [Aggregation.SUM, Aggregation.AVG])
def test_publish_rejects_numeric_aggregation_on_text_field(sales_release, aggregation):
    fields = tuple(
        field.model_copy(update={"data_type": "character varying"})
        if field.id == "orders.net_amount"
        else field
        for field in sales_release.fields
    )
    metrics = tuple(
        metric.model_copy(update={"aggregation": aggregation})
        if metric.id == "net_revenue"
        else metric
        for metric in sales_release.metrics
    )
    spec = sales_release.model_copy(update={"fields": fields, "metrics": metrics})
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:type-contract",
        semantic_spec=spec,
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        RevisionEditor().validate_for_publish(revision)

    assert exc_info.value.code == "INVALID_METRIC_AGGREGATION_TYPE"


def test_publish_allows_count_metric_on_text_field(sales_release):
    region_field = next(item for item in sales_release.fields if item.id == "orders.region")
    region_count = MetricSpec(
        id="region_non_null_count",
        name="有区域订单数",
        model_id=region_field.model_id,
        field_id=region_field.id,
        aggregation=Aggregation.COUNT,
    )
    dataset = sales_release.datasets[0].model_copy(
        update={"metric_ids": (*sales_release.datasets[0].metric_ids, region_count.id)}
    )
    spec = sales_release.model_copy(
        update={"metrics": (*sales_release.metrics, region_count), "datasets": (dataset,)}
    )
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:type-contract",
        semantic_spec=spec,
    )

    validated = RevisionEditor().validate_for_publish(revision)

    assert validated.state is RevisionState.VALIDATED


@pytest.mark.parametrize("aggregation", [Aggregation.SUM, Aggregation.AVG])
def test_publish_allows_postgresql_interval_aggregation(sales_release, aggregation):
    fields = tuple(
        field.model_copy(update={"data_type": "interval"})
        if field.id == "orders.net_amount"
        else field
        for field in sales_release.fields
    )
    metrics = tuple(
        metric.model_copy(update={"aggregation": aggregation})
        if metric.id == "net_revenue"
        else metric
        for metric in sales_release.metrics
    )
    spec = sales_release.model_copy(update={"fields": fields, "metrics": metrics})
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:type-contract",
        semantic_spec=spec,
    )

    validated = RevisionEditor().validate_for_publish(revision)

    assert validated.state is RevisionState.VALIDATED


def test_publish_rejects_incompatible_relation_field_types(sales_release):
    relation = next(item for item in sales_release.relations if item.id == "orders_customer")
    incompatible = relation.model_copy(
        update={
            "conditions": (
                relation.conditions[0].model_copy(update={"right_field_id": "customers.segment"}),
            )
        }
    )
    fields = tuple(
        field.model_copy(update={"data_type": "bigint"})
        if field.id == "orders.customer_id"
        else field
        for field in sales_release.fields
    )
    spec = sales_release.model_copy(
        update={
            "fields": fields,
            "relations": tuple(
                incompatible if item.id == incompatible.id else item
                for item in sales_release.relations
            ),
        }
    )
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:join-type-contract",
        semantic_spec=spec,
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        RevisionEditor().validate_for_publish(revision)

    assert exc_info.value.code == "INVALID_RELATION_FIELD_TYPES"


def test_publish_allows_compatible_cross_width_numeric_join(sales_release):
    fields = tuple(
        field.model_copy(
            update={
                "data_type": ("bigint" if field.id == "orders.customer_id" else "numeric(18, 0)")
            }
        )
        if field.id in {"orders.customer_id", "customers.id"}
        else field
        for field in sales_release.fields
    )
    spec = sales_release.model_copy(update={"fields": fields})
    revision = RevisionEditor().create(
        project_id=spec.project_id,
        schema_snapshot_hash="schema:join-type-contract",
        semantic_spec=spec,
    )

    validated = RevisionEditor().validate_for_publish(revision)

    assert validated.state is RevisionState.VALIDATED
