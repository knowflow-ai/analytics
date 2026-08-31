from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.contracts import (
    QueryRuleMode,
    QueryRuleType,
    SemanticContextEntry,
    TermSpec,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.ai_artifacts import reconcile_query_scopes
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    QueryRuleContract,
    SemanticCatalog,
)
from knowflow_analytics.modeling.contracts import (
    ModelingRevision,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
    semantic_context_content_hash,
)
from knowflow_analytics.modeling.deletion import CatalogDeletionPlanner, ResourceKind
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.semantic.index import EmbeddingBatch

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _catalog_with_dimension_references() -> SemanticCatalog:
    """fixture 目录本身已经在数据集范围和维度值字典里引用了维度。"""

    return _catalog()


def _assert_scopes_are_fully_routed(catalog: SemanticCatalog) -> None:
    dataset_ids = {item.id for item in catalog.data_sets}
    route_dataset_ids = {item.dataset_id for item in catalog.analysis_topic_routes}
    assert dataset_ids == route_dataset_ids
    assert all(item.dataset_id in dataset_ids for item in catalog.analysis_topic_routes)


class _EmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="test",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


def _application_with_catalog(catalog: SemanticCatalog) -> AnalyticsApplication:
    store = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    store.create_schema()
    application = AnalyticsApplication(
        catalog=store,
        introspector=object(),
        executor=object(),
        embedding_gateway=_EmbeddingGateway(),
        require_evaluation_for_publish=False,
    )
    application.create_project(project_id=catalog.project_id, name="销售分析")
    table_models = tuple(
        item for item in catalog.models if item.model_detail.table_query is not None
    )
    store.save_schema_snapshot(
        project_id=catalog.project_id,
        snapshot=SchemaSnapshot(
            id="schema_schema",
            database_name="analytics",
            captured_at=datetime(2026, 8, 19, tzinfo=UTC),
            content_hash="sha256:schema",
            tables=tuple(
                TableSnapshot(
                    schema_name=item.model_detail.table_query.rsplit(".", 1)[0],
                    name=item.model_detail.table_query.rsplit(".", 1)[1],
                    columns=tuple(
                        SchemaColumnSnapshot(
                            name=field.field_name,
                            data_type=field.data_type,
                            nullable=True,
                            ordinal_position=index,
                        )
                        for index, field in enumerate(item.model_detail.fields)
                    ),
                )
                for item in table_models
            ),
        ),
    )
    release = compile_semantic_catalog(catalog)
    store.save_revision(
        ModelingRevision(
            id=catalog.revision_id,
            project_id=catalog.project_id,
            schema_snapshot_hash="sha256:schema",
            etag=1,
            semantic_spec=release,
            semantic_catalog=catalog,
        )
    )
    return application


def test_term_upsert_requires_a_governed_metric_or_dimension_binding():
    catalog = _catalog()
    application = _application_with_catalog(catalog)
    revision = application.get_revision(catalog.revision_id)

    with pytest.raises(SemanticValidationError) as error:
        application.upsert_term(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            term=TermSpec(id="term_unbound", name="无关联术语"),
        )

    assert error.value.code == "TERM_BINDING_REQUIRED"
    assert application.get_revision(revision.id).etag == revision.etag


@pytest.mark.parametrize(
    ("existing", "submitted"),
    (
        (
            None,
            TermSpec(
                id="term_new",
                name="收入",
                dataset_ids=("dataset_sales",),
                metric_ids=("metric_revenue",),
            ),
        ),
        (
            TermSpec(
                id="term_existing",
                name="收入",
                dataset_ids=("dataset_sales",),
                metric_ids=("metric_revenue",),
            ),
            TermSpec(
                id="term_existing",
                name="营收",
                dataset_ids=(),
                metric_ids=("metric_revenue",),
            ),
        ),
    ),
)
def test_term_upsert_keeps_query_scope_links_compiler_owned(existing, submitted):
    catalog = _catalog()
    if existing is not None:
        catalog = catalog.model_copy(update={"terms": (existing,)})
    application = _application_with_catalog(catalog)
    revision = application.get_revision(catalog.revision_id)

    with pytest.raises(SemanticValidationError) as error:
        application.upsert_term(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            term=submitted,
        )

    assert error.value.code == "TERM_SCOPE_LINKS_MANAGED"
    assert application.get_revision(revision.id).etag == revision.etag


def test_metric_delete_preview_cascades_derived_metrics_and_unlinks_dataset():
    catalog = _catalog()
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.METRIC,
        resource_id="metric_revenue",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.METRIC,
        resource_id="metric_revenue",
        expected_impact_hash=impact.impact_hash,
    )

    assert impact.requires_confirmation is True
    deleted = {
        (item.resource_kind, item.resource_id) for item in impact.effects if item.action == "delete"
    }
    assert deleted == {
        (ResourceKind.METRIC, "metric_revenue"),
        (ResourceKind.METRIC, "metric_avg_order_value"),
    }
    assert {item.id for item in updated.metrics} == {"metric_order_count"}
    orders_scope = updated.data_sets[0].data_set_detail.data_set_model_configs[0]
    assert orders_scope.metrics == ("metric_order_count",)


def test_dimension_delete_cleans_dataset_and_dictionary_references():
    catalog = _catalog_with_dimension_references()
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id="dimension_channel",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id="dimension_channel",
        expected_impact_hash=impact.impact_hash,
    )

    orders_scope = updated.data_sets[0].data_set_detail.data_set_model_configs[0]
    assert "dimension_channel" not in orders_scope.dimensions
    assert "dimension_channel" not in {item.id for item in updated.dimensions}


@pytest.mark.parametrize(
    ("target_type", "resource_kind", "resource_id"),
    [
        ("model", ResourceKind.MODEL, "model_orders"),
        ("metric", ResourceKind.METRIC, "metric_revenue"),
        ("dimension", ResourceKind.DIMENSION, "dimension_channel"),
        ("query_scope", ResourceKind.DATASET, "dataset_sales"),
    ],
)
def test_resource_delete_cascades_only_context_bound_to_deleted_targets(
    target_type,
    resource_kind,
    resource_id,
):
    catalog = _catalog()
    target_context = SemanticContextEntry(
        id=f"context-{target_type}",
        target_type=target_type,
        target_id=resource_id,
        kind="definition",
        text="随目标删除的上下文",
        source_type="catalog_description",
    )
    retained_context = SemanticContextEntry(
        id="context-project-retained",
        target_type="project",
        target_id=catalog.project_id,
        kind="convention",
        text="项目级上下文必须保留",
        source_type="human_convention",
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"semantic_context": (target_context, retained_context)}
        ).model_dump(mode="python")
    )
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    updated = planner.apply(
        catalog,
        resource_kind=resource_kind,
        resource_id=resource_id,
        expected_impact_hash=impact.impact_hash,
    )

    assert {item.id for item in updated.semantic_context} == {
        retained_context.id
    }
    assert any(
        item.action == "delete"
        and item.resource_kind.value == "semantic_context"
        and item.resource_id == target_context.id
        for item in impact.effects
    )


def test_removing_a_reviewed_context_subset_preserves_review_of_remaining_entries():
    catalog = _catalog()
    removed = SemanticContextEntry(
        id="context-dimension-removed",
        target_type="dimension",
        target_id="dimension_channel",
        kind="definition",
        text="渠道定义",
        source_type="catalog_description",
    )
    retained = SemanticContextEntry(
        id="context-project-retained",
        target_type="project",
        target_id=catalog.project_id,
        kind="convention",
        text="金额统一使用人民币",
        source_type="human_convention",
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"semantic_context": (removed, retained)}
        ).model_dump(mode="python")
    )
    reviewed_at = datetime(2026, 8, 27, tzinfo=UTC)
    revision = ModelingRevision(
        id=catalog.revision_id,
        project_id=catalog.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        semantic_catalog=catalog,
        semantic_spec=compile_semantic_catalog(catalog),
        semantic_context_review_hash=semantic_context_content_hash(
            catalog.semantic_context
        ),
        semantic_context_reviewed_by="reviewer-1",
        semantic_context_reviewed_at=reviewed_at,
    )
    reduced_catalog = SemanticCatalog.model_validate(
        catalog.model_copy(update={"semantic_context": (retained,)}).model_dump(
            mode="python"
        )
    )

    updated = RevisionEditor().replace_semantic_catalog(
        revision,
        expected_etag=revision.etag,
        expected_schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_catalog=reduced_catalog,
    )

    assert updated.semantic_context_review_hash == semantic_context_content_hash(
        (retained,)
    )
    assert updated.semantic_context_reviewed_by == "reviewer-1"
    assert updated.semantic_context_reviewed_at == reviewed_at

    changed_context = retained.model_copy(update={"text": "修改后的项目口径"})
    changed_catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"semantic_context": (removed, changed_context)}
        ).model_dump(mode="python")
    )
    changed = RevisionEditor().replace_semantic_catalog(
        revision,
        expected_etag=revision.etag,
        expected_schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_catalog=changed_catalog,
    )
    assert changed.semantic_context_review_hash is None
    assert changed.semantic_context_reviewed_by is None
    assert changed.semantic_context_reviewed_at is None


def test_model_delete_cascades_owned_semantics_but_preserves_other_model_scope():
    catalog = _catalog()
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.MODEL,
        resource_id="model_orders",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.MODEL,
        resource_id="model_orders",
        expected_impact_hash=impact.impact_hash,
    )

    assert "model_orders" not in {item.id for item in updated.models}
    assert updated.model_relations == ()
    assert {item.model_id for item in updated.dimensions} == {"model_customers"}
    assert updated.metrics == ()
    assert [item.id for item in updated.data_sets[0].data_set_detail.data_set_model_configs] == [
        "model_customers"
    ]


def test_delete_rejects_a_stale_impact_hash_after_catalog_change():
    catalog = _catalog()
    planner = CatalogDeletionPlanner()
    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.DATASET,
        resource_id="dataset_sales",
    )
    changed = catalog.model_copy(
        update={"data_sets": (catalog.data_sets[0].model_copy(update={"description": "changed"}),)}
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        planner.apply(
            changed,
            resource_kind=ResourceKind.DATASET,
            resource_id="dataset_sales",
            expected_impact_hash=impact.impact_hash,
        )

    assert exc_info.value.code == "DELETION_IMPACT_CHANGED"


def test_authenticated_delete_api_requires_exact_preview_hash():
    catalog = _catalog()
    metric_context = SemanticContextEntry(
        id="context-metric-revenue",
        target_type="metric",
        target_id="metric_revenue",
        kind="definition",
        text="收入指标说明",
        source_type="human_convention",
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"semantic_context": (metric_context,)}
        ).model_dump(mode="python")
    )
    application = _application_with_catalog(catalog)
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": catalog.project_id,
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v2",
    }
    base = (
        f"/v1/analytics/projects/{catalog.project_id}/revisions/{catalog.revision_id}"
        "/catalog/metrics/metric_revenue"
    )
    preview = client.post(
        f"{base}/deletion-impact",
        headers=headers,
        json={"expected_etag": 1, "schema_snapshot_hash": "sha256:schema"},
    )
    assert preview.status_code == 200, preview.text
    impact = preview.json()
    assert any(
        item["resource_kind"] == "semantic_context"
        and item["resource_id"] == metric_context.id
        for item in impact["effects"]
    )

    deleted = client.request(
        "DELETE",
        base,
        headers=headers,
        json={
            "expected_etag": 1,
            "schema_snapshot_hash": "sha256:schema",
            "expected_impact_hash": impact["impact_hash"],
            "confirmation": "delete",
        },
    )
    assert deleted.status_code == 200, deleted.text
    revision = deleted.json()
    assert revision["etag"] == 2
    assert {item["id"] for item in revision["semantic_catalog"]["metrics"]} == {
        "metric_order_count"
    }
    assert revision["semantic_catalog"]["semanticContext"] == []

    stale = client.request(
        "DELETE",
        base,
        headers=headers,
        json={
            "expected_etag": 1,
            "schema_snapshot_hash": "sha256:schema",
            "expected_impact_hash": impact["impact_hash"],
            "confirmation": "delete",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"


@pytest.mark.parametrize(
    ("resource_kind", "resource_id"),
    [
        (ResourceKind.RELATION, "relation_orders_customers"),
        (ResourceKind.DATASET, "dataset_sales"),
    ],
)
def test_direct_resource_delete_is_deterministic_under_semantic_id_renaming(
    resource_kind,
    resource_id,
):
    catalog = _catalog()
    planner = CatalogDeletionPlanner()
    impact = planner.preview(
        catalog,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )

    assert impact.effects[0].resource_id == resource_id
    assert impact.impact_hash.startswith("sha256:")

    renamed_id = f"renamed_{resource_id}"
    if resource_kind is ResourceKind.RELATION:
        renamed_catalog = catalog.model_copy(
            update={
                "model_relations": (
                    catalog.model_relations[0].model_copy(update={"id": renamed_id}),
                )
            }
        )
    else:
        renamed_catalog = catalog.model_copy(
            update={"data_sets": (catalog.data_sets[0].model_copy(update={"id": renamed_id}),)}
        )
    renamed_impact = planner.preview(
        SemanticCatalog.model_validate(renamed_catalog.model_dump(mode="python")),
        resource_kind=resource_kind,
        resource_id=renamed_id,
    )
    assert [(item.action, item.resource_kind, item.reason) for item in renamed_impact.effects] == [
        (item.action, item.resource_kind, item.reason) for item in impact.effects
    ]


def test_relation_delete_reconciles_scopes_and_invalidated_query_rules_in_preview() -> None:
    catalog = reconcile_query_scopes(_catalog())
    invalidated_rule = QueryRuleContract(
        id="rule-orders-customer-name",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_customer_name",),
    )
    retained_rule = QueryRuleContract(
        id="rule-orders-time",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_order_time",),
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"query_rules": (invalidated_rule, retained_rule)}
        ).model_dump(mode="python")
    )
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.RELATION,
        resource_id="relation_orders_customers",
    )
    repeated = planner.preview(
        catalog,
        resource_kind=ResourceKind.RELATION,
        resource_id="relation_orders_customers",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.RELATION,
        resource_id="relation_orders_customers",
        expected_impact_hash=impact.impact_hash,
    )

    assert repeated.impact_hash == impact.impact_hash
    assert repeated.effects == impact.effects
    _assert_scopes_are_fully_routed(updated)
    orders_scope = next(item for item in updated.data_sets if item.id == "dataset_sales")
    assert {item.id for item in orders_scope.data_set_detail.data_set_model_configs} == {
        "model_orders"
    }
    assert next(
        item for item in updated.analysis_topic_routes if item.dataset_id == "dataset_sales"
    ).paths == ()
    assert updated.query_rules == (retained_rule,)
    assert any(
        item.action == "unlink"
        and item.resource_kind is ResourceKind.DATASET
        and item.resource_id == "dataset_sales"
        for item in impact.effects
    )
    assert any(
        item.action == "delete"
        and item.resource_kind is ResourceKind.QUERY_RULE
        and item.resource_id == invalidated_rule.id
        for item in impact.effects
    )
    assert not any(
        item.resource_kind is ResourceKind.QUERY_RULE
        and item.resource_id == retained_rule.id
        for item in impact.effects
    )


def test_authenticated_relation_delete_returns_the_reconciled_query_rule_impact() -> None:
    catalog = reconcile_query_scopes(_catalog())
    invalidated_rule = QueryRuleContract(
        id="rule-orders-customer-name",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_customer_name",),
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(update={"query_rules": (invalidated_rule,)}).model_dump(
            mode="python"
        )
    )
    application = _application_with_catalog(catalog)
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": catalog.project_id,
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v2",
    }
    base = (
        f"/v1/analytics/projects/{catalog.project_id}/revisions/{catalog.revision_id}"
        "/catalog/relations/relation_orders_customers"
    )

    preview = client.post(
        f"{base}/deletion-impact",
        headers=headers,
        json={"expected_etag": 1, "schema_snapshot_hash": "sha256:schema"},
    )

    assert preview.status_code == 200, preview.text
    impact = preview.json()
    assert any(
        item["resource_kind"] == "query_rule"
        and item["resource_id"] == invalidated_rule.id
        for item in impact["effects"]
    )
    deleted = client.request(
        "DELETE",
        base,
        headers=headers,
        json={
            "expected_etag": 1,
            "schema_snapshot_hash": "sha256:schema",
            "expected_impact_hash": impact["impact_hash"],
            "confirmation": "delete",
        },
    )
    assert deleted.status_code == 200, deleted.text
    semantic_catalog = SemanticCatalog.model_validate(deleted.json()["semantic_catalog"])
    _assert_scopes_are_fully_routed(semantic_catalog)
    assert semantic_catalog.query_rules == ()


def test_authenticated_query_rule_delete_uses_the_generic_catalog_contract() -> None:
    catalog = reconcile_query_scopes(_catalog())
    rule = QueryRuleContract(
        id="rule-orders-time",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_order_time",),
    )
    catalog = SemanticCatalog.model_validate(
        catalog.model_copy(update={"query_rules": (rule,)}).model_dump(mode="python")
    )
    application = _application_with_catalog(catalog)
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": catalog.project_id,
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v2",
    }
    base = (
        f"/v1/analytics/projects/{catalog.project_id}/revisions/{catalog.revision_id}"
        f"/catalog/query-rules/{rule.id}"
    )

    preview = client.post(
        f"{base}/deletion-impact",
        headers=headers,
        json={"expected_etag": 1, "schema_snapshot_hash": "sha256:schema"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["resource_kind"] == "query_rule"
    deleted = client.request(
        "DELETE",
        base,
        headers=headers,
        json={
            "expected_etag": 1,
            "schema_snapshot_hash": "sha256:schema",
            "expected_impact_hash": preview.json()["impact_hash"],
            "confirmation": "delete",
        },
    )

    assert deleted.status_code == 200, deleted.text
    semantic_catalog = SemanticCatalog.model_validate(deleted.json()["semantic_catalog"])
    assert semantic_catalog.query_rules == ()
    _assert_scopes_are_fully_routed(semantic_catalog)


def test_model_delete_retires_its_compiled_scope_in_the_reviewed_plan() -> None:
    catalog = reconcile_query_scopes(_catalog())
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.MODEL,
        resource_id="model_orders",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.MODEL,
        resource_id="model_orders",
        expected_impact_hash=impact.impact_hash,
    )

    _assert_scopes_are_fully_routed(updated)
    assert "dataset_sales" not in {item.id for item in updated.data_sets}
    assert {item.root_model_id for item in updated.analysis_topic_routes} == {
        "model_customers"
    }
    assert any(
        item.action == "delete"
        and item.resource_kind is ResourceKind.DATASET
        and item.resource_id == "dataset_sales"
        for item in impact.effects
    )


def test_deleting_the_last_business_metric_retires_a_root_without_a_primary_key() -> None:
    catalog = _catalog()
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
    structural = SemanticCatalog.model_validate(
        catalog.model_copy(
            update={
                "models": tuple(
                    orders_without_primary if item.id == orders.id else item
                    for item in catalog.models
                ),
                "metrics": tuple(
                    item for item in catalog.metrics if item.id == "metric_revenue"
                ),
                "data_sets": (),
                "analysis_topic_routes": (),
            }
        ).model_dump(mode="python")
    )
    catalog = reconcile_query_scopes(structural)
    orders_dataset_id = next(
        item.dataset_id
        for item in catalog.analysis_topic_routes
        if item.root_model_id == "model_orders"
    )
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.METRIC,
        resource_id="metric_revenue",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.METRIC,
        resource_id="metric_revenue",
        expected_impact_hash=impact.impact_hash,
    )

    _assert_scopes_are_fully_routed(updated)
    assert orders_dataset_id not in {item.id for item in updated.data_sets}
    assert any(
        item.action == "delete"
        and item.resource_kind is ResourceKind.DATASET
        and item.resource_id == orders_dataset_id
        for item in impact.effects
    )


def test_compiler_owned_query_scope_cannot_be_deleted_directly() -> None:
    catalog = reconcile_query_scopes(_catalog())
    planner = CatalogDeletionPlanner()

    with pytest.raises(SemanticValidationError) as error:
        planner.preview(
            catalog,
            resource_kind=ResourceKind.DATASET,
            resource_id="dataset_sales",
        )

    assert error.value.code == "DERIVED_QUERY_SCOPE_IMMUTABLE"


def test_legacy_deletion_removes_a_rule_invalidated_without_enabling_scope_compile() -> None:
    rule = QueryRuleContract(
        id="rule-orders-customer-name",
        dataset_id="dataset_sales",
        priority=1,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("dimension_channel",),
        outputs=("dimension_customer_name",),
    )
    catalog = SemanticCatalog.model_validate(
        _catalog().model_copy(update={"query_rules": (rule,)}).model_dump(mode="python")
    )
    assert catalog.analysis_topic_routes == ()
    planner = CatalogDeletionPlanner()

    impact = planner.preview(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id="dimension_customer_name",
    )
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id="dimension_customer_name",
        expected_impact_hash=impact.impact_hash,
    )

    assert updated.analysis_topic_routes == ()
    assert updated.query_rules == ()
    compile_semantic_catalog(updated)
    assert any(
        item.action == "delete"
        and item.resource_kind is ResourceKind.QUERY_RULE
        and item.resource_id == rule.id
        for item in impact.effects
    )
