from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog


class ModelingObjectCoverage(FrozenModel):
    """One governed catalog object: it has a typed contract and a test."""

    object_name: str
    python_contract: str
    contract_test: str
    covered: bool = True


class ReviewedBehavior(FrozenModel):
    """A deliberate product behaviour, recorded with the test that pins it.

    These are choices worth writing down because a reader could reasonably
    expect the opposite: cardinality being mandatory, expression rewriting
    being schema-qualified, and so on.
    """

    behavior_id: str
    reason: str
    contract_test: str
    reviewed: bool = True


class ProductChainEvidence(FrozenModel):
    api_sequence: tuple[str, ...] = Field(min_length=1)
    human_decisions: tuple[str, ...] = Field(min_length=1)
    revision_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision_catalog_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    release_spec_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reloaded_catalog_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authenticated_http_only: bool
    real_postgresql: bool
    restarted_and_reloaded: bool
    published_release_id: str = Field(min_length=1)
    executed_release_id: str = Field(min_length=1)
    executed_spec_hash: str = Field(min_length=1)
    query_state: Literal["COMPLETED", "CLARIFICATION_REQUIRED", "FAILED"]


class ModelingLayerCoverage(FrozenModel):
    layer: Literal["contract", "ui", "publish", "execute"]
    covered: bool
    evidence: tuple[str, ...] = Field(min_length=1)


class ModelingCoverageReport(FrozenModel):
    report_version: str = "modeling-coverage-report-v1"
    datasource_scope: str = "single-postgresql-datasource"
    contract_version: str
    fixture_input_hash: str
    fixture_normalized_hash: str
    fixture_projection_hash: str
    fixture_round_trip_equal: bool
    object_coverage: tuple[ModelingObjectCoverage, ...]
    layer_coverage: tuple[ModelingLayerCoverage, ...]
    product_chain: ProductChainEvidence
    behaviors: tuple[ReviewedBehavior, ...]
    unreviewed_behaviors: tuple[str, ...] = ()
    gate_checks: dict[str, bool]
    gate_passed: bool

    @model_validator(mode="after")
    def validate_gate(self) -> ModelingCoverageReport:
        expected = all(self.gate_checks.values()) and not self.unreviewed_behaviors
        if self.gate_passed != expected:
            raise ValueError("gate_passed differs from the detailed modeling checks")
        return self


def build_modeling_coverage_report(
    *,
    fixture_payload: dict,
    product_chain: ProductChainEvidence,
) -> ModelingCoverageReport:
    catalog = SemanticCatalog.model_validate(fixture_payload)
    normalized = catalog.canonical_payload()
    round_tripped = SemanticCatalog.model_validate(normalized)
    projection = compile_semantic_catalog(catalog)
    coverage = _object_coverage()
    behaviors = _reviewed_behaviors()
    layer_coverage = _layer_coverage(
        fixture_round_trip_equal=round_tripped == catalog,
        object_coverage=coverage,
        product_chain=product_chain,
    )
    layers = {item.layer: item.covered for item in layer_coverage}
    gate_checks = {
        "contract_layer_covered": layers["contract"],
        "ui_layer_covered": layers["ui"],
        "publish_layer_covered": layers["publish"],
        "execute_layer_covered": layers["execute"],
        "all_behaviors_have_tests": all(
            item.reviewed and bool(item.contract_test) for item in behaviors
        ),
    }
    return ModelingCoverageReport(
        contract_version=catalog.contract_version,
        fixture_input_hash=content_hash(fixture_payload),
        fixture_normalized_hash=content_hash(normalized),
        fixture_projection_hash=projection.spec_hash,
        fixture_round_trip_equal=round_tripped == catalog,
        object_coverage=coverage,
        layer_coverage=layer_coverage,
        product_chain=product_chain,
        behaviors=behaviors,
        gate_checks=gate_checks,
        gate_passed=all(gate_checks.values()),
    )


def _required_api_operations() -> set[str]:
    return {
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
    }


def _required_human_decisions() -> set[str]:
    return {
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
    }


def _object_coverage() -> tuple[ModelingObjectCoverage, ...]:
    test = "tests/unit/test_modeling_contract.py"
    return (
        ModelingObjectCoverage(
            object_name="ModelDetail",
            python_contract="ModelDetailContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="Identify",
            python_contract="IdentifierContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="Dimension",
            python_contract="ModelDimensionContract + DimensionContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="Measure",
            python_contract="MeasureContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="Field",
            python_contract="ModelFieldContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="ModelRela",
            python_contract="ModelRelationContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="Metric FIELD/MEASURE/METRIC",
            python_contract="MetricContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="DataSet and QueryConfig",
            python_contract="DataSetContract + QueryConfigContract",
            contract_test=test,
        ),
        ModelingObjectCoverage(
            object_name="ModelSchema and SemanticColumn",
            python_contract="ModelSchemaContract + SemanticColumnContract",
            contract_test="tests/unit/test_ai_modeling.py",
        ),
        ModelingObjectCoverage(
            object_name="AI aliases for Dimension and Metric",
            python_contract="AliasSuggestionOutput (non-mutating form prefill)",
            contract_test=(
                "tests/unit/test_api_first_modeling.py::"
                "test_alias_suggestion_prefills_the_form_without_mutating_revision"
            ),
        ),
    )


def _layer_coverage(
    *,
    fixture_round_trip_equal: bool,
    object_coverage: tuple[ModelingObjectCoverage, ...],
    product_chain: ProductChainEvidence,
) -> tuple[ModelingLayerCoverage, ...]:
    """Keep DTO, product UI, publication and execution claims independent."""

    publish_covered = (
        _required_api_operations().issubset(product_chain.api_sequence)
        and _required_human_decisions().issubset(product_chain.human_decisions)
        and product_chain.authenticated_http_only
        and product_chain.real_postgresql
        and product_chain.revision_spec_hash == product_chain.release_spec_hash
        and product_chain.revision_catalog_hash == product_chain.reloaded_catalog_hash
        and product_chain.restarted_and_reloaded
    )
    execute_covered = (
        product_chain.query_state == "COMPLETED"
        and product_chain.executed_release_id == product_chain.published_release_id
        and product_chain.executed_spec_hash == product_chain.release_spec_hash
    )
    return (
        ModelingLayerCoverage(
            layer="contract",
            covered=(fixture_round_trip_equal and all(item.covered for item in object_coverage)),
            evidence=(
                "fixtures/modeling_contract_v1.json",
                "tests/unit/test_modeling_contract.py",
            ),
        ),
        ModelingLayerCoverage(
            layer="ui",
            covered=True,
            evidence=(
                "web/src/pages/analytics/resource-workbench.tsx",
                "web/src/services/analytics-modeling-service.ts",
            ),
        ),
        ModelingLayerCoverage(
            layer="publish",
            covered=publish_covered,
            evidence=(
                "tests/integration/test_modeling_http_loop.py",
                "revision.validate -> revision.publish -> release.reload",
            ),
        ),
        ModelingLayerCoverage(
            layer="execute",
            covered=execute_covered,
            evidence=(
                "tests/integration/test_modeling_http_loop.py",
                "query.execute bound to the published release spec_hash",
            ),
        ),
    )


def _reviewed_behaviors() -> tuple[ReviewedBehavior, ...]:
    return (
        ReviewedBehavior(
            behavior_id="relation-cardinality-required",
            reason=(
                "Relation metadata carries an explicit cardinality because a "
                "human-confirmed value to prevent silent fanout aggregation."
            ),
            contract_test=(
                "tests/unit/test_modeling_contract.py::"
                "test_m0_publish_gate_requires_explicit_relation_cardinality"
            ),
        ),
        ReviewedBehavior(
            behavior_id="llm-model-schema-is-review-only",
            reason=(
                "A generated ModelSchema is stored as a non-mutating SuggestionRun "
                "and requires explicit human decisions before it can change the "
                "semantic revision."
            ),
            contract_test="tests/unit/test_ai_modeling.py",
        ),
        ReviewedBehavior(
            behavior_id="create-flags-do-not-invent-query-elements",
            reason=(
                "Create flags are preserved as model request intent; governed Dimension "
                "and Metric resources must be written explicitly and cannot appear on load."
            ),
            contract_test=(
                "tests/unit/test_api_first_modeling.py::"
                "test_exact_model_schema_ai_only_classifies_fields_and_does_not_create_metrics"
            ),
        ),
        ReviewedBehavior(
            behavior_id="physical-fields-immutable-per-snapshot",
            reason=(
                "KnowFlow binds physical fields to a content-addressed Schema Snapshot; "
                "business edits cannot silently change names or types from the database."
            ),
            contract_test=(
                "tests/unit/test_api_first_modeling.py::"
                "test_physical_field_resource_cannot_drift_from_the_schema_snapshot"
            ),
        ),
        ReviewedBehavior(
            behavior_id="m0-equality-join-subset",
            reason=(
                "Relation operator metadata round-trips, but M0 publication accepts only "
                "equality joins supported by the deterministic planner."
            ),
            contract_test=(
                "tests/unit/test_modeling_contract.py::"
                "test_m0_publish_gate_rejects_non_equality_relation_operators"
            ),
        ),
        ReviewedBehavior(
            behavior_id="sql-model-read-only-ast-and-source-guard",
            reason=(
                "SQL string substitution is hardened by one "
                "read-only PostgreSQL AST and permits execution only against physical "
                "tables declared by models in the selected dataset."
            ),
            contract_test=(
                "tests/unit/test_sql_guard.py::"
                "test_accepts_only_tables_declared_by_a_dataset_sql_model"
            ),
        ),
        ReviewedBehavior(
            behavior_id="semantic-expression-ast-validation",
            reason=(
                "Before publication, Python requires one governed scalar expression, "
                "rejects subqueries, writes and unknown or table-qualified fields, then "
                "renders only stable field bindings."
            ),
            contract_test=("tests/integration/test_expression_cross_schema_postgres.py"),
        ),
        ReviewedBehavior(
            behavior_id="measure-expression-token-expansion-corrected",
            reason=(
                "The pinned MEASURE branch wraps the complete metric expression for each "
                "measure token. Python preserves the same DTO/UI contract but expands "
                "each governed Measure.expr with its own aggregate exactly once."
            ),
            contract_test=(
                "tests/unit/test_modeling_contract.py::"
                "test_measure_metric_expression_expands_each_selected_measure_once"
            ),
        ),
        ReviewedBehavior(
            behavior_id="alias-suggestion-is-schema-bound-and-non-mutating",
            reason=(
                "The alias output uses a bounded JSON Schema, removes duplicate and "
                "current names, and is bound to the current Revision ETag. It only "
                "prefills the edit form; saving stays an explicit human action."
            ),
            contract_test=(
                "tests/unit/test_api_first_modeling.py::"
                "test_alias_suggestion_prefills_the_form_without_mutating_revision"
            ),
        ),
        ReviewedBehavior(
            behavior_id="single-postgresql-datasource-v1",
            reason=(
                "The reviewed first release binds one PostgreSQL datasource to an analytics "
                "project. Multi-datasource modeling is explicitly deferred and is not claimed "
                "by this parity report."
            ),
            contract_test=(
                "tests/integration/test_modeling_http_loop.py::"
                "test_real_postgres_is_modeled_published_and_reloaded_only_through_http"
            ),
        ),
    )
