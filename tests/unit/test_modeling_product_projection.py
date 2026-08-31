from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    FieldKind,
    FieldSpec,
    MetricSpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    ModelingRunSource,
    ModelingSuggestionRun,
    SuggestionPatch,
    SuggestionSource,
)
from knowflow_analytics.modeling.product import (
    DecisionChoice,
    ModelingPlanBuilder,
    ModelingPlanPhase,
    ScopeInference,
    ScopeRecommendationBuilder,
    ScopeRecommendationGroup,
)
from knowflow_analytics.modeling.revision import RevisionConflictError
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller


def _revision(schema_snapshot, *, suggestions=()) -> ModelingRevision:
    semantic_spec = SemanticRelease(
        id="rev_sales",
        project_id="sales",
        revision_id="rev_sales",
        spec_hash="fixture",
        models=(),
        fields=(),
        datasets=(),
    )
    return ModelingRevision(
        id="rev_sales",
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        etag=7,
        semantic_spec=semantic_spec,
        suggestions=tuple(suggestions),
    )


def test_scope_recommendations_use_only_fk_connected_components(schema_snapshot):
    result = ScopeRecommendationBuilder().build(
        project_id="sales",
        datasource_id="default",
        schema_name="sales",
        tables=schema_snapshot.tables,
    )

    assert result.total_table_count == 2
    assert len(result.groups) == 1
    assert result.groups[0].inference == "database_constraint"
    assert result.groups[0].tables == ("customers", "orders")
    assert result.groups[0].foreign_key_count == 1


def test_scope_recommendations_never_invent_links_between_isolated_tables(
    schema_snapshot,
):
    disconnected = schema_snapshot.model_copy(
        update={
            "tables": tuple(
                table.model_copy(update={"foreign_keys": ()}) for table in schema_snapshot.tables
            )
        }
    )
    result = ScopeRecommendationBuilder().build(
        project_id="sales",
        datasource_id="default",
        schema_name="sales",
        tables=disconnected.tables,
    )

    assert [group.tables for group in result.groups] == [
        ("customers",),
        ("orders",),
    ]
    assert all(group.inference == "schema_only" for group in result.groups)
    assert all(group.foreign_key_count == 0 for group in result.groups)


def test_scope_recommendation_titles_are_bounded_for_long_database_comments(
    schema_snapshot,
):
    verbose = schema_snapshot.model_copy(
        update={
            "tables": tuple(
                table.model_copy(update={"comment": "业务注释" * 200})
                for table in schema_snapshot.tables
            )
        }
    )

    result = ScopeRecommendationBuilder().build(
        project_id="sales",
        datasource_id="default",
        schema_name="sales",
        tables=verbose.tables,
    )

    assert all(len(group.title) <= 256 for group in result.groups)


def test_scope_contract_can_report_a_connected_group_larger_than_draft_limit():
    group = ScopeRecommendationGroup(
        id="scope_large",
        title="大型关联域",
        schema_name="public",
        tables=tuple(f"table_{index}" for index in range(101)),
        inference=ScopeInference.DATABASE_CONSTRAINT,
        foreign_key_count=100,
    )

    assert len(group.tables) == 101


def test_dataset_plan_preserves_model_scoped_duplicate_business_names(schema_snapshot):
    models = (
        ModelSpec(id="model:a", name="年度经营", schema_name="sales", table="annual"),
        ModelSpec(id="model:b", name="每日经营", schema_name="sales", table="daily"),
    )
    fields = (
        FieldSpec(
            id="field:a:period",
            model_id="model:a",
            name="期间",
            column="period_a",
            data_type="integer",
            kind=FieldKind.MEASURE,
            default_aggregation=Aggregation.SUM,
            create_metric=True,
        ),
        FieldSpec(
            id="field:b:period",
            model_id="model:b",
            name="期间",
            column="period_b",
            data_type="integer",
            kind=FieldKind.MEASURE,
            default_aggregation=Aggregation.SUM,
            create_metric=True,
        ),
    )
    metrics = (
        MetricSpec(
            id="metric:a:period",
            name="期间",
            model_id="model:a",
            field_id="field:a:period",
            aggregation=Aggregation.SUM,
        ),
        MetricSpec(
            id="metric:b:period",
            name="期间",
            model_id="model:b",
            field_id="field:b:period",
            aggregation=Aggregation.SUM,
        ),
    )
    semantic_spec = SemanticRelease(
        id="rev_duplicate_names",
        project_id="sales",
        revision_id="rev_duplicate_names",
        spec_hash="fixture",
        models=models,
        fields=fields,
        metrics=metrics,
        datasets=(),
    )
    revision = ModelingRevision(
        id="rev_duplicate_names",
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        etag=3,
        semantic_spec=semantic_spec,
    )

    plan = ModelingPlanBuilder().build(revision=revision, project_name="经营分析")

    assert plan.contract_version == "product-plan-v3"
    assert plan.phase is ModelingPlanPhase.REVIEWING_DATASET
    assert plan.queue.summary.blocking == 0
    assert len(plan.queue.decisions) == 1
    decision = plan.queue.decisions[0]
    assert decision.kind == "dataset_scope"
    assert decision.risk_level == "execution"
    proposed = DatasetSpec.model_validate(decision.proposed_resource)
    assert proposed.metric_ids == ("metric:a:period", "metric:b:period")
    assert {item.id for item in decision.options} == {"accept", "reject"}


def test_a_fact_table_amount_gets_sum_recommended_by_the_rules(schema_snapshot):
    """此前规则对数值列一刀切 0.65，"不知道所以不推荐"。现在规则知道表角色：
    orders 有外键指出去且有数值非键列 → 事实表 → 其中的金额列 SUM 0.8，够得上推荐。
    编码/年份/状态类数值列走另一条路（分类维度，无聚合），见 test_classify。"""

    baseline = RuleSemanticModeller().build(
        project_id="sales",
        snapshot=schema_snapshot,
        create_default_dataset=False,
    )
    revision = ModelingRevision(
        id=baseline.semantic_spec.id,
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        etag=1,
        semantic_spec=baseline.semantic_spec,
        suggestions=baseline.suggestions,
    )
    numeric_candidate = next(
        item
        for item in baseline.suggestions
        if item.source is SuggestionSource.RULE and item.changes.get("kind") == "measure"
    )

    plan = ModelingPlanBuilder().build(revision=revision, project_name="销售分析")
    decision = next(
        item for item in plan.queue.decisions if numeric_candidate.id in item.source_suggestion_ids
    )

    assert decision.kind == "measure_aggregation"
    recommended = [option.id for option in decision.options if option.recommended]
    assert recommended == ["aggregation:sum"]


def test_decision_queue_covers_each_ai_patch_once_and_keeps_constraints_informational(
    schema_snapshot,
):
    constraint = SuggestionPatch(
        id="constraint:customer-id",
        target_kind="field",
        target_id="field:customers:id",
        changes={"kind": "identifier", "identifier_type": "primary"},
        source=SuggestionSource.DATABASE_CONSTRAINT,
        confidence=1.0,
        reason="数据库主键约束",
        high_impact=True,
    )
    ai_name = SuggestionPatch(
        id="ai:model-name",
        target_kind="model",
        target_id="model:customers",
        changes={"name": "客户"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.8,
        reason="AI 业务名称建议",
    )
    ai_measure = SuggestionPatch(
        id="ai:amount",
        target_kind="field",
        target_id="field:orders:amount",
        changes={"kind": "measure", "aggregation": "sum"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.7,
        reason="AI 数值字段分类建议",
        high_impact=True,
    )
    revision = _revision(schema_snapshot, suggestions=(constraint,))
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        source=ModelingRunSource.UI,
        input_hash="sha256:input",
        suggestions=(ai_name, ai_measure),
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    assert plan.phase is ModelingPlanPhase.REVIEWING_SEMANTICS
    assert plan.queue.summary.informational == 1
    assert [item.source_suggestion_ids for item in plan.queue.information] == [(constraint.id,)]
    covered_ai_ids = [
        suggestion_id
        for decision in plan.queue.decisions
        for suggestion_id in decision.source_suggestion_ids
        if suggestion_id.startswith("ai:")
    ]
    assert sorted(covered_ai_ids) == [ai_measure.id, ai_name.id]
    assert len(covered_ai_ids) == len(set(covered_ai_ids))
    measure_decision = next(
        item for item in plan.queue.decisions if item.id.endswith(ai_measure.id)
    )
    assert measure_decision.risk_level == "execution"


def test_low_risk_names_are_batched_without_losing_explicit_ai_coverage(
    schema_snapshot,
):
    revision = _revision(schema_snapshot)
    suggestions = tuple(
        SuggestionPatch(
            id=f"ai:model-name:{index}",
            target_kind="model",
            target_id=f"model:{index}",
            changes={"name": name, "description": f"{name}说明"},
            source=SuggestionSource.AI_SCHEMA,
            confidence=0.8,
            reason="AI 业务名称建议",
        )
        for index, name in enumerate(("客户", "订单"), start=1)
    )
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        input_hash="sha256:input",
        suggestions=suggestions,
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    assert len(plan.queue.decisions) == 1
    assert plan.queue.decisions[0].kind == "semantic_name_batch"
    assert set(plan.queue.decisions[0].source_suggestion_ids) == {item.id for item in suggestions}


def test_execution_field_classifications_are_reviewed_individually(
    schema_snapshot,
):
    baseline = RuleSemanticModeller().build(
        project_id="sales",
        snapshot=schema_snapshot,
        create_default_dataset=False,
    )
    revision = ModelingRevision(
        id=baseline.semantic_spec.id,
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        etag=1,
        semantic_spec=baseline.semantic_spec,
        suggestions=baseline.suggestions,
    )

    plan = ModelingPlanBuilder().build(revision=revision, project_name="销售分析")

    field_decisions = [item for item in plan.queue.decisions if item.kind == "field_classification"]
    assert all(len(item.source_suggestion_ids) == 1 for item in field_decisions)
    expected = {
        item.id
        for item in baseline.suggestions
        if item.target_kind == "field"
        and item.source is not SuggestionSource.DATABASE_CONSTRAINT
        and item.changes.get("kind") != "measure"
    }
    reviewed = {
        suggestion_id for item in field_decisions for suggestion_id in item.source_suggestion_ids
    }
    assert expected.issubset(reviewed)
    relation_suggestion = next(
        item for item in baseline.suggestions if item.target_kind == "relation"
    )
    assert any(
        relation_suggestion.id in item.source_suggestion_ids
        and item.kind == "relation_cardinality"
        and item.risk_level == "execution"
        for item in plan.queue.decisions
    )
    relation_decision = next(
        item
        for item in plan.queue.decisions
        if relation_suggestion.id in item.source_suggestion_ids
    )
    assert {item.id for item in relation_decision.options} == {
        "cardinality:many_to_one",
        "cardinality:one_to_one",
        "cardinality:one_to_many",
        "exclude",
    }
    assert all(
        relation_suggestion.id not in item.source_suggestion_ids for item in plan.queue.information
    )


def test_conflicting_suggestions_require_one_explicit_order_independent_choice(
    schema_snapshot,
):
    revision = _revision(schema_snapshot)
    suggestions = (
        SuggestionPatch(
            id="suggestion:a",
            target_kind="field",
            target_id="field:orders:created_at",
            changes={"kind": "time", "dimension_type": "time"},
            source=SuggestionSource.RULE,
            confidence=0.8,
            reason="规则建议",
        ),
        SuggestionPatch(
            id="suggestion:b",
            target_kind="field",
            target_id="field:orders:created_at",
            changes={"kind": "dimension", "dimension_type": "categorical"},
            source=SuggestionSource.AI_SCHEMA,
            confidence=0.8,
            reason="AI 建议",
        ),
    )

    def build(items):
        run = ModelingSuggestionRun(
            id="run_sales",
            project_id="sales",
            revision_id=revision.id,
            revision_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            input_hash="sha256:input",
            suggestions=items,
            created_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        return ModelingPlanBuilder().build(
            revision=revision,
            project_name="销售分析",
            suggestion_run=run,
        )

    decision = build(suggestions).queue.decisions[0]
    reversed_decision = build(tuple(reversed(suggestions))).queue.decisions[0]

    assert decision.kind == "suggestion_conflict"
    assert decision.risk_level == "blocking"
    assert decision.source_suggestion_ids == ("suggestion:a", "suggestion:b")
    assert decision.option_accepts_suggestion_ids == {
        "proposal:1": ("suggestion:a",),
        "proposal:2": ("suggestion:b",),
        "reject_all": (),
    }
    assert decision.model_dump() == reversed_decision.model_dump()


def test_plan_rejects_choices_after_revision_version_changes(schema_snapshot):
    suggestion = SuggestionPatch(
        id="ai:model-name",
        target_kind="model",
        target_id="model:customers",
        changes={"name": "客户"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.8,
        reason="AI 业务名称建议",
    )
    revision = _revision(schema_snapshot)
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        input_hash="sha256:input",
        suggestions=(suggestion,),
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    with pytest.raises(RevisionConflictError, match="stale"):
        ModelingPlanBuilder.validate_choices(
            plan=plan,
            revision=revision.model_copy(update={"etag": revision.etag + 1}),
            choices=(DecisionChoice(decision_id=plan.queue.decisions[0].id, option_id="accept"),),
        )


def test_plan_requires_one_server_defined_choice_for_every_decision(schema_snapshot):
    suggestion = SuggestionPatch(
        id="ai:model-name",
        target_kind="model",
        target_id="model:customers",
        changes={"name": "客户"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.8,
        reason="AI 业务名称建议",
    )
    revision = _revision(schema_snapshot)
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        input_hash="sha256:input",
        suggestions=(suggestion,),
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    with pytest.raises(RevisionConflictError, match="every modeling decision"):
        ModelingPlanBuilder.validate_choices(plan=plan, revision=revision, choices=())

    with pytest.raises(RevisionConflictError, match="unknown decision option"):
        ModelingPlanBuilder.validate_choices(
            plan=plan,
            revision=revision,
            choices=(
                DecisionChoice(
                    decision_id=plan.queue.decisions[0].id,
                    option_id="silently_accept",
                ),
            ),
        )


def test_blocking_decision_is_also_counted_as_needing_confirmation(schema_snapshot):
    revision = _revision(schema_snapshot)

    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
    )

    assert plan.phase is ModelingPlanPhase.BLOCKED
    assert plan.queue.summary.blocking == 1
    assert plan.queue.summary.needs_confirmation == 1


def test_decision_preview_is_bounded_when_a_suggestion_contains_long_metadata(
    schema_snapshot,
):
    revision = _revision(schema_snapshot)
    suggestion = SuggestionPatch(
        id="ai:long-field-description",
        target_kind="field",
        target_id="field:unknown",
        changes={"description": "说明" * 2_000},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.8,
        reason="AI 字段说明建议",
    )
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        input_hash="sha256:input",
        suggestions=(suggestion,),
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    assert len(plan.queue.decisions[0].proposal) == 2_000


def test_measure_decision_exposes_every_supported_aggregation(
    schema_snapshot,
):
    revision = _revision(schema_snapshot)
    suggestion = SuggestionPatch(
        id="ai:distinct-customers",
        target_kind="field",
        target_id="field:unknown",
        changes={"kind": "measure", "aggregation": "count_distinct"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.8,
        reason="AI 聚合建议",
        high_impact=True,
    )
    run = ModelingSuggestionRun(
        id="run_sales",
        project_id="sales",
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        input_hash="sha256:input",
        suggestions=(suggestion,),
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    )

    plan = ModelingPlanBuilder().build(
        revision=revision,
        project_name="销售分析",
        suggestion_run=run,
    )

    options = plan.queue.decisions[0].options
    assert {item.id for item in options} == {
        "aggregation:sum",
        "aggregation:count",
        "aggregation:count_distinct",
        "aggregation:avg",
        "aggregation:min",
        "aggregation:max",
        "exclude",
    }
    assert next(item for item in options if item.id == "aggregation:count_distinct").recommended
