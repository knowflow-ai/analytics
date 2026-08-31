from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_m2_modeling import _application_with_catalog

from knowflow_analytics.contracts import (
    QueryRuleMode,
    QueryRuleSpec,
    QueryRuleType,
    SemanticContextEntry,
    TermSpec,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.ai_artifacts import reconcile_query_scopes
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import QueryRuleContract, SemanticCatalog

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _manifest(catalog: SemanticCatalog) -> tuple:
    release = compile_semantic_catalog(catalog)
    return (
        tuple(
            sorted(
                (
                    item.id,
                    item.model_ids,
                    item.metric_ids,
                    item.dimension_ids,
                )
                for item in release.datasets
            )
        ),
        tuple(
            sorted(
                (
                    item.dataset_id,
                    item.root_model_id,
                    item.default_count_metric_id,
                    tuple((path.target_model_id, path.relation_ids) for path in item.paths),
                )
                for item in release.analysis_topic_routes
            )
        ),
    )


def _rule(
    rule_id: str,
    *,
    dataset_id: str,
    parameter: str,
    output: str,
) -> QueryRuleContract:
    return QueryRuleContract(
        id=rule_id,
        dataset_id=dataset_id,
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=(parameter,),
        outputs=(output,),
    )


def test_query_scope_reconciliation_is_idempotent_and_covers_every_metric() -> None:
    reconciled = reconcile_query_scopes(_catalog())
    repeated = reconcile_query_scopes(reconciled)
    release = compile_semantic_catalog(reconciled)

    assert _manifest(repeated) == _manifest(reconciled)
    exposed_metric_ids = {
        metric_id for dataset in release.datasets for metric_id in dataset.metric_ids
    }
    assert {item.id for item in release.metrics}.issubset(exposed_metric_ids)
    assert {item.dataset_id for item in release.analysis_topic_routes} == {
        item.id for item in release.datasets
    }


def test_query_scope_reconciliation_is_idempotent_with_valid_query_rules() -> None:
    catalog = reconcile_query_scopes(_catalog())
    rule = _rule(
        "rule-orders-root-dimensions",
        dataset_id="dataset_sales",
        parameter="dimension_channel",
        output="dimension_order_time",
    )
    ruled = SemanticCatalog.model_validate(
        catalog.model_copy(update={"query_rules": (rule,)}).model_dump(mode="python")
    )

    reconciled = reconcile_query_scopes(ruled)
    repeated = reconcile_query_scopes(reconciled)

    assert reconciled.query_rules == (rule,)
    assert repeated.query_rules == (rule,)
    assert _manifest(repeated) == _manifest(reconciled)


def test_structural_count_compile_preserves_reviewed_query_scope_context() -> None:
    catalog = reconcile_query_scopes(_catalog())
    context = SemanticContextEntry(
        id="context-orders-scope",
        target_type="query_scope",
        target_id="dataset_sales",
        kind="definition",
        text="销售事实范围只含已完成订单",
        source_type="human_convention",
    )
    contextual = SemanticCatalog.model_validate(
        catalog.model_copy(update={"semantic_context": (context,)}).model_dump(mode="python")
    )

    reconciled = reconcile_query_scopes(contextual)

    assert reconciled.semantic_context == (context,)


def test_query_scope_reconciliation_recovers_rules_after_structural_compile() -> None:
    catalog = reconcile_query_scopes(_catalog())
    reviewed_orders_scope = next(item for item in catalog.data_sets if item.id == "dataset_sales")
    reviewed_orders_scope = reviewed_orders_scope.model_copy(
        update={
            "name": "已审核销售范围",
            "biz_name": "reviewed_sales_scope",
            "description": "人工审核过的销售事实范围",
            "alias": "销售口径,成交口径",
            "status": 7,
            "type_enum": "reviewed",
            "sensitive_level": 3,
            "domain_id": "domain-sales",
            "admins": ("alice",),
            "admin_orgs": ("finance",),
        }
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "data_sets": tuple(
                    reviewed_orders_scope if item.id == reviewed_orders_scope.id else item
                    for item in catalog.data_sets
                )
            }
        ).model_dump(mode="python")
    )
    customer_dataset_id = next(
        item.dataset_id
        for item in catalog.analysis_topic_routes
        if item.root_model_id == "model_customers"
    )
    order_rule = _rule(
        "rule-orders-root-dimensions",
        dataset_id="dataset_sales",
        parameter="dimension_channel",
        output="dimension_order_time",
    )
    customer_rule = _rule(
        "rule-customer-name",
        dataset_id=customer_dataset_id,
        parameter="dimension_customer_name",
        output="dimension_customer_name",
    )
    ruled = SemanticCatalog.model_validate(
        catalog.model_copy(update={"query_rules": (order_rule, customer_rule)}).model_dump(
            mode="python"
        )
    )
    # Keeping the old frozen route while removing its relation deliberately
    # enters reconcile_query_scopes' structural recovery path.
    relation_removed = ruled.model_copy(update={"model_relations": ()})

    recovered = reconcile_query_scopes(relation_removed)

    assert {item.id for item in recovered.data_sets} == {
        "dataset_sales",
        customer_dataset_id,
    }
    assert recovered.query_rules == (order_rule, customer_rule)
    assert (
        next(
            item for item in recovered.analysis_topic_routes if item.dataset_id == "dataset_sales"
        ).paths
        == ()
    )
    recovered_orders_scope = next(
        item for item in recovered.data_sets if item.id == "dataset_sales"
    )
    assert recovered_orders_scope.model_dump(
        mode="python", exclude={"data_set_detail"}
    ) == reviewed_orders_scope.model_dump(mode="python", exclude={"data_set_detail"})
    assert reconcile_query_scopes(recovered).query_rules == recovered.query_rules


def test_structural_reconciliation_removes_only_rules_for_a_retired_scope() -> None:
    catalog = reconcile_query_scopes(_catalog())
    customer_dataset_id = next(
        item.dataset_id
        for item in catalog.analysis_topic_routes
        if item.root_model_id == "model_customers"
    )
    order_rule = _rule(
        "rule-orders-root-dimensions",
        dataset_id="dataset_sales",
        parameter="dimension_channel",
        output="dimension_order_time",
    )
    customer_rule = _rule(
        "rule-customer-name",
        dataset_id=customer_dataset_id,
        parameter="dimension_customer_name",
        output="dimension_customer_name",
    )
    orders = next(item for item in catalog.models if item.id == "model_orders")
    orders_without_primary = orders.model_copy(
        update={
            "model_detail": orders.model_detail.model_copy(
                update={
                    "identifiers": tuple(
                        item
                        for item in orders.model_detail.identifiers
                        if item.type.value != "primary"
                    )
                }
            )
        }
    )
    retired = catalog.model_copy(
        update={
            "models": tuple(
                orders_without_primary if item.id == orders.id else item for item in catalog.models
            ),
            "metrics": tuple(item for item in catalog.metrics if item.model_id != orders.id),
            "query_rules": (order_rule, customer_rule),
        }
    )

    reconciled = reconcile_query_scopes(retired)

    assert {item.dataset_id for item in reconciled.analysis_topic_routes} == {customer_dataset_id}
    assert reconciled.query_rules == (customer_rule,)
    assert compile_semantic_catalog(reconciled).query_rules == (
        QueryRuleSpec.model_validate(customer_rule.model_dump(mode="python")),
    )


def test_query_scope_reconciliation_matches_a_clean_recompile_after_root_retirement() -> None:
    reconciled = reconcile_query_scopes(_catalog())
    orders = next(item for item in reconciled.models if item.id == "model_orders")
    detail = orders.model_detail.model_copy(
        update={
            "identifiers": tuple(
                item for item in orders.model_detail.identifiers if item.type.value != "primary"
            )
        }
    )
    retired = reconciled.model_copy(
        update={
            "models": tuple(
                orders.model_copy(update={"model_detail": detail}) if item.id == orders.id else item
                for item in reconciled.models
            ),
            "metrics": tuple(
                item for item in reconciled.metrics if item.model_id != "model_orders"
            ),
        }
    )

    incrementally_reconciled = reconcile_query_scopes(retired)
    clean_input = retired.model_copy(
        update={
            "data_sets": (),
            "analysis_topic_routes": (),
            "query_rules": (),
        }
    )
    clean_recompile = reconcile_query_scopes(
        SemanticCatalog.model_validate(clean_input.model_dump(mode="python"))
    )

    assert _manifest(incrementally_reconciled) == _manifest(clean_recompile)
    roots = {item.root_model_id for item in incrementally_reconciled.analysis_topic_routes}
    assert "model_orders" not in roots


def test_scope_sensitive_atomic_catalog_write_reconciles_query_scopes() -> None:
    catalog = reconcile_query_scopes(_catalog())
    application = _application_with_catalog(catalog)
    revision = application.get_revision(catalog.revision_id)
    template = catalog.dimensions[0]
    amount_dimension = template.model_copy(
        update={
            "id": "dimension_amount_band",
            "name": "金额分段",
            "biz_name": "amount_band",
            "description": "订单金额的业务分段",
            "model_id": "model_orders",
            "type": "categorical",
            "expr": "amount",
            "semantic_type": "CATEGORY",
            "alias": None,
            "data_type": "numeric",
            "type_params": None,
        }
    )

    updated = application.upsert_catalog_dimension(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        dimension=amount_dimension,
    )

    orders_scope = next(
        item for item in updated.semantic_spec.datasets if "metric_revenue" in item.metric_ids
    )
    assert "dimension_amount_band" in orders_scope.dimension_ids


def test_application_can_upsert_a_rule_for_a_generated_query_scope() -> None:
    catalog = reconcile_query_scopes(_catalog())
    application = _application_with_catalog(catalog)
    revision = application.get_revision(catalog.revision_id)
    rule = QueryRuleSpec(
        id="rule-orders-root-dimensions",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_order_time",),
    )

    updated = application.upsert_query_rule(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        query_rule=rule,
    )

    assert updated.semantic_catalog is not None
    assert updated.semantic_catalog.query_rules == (
        QueryRuleContract.model_validate(rule.model_dump(mode="python")),
    )
    assert updated.semantic_spec.query_rules == (rule,)


def test_reconciliation_removes_an_unrouted_dataset_from_a_compiler_owned_catalog() -> None:
    catalog = reconcile_query_scopes(_catalog())
    template = catalog.data_sets[0]
    orphan = template.model_copy(
        update={
            "id": "manual_orphan_scope",
            "name": "手工孤立范围",
            "biz_name": "manual_orphan_scope",
        }
    )
    orphan_rule = _rule(
        "rule-manual-orphan",
        dataset_id=orphan.id,
        parameter=orphan.data_set_detail.data_set_model_configs[0].dimensions[0],
        output=orphan.data_set_detail.data_set_model_configs[0].dimensions[0],
    )
    orphan_context = SemanticContextEntry(
        id="context-manual-orphan",
        target_type="query_scope",
        target_id=orphan.id,
        kind="scope",
        text="不应保留的手工查询范围",
        source_type="human_convention",
    )
    bound_term = TermSpec(
        id="term-orphan-dataset-with-valid-metric",
        name="订单业务量",
        dataset_ids=(orphan.id,),
        metric_ids=(catalog.metrics[0].id,),
    )
    mixed = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "data_sets": (*catalog.data_sets, orphan),
                "query_rules": (*catalog.query_rules, orphan_rule),
                "semantic_context": (*catalog.semantic_context, orphan_context),
                "terms": (*catalog.terms, bound_term),
            }
        ).model_dump(mode="python")
    )

    reconciled = reconcile_query_scopes(mixed)

    dataset_ids = {item.id for item in reconciled.data_sets}
    routed_ids = {item.dataset_id for item in reconciled.analysis_topic_routes}
    assert dataset_ids == routed_ids
    assert orphan.id not in dataset_ids
    assert orphan_rule.id not in {item.id for item in reconciled.query_rules}
    assert orphan_context.id not in {item.id for item in reconciled.semantic_context}
    reconciled_term = next(item for item in reconciled.terms if item.id == bound_term.id)
    assert reconciled_term.dataset_ids == ()
    assert reconciled_term.metric_ids == bound_term.metric_ids


def test_application_rejects_direct_query_scope_upsert() -> None:
    catalog = reconcile_query_scopes(_catalog())
    application = _application_with_catalog(catalog)
    revision = application.get_revision(catalog.revision_id)

    with pytest.raises(SemanticValidationError) as exc_info:
        application.upsert_catalog_dataset(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            data_set=catalog.data_sets[0],
        )

    assert exc_info.value.code == "DERIVED_QUERY_SCOPE_IMMUTABLE"
