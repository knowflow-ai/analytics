from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.pool import StaticPool

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import (
    CatalogError,
    CatalogStore,
    modeling_plans,
    modeling_proposals,
    modeling_runs,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.evaluation.contracts import GoldenCase, GoldenSuite
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller, AliasSuggestionOutput
from knowflow_analytics.modeling.catalog_contracts import (
    ModelDimensionContract,
    ModelDimensionType,
)
from knowflow_analytics.modeling.contracts import (
    DimensionDataProfile,
    ModelingProposalStatus,
    ModelingRunSource,
    ProfiledValue,
    SchemaSnapshot,
    SemanticAliasReview,
    SemanticDataProfile,
    SuggestionDecision,
    SuggestionPatch,
    SuggestionSource,
    TableCatalogEntry,
)
from knowflow_analytics.modeling.product import DecisionChoice, ModelingPlanPhase
from knowflow_analytics.modeling.relation_candidates import (
    synchronize_database_relation_candidates,
)
from knowflow_analytics.modeling.revision import RevisionConflictError
from knowflow_analytics.query.contracts import MemoryReviewResult, MemoryStatus, QueryState
from knowflow_analytics.semantic.index import EmbeddingBatch


class _StaticIntrospector:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def list_schemas(self):
        return tuple(sorted({item.schema_name for item in self.snapshot.tables}))

    def list_tables(self, *, schema_name, include_views=False):
        del include_views
        return tuple(
            TableCatalogEntry(
                schema_name=item.schema_name,
                name=item.name,
                source_type=item.source_type,
                comment=item.comment,
            )
            for item in self.snapshot.tables
            if item.schema_name == schema_name
        )

    def describe_table(self, *, schema_name, table_name, include_views=False):
        del include_views
        return next(
            item
            for item in self.snapshot.tables
            if item.schema_name == schema_name and item.name == table_name
        )

    def scan(self, **_kwargs):
        return self.snapshot


class _ScopedIntrospector(_StaticIntrospector):
    def scan(self, **kwargs):
        selected_tables = kwargs.get("selected_tables")
        if selected_tables is None:
            tables = self.snapshot.tables
        else:
            selected = {
                (schema, table)
                for schema, table_names in selected_tables.items()
                for table in table_names
            }
            tables = tuple(
                table
                for table in self.snapshot.tables
                if (table.schema_name, table.name) in selected
            )
        return SchemaSnapshot.create(
            database_name=self.snapshot.database_name,
            captured_at=self.snapshot.captured_at,
            tables=tables,
        )


class _EmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="test",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


class _AiModeller:
    def __init__(self) -> None:
        self.alias_calls = 0

    def suggest(self, *, revision, **_kwargs):
        model = revision.semantic_spec.models[0]
        return (
            SuggestionPatch(
                id=f"suggestion:{revision.id}:ai:model-name",
                target_kind="model",
                target_id=model.id,
                changes={"name": "AI 预填名称"},
                source=SuggestionSource.AI_SCHEMA,
                confidence=0.8,
                reason="页面预填建议",
            ),
        )

    def suggest_alias_batch(self, *, resources, **_kwargs):
        self.alias_calls += 1
        return {str(item["resource_id"]): AliasSuggestionOutput(aliases=()) for item in resources}


class _CompleteAiModeller:
    def suggest(self, *, revision, **_kwargs):
        segment = next(item for item in revision.semantic_spec.fields if item.column == "segment")
        return (
            SuggestionPatch(
                id=f"suggestion:{revision.id}:segment",
                target_kind="field",
                target_id=segment.id,
                changes={
                    "name": "客户分群",
                    "description": "客户经营分层",
                    "kind": "dimension",
                    "dimension_type": "categorical",
                    "semantic_expr": "segment",
                    "create_dimension": True,
                },
                source=SuggestionSource.AI_SCHEMA,
                confidence=0.9,
                reason="AI 字段分类",
            ),
        )

    def suggest_alias_batch(self, *, resources, **_kwargs):
        return {
            str(item["resource_id"]): AliasSuggestionOutput(
                aliases=("客户类型",) if item["resource_type"] == "dimension" else ("客户数",)
            )
            for item in resources
        }


class _FailingAliasModeller(_AiModeller):
    def suggest_alias_batch(self, **_kwargs):
        raise RuntimeError("alias model unavailable")


class _DimensionValueAliases:
    def suggest(self, *, candidates, **_kwargs):
        return {
            item.id: {
                "display_name": str(item.value),
                "aliases": (f"{item.value}地区",),
            }
            for item in candidates
        }


def _alias_reviews(proposal):
    return tuple(
        SemanticAliasReview(
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            aliases=item.aliases,
            display_name=item.display_name,
        )
        for item in proposal.artifact.alias_drafts
    )


class _ExactModelSchemaGateway:
    def generate_json(self, **_kwargs):
        return {
            "name": "销售订单",
            "bizName": "sales_orders",
            "description": "销售订单事实模型",
            "semanticColumns": [
                {
                    "columnName": "net_amount",
                    "dataType": "NUMERIC(18,2)",
                    "comment": "订单净收入候选度量",
                    "filedType": "categorical",
                    "name": "订单净金额",
                    "expr": "net_amount",
                }
            ],
            "metrics": [{"columnName": "net_amount", "agg": "SUM", "unit": "元"}],
        }


class _AliasModelGateway:
    def __init__(self) -> None:
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return {"aliases": ["净收入", "销售额", "销售额", "net_revenue", "确认收入"]}


def _application(
    schema_snapshot,
    *,
    ai_modeller=None,
    require_evaluation=True,
    require_quality_report=True,
    introspector=None,
    semantic_profiler=None,
    dimension_alias_suggester=None,
):
    catalog = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    catalog.create_schema()
    return AnalyticsApplication(
        catalog=catalog,
        introspector=introspector or _StaticIntrospector(schema_snapshot),
        executor=object(),  # query execution is outside these modeling contract tests
        embedding_gateway=_EmbeddingGateway(),
        ai_modeller=ai_modeller,
        dimension_alias_suggester=dimension_alias_suggester,
        semantic_profiler=semantic_profiler,
        require_evaluation_for_publish=require_evaluation,
        require_quality_report_for_publish=require_quality_report,
    )


class _DimensionValueProfiler:
    def __init__(
        self,
        *,
        truncated: bool = False,
        source_rows_truncated: bool = False,
        observed_distinct_values: int | None = None,
        omit_target: bool = False,
    ) -> None:
        self.truncated = truncated
        self.source_rows_truncated = source_rows_truncated
        self.observed_distinct_values = observed_distinct_values
        self.omit_target = omit_target
        self.calls: list[tuple[str, ...]] = []

    def profile(self, *, snapshot, semantic_spec, dimension_ids=None):
        selected = tuple(dimension_ids or ())
        self.calls.append(selected)
        dimension = next(item for item in semantic_spec.dimensions if item.id == selected[0])
        dimensions = (
            ()
            if self.omit_target
            else (
                DimensionDataProfile(
                    dimension_id=dimension.id,
                    model_id=dimension.model_id,
                    field_id=dimension.field_id,
                    sampled_rows=3,
                    observed_distinct_values=(
                        self.observed_distinct_values
                        if self.observed_distinct_values is not None
                        else 51
                        if self.truncated
                        else 2
                    ),
                    source_rows_truncated=self.source_rows_truncated,
                    truncated=self.truncated,
                    values=(
                        ProfiledValue(value="华东", frequency=2),
                        ProfiledValue(value="华南", frequency=1),
                    ),
                ),
            )
        )
        return SemanticDataProfile(
            id="profile-auto-dictionary",
            schema_snapshot_hash=snapshot.content_hash,
            content_hash="sha256:auto-dictionary-profile",
            captured_at=snapshot.captured_at,
            dimensions=dimensions,
            warnings=("画像失败，未生成维度值候选",) if self.omit_target else (),
        )


def test_schema_browse_revision_and_model_creation_are_separate(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")

    assert application.list_datasource_schemas(project_id="sales") == ("sales",)
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )

    assert revision.semantic_spec.models == ()
    assert revision.semantic_spec.fields == ()
    assert revision.semantic_spec.datasets == ()

    customers = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    assert [item.table for item in customers.semantic_spec.models] == ["customers"]
    assert customers.semantic_spec.datasets == ()
    assert customers.suggestions == ()
    assert customers.semantic_catalog is not None
    assert customers.semantic_catalog.models[0].model_detail.identifiers

    orders = application.add_table_model(
        revision_id=customers.id,
        expected_etag=customers.etag,
        schema_snapshot_hash=customers.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    assert {item.table for item in orders.semantic_spec.models} == {"customers", "orders"}
    assert orders.semantic_spec.datasets == ()
    assert orders.suggestions == ()
    assert len(orders.semantic_spec.relations) == 1
    assert orders.semantic_catalog is not None
    relation = orders.semantic_catalog.model_relations[0]
    assert relation.from_model_id == next(
        item.id for item in orders.semantic_spec.models if item.table == "orders"
    )
    assert relation.to_model_id == next(
        item.id for item in orders.semantic_spec.models if item.table == "customers"
    )
    assert relation.join_type == "left join"
    assert relation.join_conditions[0].left_field == "customer_id"
    assert relation.join_conditions[0].right_field == "id"
    assert relation.knowflow_cardinality is None


def test_creating_categorical_dimension_automatically_presets_database_values(
    schema_snapshot,
):
    profiler = _DimensionValueProfiler()
    application = _application(schema_snapshot, semantic_profiler=profiler)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = revision.semantic_catalog.models[0]
    model = model.model_copy(
        update={
            "model_detail": model.model_detail.model_copy(
                update={
                    "dimensions": (
                        ModelDimensionContract(
                            name="区域",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="region",
                            biz_name="region",
                            data_type="TEXT",
                            is_create_dimension=1,
                        ),
                    )
                }
            )
        }
    )

    updated = application.upsert_catalog_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        model=model,
    )

    dimension = updated.semantic_spec.dimensions[0]
    assert profiler.calls == [(dimension.id,)]
    assert [
        (item.value, item.display_name, item.aliases, item.enabled)
        for item in updated.semantic_spec.dimension_values
    ] == [
        ("华东", "华东", (), True),
        ("华南", "华南", (), True),
    ]
    assert len(updated.semantic_spec.datasets) == 1
    assert dimension.id in updated.semantic_spec.datasets[0].dimension_ids
    assert updated.semantic_spec.analysis_topic_routes[0].root_model_id == model.id


@pytest.mark.parametrize(
    "profiler",
    [
        _DimensionValueProfiler(truncated=True),
        _DimensionValueProfiler(source_rows_truncated=True),
        _DimensionValueProfiler(observed_distinct_values=3),
    ],
    ids=("value_limit", "source_rows", "distinct_count_mismatch"),
)
def test_creating_dimension_does_not_publish_partial_dictionary(
    schema_snapshot,
    profiler,
):
    application = _application(schema_snapshot, semantic_profiler=profiler)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = revision.semantic_catalog.models[0]
    model = model.model_copy(
        update={
            "model_detail": model.model_detail.model_copy(
                update={
                    "dimensions": (
                        ModelDimensionContract(
                            name="区域",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="region",
                            biz_name="region",
                            data_type="TEXT",
                            is_create_dimension=1,
                        ),
                    )
                }
            )
        }
    )

    updated = application.upsert_catalog_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        model=model,
    )

    assert len(updated.semantic_spec.dimensions) == 1
    assert updated.semantic_spec.dimension_values == ()


def test_creating_dimension_fails_closed_when_automatic_profile_omits_target(
    schema_snapshot,
):
    profiler = _DimensionValueProfiler(omit_target=True)
    application = _application(schema_snapshot, semantic_profiler=profiler)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = revision.semantic_catalog.models[0]
    model = model.model_copy(
        update={
            "model_detail": model.model_detail.model_copy(
                update={
                    "dimensions": (
                        ModelDimensionContract(
                            name="区域",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="region",
                            biz_name="region",
                            data_type="TEXT",
                            is_create_dimension=1,
                        ),
                    )
                }
            )
        }
    )

    with pytest.raises(SemanticValidationError) as error:
        application.upsert_catalog_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            model=model,
        )

    assert error.value.code == "DIMENSION_DICTIONARY_PROFILE_INCOMPLETE"
    unchanged = application.get_revision(revision.id)
    assert unchanged.etag == revision.etag
    assert unchanged.semantic_spec.dimensions == ()


def test_dimension_value_edit_keeps_database_identity_server_owned(schema_snapshot):
    profiler = _DimensionValueProfiler()
    application = _application(schema_snapshot, semantic_profiler=profiler)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = revision.semantic_catalog.models[0]
    model = model.model_copy(
        update={
            "model_detail": model.model_detail.model_copy(
                update={
                    "dimensions": (
                        ModelDimensionContract(
                            name="区域",
                            type=ModelDimensionType.CATEGORICAL,
                            expr="region",
                            biz_name="region",
                            data_type="TEXT",
                            is_create_dimension=1,
                        ),
                    )
                }
            )
        }
    )
    revision = application.upsert_catalog_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        model=model,
    )
    original = revision.semantic_spec.dimension_values[0]

    updated = application.upsert_dimension_value(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        dimension_value=original.model_copy(
            update={
                "display_name": "华东区域",
                "aliases": ("东区",),
                "enabled": False,
            }
        ),
    )

    edited = updated.semantic_spec.dimension_values[0]
    assert (edited.id, edited.dimension_id, edited.value) == (
        original.id,
        original.dimension_id,
        original.value,
    )
    assert (edited.display_name, edited.aliases, edited.enabled) == (
        "华东区域",
        ("东区",),
        False,
    )
    with pytest.raises(SemanticValidationError) as exc_info:
        application.upsert_dimension_value(
            revision_id=updated.id,
            expected_etag=updated.etag,
            schema_snapshot_hash=updated.schema_snapshot_hash,
            dimension_value=original.model_copy(
                update={"id": "dimension_value:not-in-this-revision"}
            ),
        )
    assert exc_info.value.code == "DIMENSION_VALUE_NOT_FOUND"

    with pytest.raises(SemanticValidationError) as exc_info:
        application.upsert_dimension_value(
            revision_id=updated.id,
            expected_etag=updated.etag,
            schema_snapshot_hash=updated.schema_snapshot_hash,
            dimension_value=edited.model_copy(update={"value": "tampered"}),
        )
    assert exc_info.value.code == "DIMENSION_VALUE_IDENTITY_IMMUTABLE"


def test_fk_relation_candidates_are_independent_of_table_import_order(schema_snapshot):
    def import_in_order(table_names):
        application = _application(schema_snapshot)
        application.create_project(project_id="sales", name="销售分析")
        snapshot = application.create_schema_snapshot(
            project_id="sales",
            schemas=("sales",),
            selected_tables={"sales": tuple(table_names)},
        )
        revision = application.create_empty_revision(
            project_id="sales",
            schema_snapshot_id=snapshot.id,
        )
        for table_name in table_names:
            revision = application.add_table_model(
                revision_id=revision.id,
                expected_etag=revision.etag,
                schema_snapshot_hash=revision.schema_snapshot_hash,
                schema_name="sales",
                table_name=table_name,
            )
        assert revision.semantic_catalog is not None
        return tuple(
            item.model_dump(mode="json", by_alias=True)
            for item in revision.semantic_catalog.model_relations
        )

    assert import_in_order(("customers", "orders")) == import_in_order(("orders", "customers"))


def test_schema_without_foreign_keys_proposes_but_never_publishes_relations(
    schema_snapshot,
):
    """Reviewed policy change: a constraint-free database used to yield no
    relations at all, which degrades into disconnected single-table topics. Name
    inference now proposes the edge, but a proposal is not a fact -- it carries no
    cardinality and stays out of the semantic spec until a human confirms it."""
    no_fk_snapshot = SchemaSnapshot.create(
        database_name="holdout_renamed",
        captured_at=schema_snapshot.captured_at,
        tables=tuple(
            table.model_copy(update={"foreign_keys": ()}) for table in schema_snapshot.tables
        ),
    )
    application = _application(no_fk_snapshot)
    application.create_project(project_id="holdout", name="无外键域")
    snapshot = application.create_schema_snapshot(
        project_id="holdout",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="holdout",
        schema_snapshot_id=snapshot.id,
    )
    for table_name in ("customers", "orders"):
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table_name,
        )

    assert revision.semantic_catalog is not None
    proposals = revision.semantic_catalog.model_relations
    assert [item.knowflow_evidence for item in proposals] == ["name_convention"]
    # No cardinality means the relation cannot reach a release: publication fails
    # closed on RELATION_CARDINALITY_REQUIRED until a human confirms it, exactly
    # as it does for an unconfirmed database foreign key.
    assert all(item.knowflow_cardinality is None for item in proposals)
    with pytest.raises(SemanticValidationError) as raised:
        application.publish_revision(revision.id, expected_etag=revision.etag)
    assert raised.value.code == "RELATION_CARDINALITY_REQUIRED"


def test_fk_candidates_keep_distinct_referenced_column_pairs(schema_snapshot):
    customers, orders = schema_snapshot.tables
    extended_customers = customers.model_copy(
        update={
            "columns": (
                *customers.columns,
                customers.columns[0].model_copy(
                    update={
                        "name": "external_id",
                        "ordinal_position": len(customers.columns),
                        "primary_key": False,
                        "unique": True,
                    }
                ),
            )
        }
    )
    alternate_reference = orders.foreign_keys[0].model_copy(
        update={"name": "orders_customer_external_fk", "referred_columns": ("external_id",)}
    )
    multiple_fk_snapshot = SchemaSnapshot.create(
        database_name="holdout_multiple_fk",
        captured_at=schema_snapshot.captured_at,
        tables=(
            extended_customers,
            orders.model_copy(update={"foreign_keys": (*orders.foreign_keys, alternate_reference)}),
        ),
    )
    application = _application(multiple_fk_snapshot)
    application.create_project(project_id="multiple-fk", name="多外键域")
    snapshot = application.create_schema_snapshot(
        project_id="multiple-fk",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="multiple-fk",
        schema_snapshot_id=snapshot.id,
    )
    for table_name in ("customers", "orders"):
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table_name,
        )

    assert revision.semantic_catalog is not None
    relations = revision.semantic_catalog.model_relations
    assert len(relations) == 2
    assert len({item.id for item in relations}) == 2
    assert {item.join_conditions[0].right_field for item in relations} == {
        "id",
        "external_id",
    }


def test_unrelated_table_import_does_not_resurrect_a_removed_fk_relation(
    schema_snapshot,
):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    for table_name in ("customers", "orders"):
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table_name,
        )
    assert revision.semantic_catalog is not None
    without_relation = revision.semantic_catalog.model_copy(update={"model_relations": ()})

    synchronized = synchronize_database_relation_candidates(
        catalog=without_relation,
        snapshot=schema_snapshot,
        changed_model_ids=frozenset({"model:sales:unrelated"}),
    )

    assert synchronized.model_relations == ()


def test_alias_suggestion_prefills_the_form_without_mutating_revision(schema_snapshot):
    """Match AliasGenerateHelper: AI returns a suggestion; save remains explicit."""

    gateway = _AliasModelGateway()
    application = _application(
        schema_snapshot,
        ai_modeller=AiSemanticModeller(model_gateway=gateway, workflow="single_call"),
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = revision.semantic_spec.models[0]
    before = application.get_revision(revision.id).model_dump(mode="json")
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }

    response = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision.id}/alias-suggestions",
            headers=headers,
            json={
                "expected_etag": revision.etag,
                "resource_type": "metric",
                "model_id": model.id,
                "name": "净收入",
                "biz_name": "net_revenue",
                "description": "扣除退款后的确认收入",
                "existing_aliases": ["销售额"],
            },
        )
    )

    assert response["aliases"] == ["确认收入"]
    assert response["revision_etag"] == revision.etag
    assert application.get_revision(revision.id).model_dump(mode="json") == before
    assert gateway.calls[0]["purpose"] == "analytics.alias_suggestion"


def test_table_expansion_creates_child_revision_and_preserves_governed_catalog(
    schema_snapshot,
):
    application = _application(
        schema_snapshot,
        introspector=_ScopedIntrospector(schema_snapshot),
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    customer_model = revision.semantic_catalog.models[0]
    revision = application.upsert_catalog_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        model=customer_model.model_copy(
            update={"name": "客户主数据", "description": "已经人工治理的客户模型"}
        ),
    )

    child = application.extend_revision_tables(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        selected_tables={"sales": ("orders",)},
    )

    assert child.id != revision.id
    assert child.parent_revision_id == revision.id
    assert child.etag == 1
    assert child.state.value == "draft"
    assert child.schema_snapshot_hash != revision.schema_snapshot_hash
    assert {model.table for model in child.semantic_spec.models} == {
        "customers",
        "orders",
    }
    inherited = next(model for model in child.semantic_spec.models if model.table == "customers")
    assert inherited.name == "客户主数据"
    assert inherited.description == "已经人工治理的客户模型"
    assert child.semantic_catalog is not None
    assert child.semantic_catalog.revision_id == child.id
    assert child.suggestions == ()

    unchanged = application.catalog.get_revision(revision.id)
    assert unchanged.etag == revision.etag
    assert len(unchanged.semantic_spec.models) == 1


def test_table_expansion_rejects_existing_table_schema_drift(schema_snapshot):
    class _DriftIntrospector(_ScopedIntrospector):
        def scan(self, **kwargs):
            snapshot = super().scan(**kwargs)
            selected_count = sum(
                len(tables) for tables in (kwargs.get("selected_tables") or {}).values()
            )
            if selected_count < 2:
                return snapshot
            tables = tuple(
                table.model_copy(
                    update={
                        "columns": tuple(
                            column.model_copy(update={"data_type": "TEXT"})
                            if table.name == "customers" and column.name == "id"
                            else column
                            for column in table.columns
                        )
                    }
                )
                for table in snapshot.tables
            )
            return SchemaSnapshot.create(
                database_name=snapshot.database_name,
                captured_at=snapshot.captured_at,
                tables=tables,
            )

    application = _application(
        schema_snapshot,
        introspector=_DriftIntrospector(schema_snapshot),
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )

    with pytest.raises(
        SemanticValidationError,
        match="requires explicit reconciliation",
    ) as exc_info:
        application.extend_revision_tables(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            selected_tables={"sales": ("orders",)},
        )

    assert exc_info.value.code == "EXISTING_TABLE_SCHEMA_DRIFT"
    assert application.catalog.get_latest_revision(project_id="sales").id == revision.id


def test_table_expansion_http_contract_returns_the_child_candidate(schema_snapshot):
    application = _application(
        schema_snapshot,
        introspector=_ScopedIntrospector(schema_snapshot),
    )
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }
    _ok(
        client.post(
            "/v1/analytics/projects",
            headers=headers,
            json={"name": "销售分析", "project_id": "sales"},
        )
    )
    snapshot = _ok(
        client.post(
            "/v1/analytics/projects/sales/schema-snapshots",
            headers=headers,
            json={
                "schemas": ["sales"],
                "selected_tables": {"sales": ["customers"]},
            },
        )
    )
    revision = _ok(
        client.post(
            "/v1/analytics/projects/sales/revisions",
            headers=headers,
            json={"schema_snapshot_id": snapshot["id"]},
        )
    )
    revision = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/models:from-table",
            headers=headers,
            json={
                "expected_etag": revision["etag"],
                "schema_snapshot_hash": revision["schema_snapshot_hash"],
                "schema_name": "sales",
                "table_name": "customers",
            },
        )
    )

    child = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/tables:extend",
            headers=headers,
            json={
                "expected_etag": revision["etag"],
                "schema_snapshot_hash": revision["schema_snapshot_hash"],
                "selected_tables": {"sales": ["orders"]},
            },
        )
    )

    assert child["parent_revision_id"] == revision["id"]
    assert child["id"] != revision["id"]
    assert {model["table"] for model in child["semantic_spec"]["models"]} == {
        "customers",
        "orders",
    }
    assert len(child["semantic_catalog"]["modelRelations"]) == 1
    assert child["semantic_catalog"]["modelRelations"][0]["knowflowCardinality"] is None


def test_ai_run_prefills_without_mutating_until_human_applies(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    before = application.get_revision(revision.id)

    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
        source=ModelingRunSource.UI,
    )

    assert run.revision_etag == revision.etag
    assert run.suggestions[0].changes == {"name": "AI 预填名称"}
    assert application.get_revision(revision.id) == before

    updated = application.apply_ai_suggestion_run(
        revision_id=revision.id,
        run_id=run.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        decisions=(SuggestionDecision(suggestion_id=run.suggestions[0].id, accept=True),),
        reviewed_by="analyst-1",
    )

    assert updated.etag == revision.etag + 1
    assert updated.semantic_spec.models[0].name == "AI 预填名称"
    reviewed_run = application.get_modeling_run(run.id)
    assert reviewed_run.status.value == "applied"
    assert reviewed_run.reviewed_by == "analyst-1"
    assert reviewed_run.resulting_revision_etag == updated.etag


def test_ai_run_requires_an_explicit_decision_for_every_prefill(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )

    with pytest.raises(RevisionConflictError, match="explicitly accepted or rejected"):
        application.apply_ai_suggestion_run(
            revision_id=revision.id,
            run_id=run.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            decisions=(),
            reviewed_by="analyst-1",
        )


def test_m3_ai_modeling_proposal_prefills_every_suggestion_without_mutating_revision(
    schema_snapshot,
):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    before = application.get_revision(revision.id)

    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        source=ModelingRunSource.UI,
        created_by="analyst-1",
    )

    assert proposal.status is ModelingProposalStatus.DRAFT
    assert proposal.revision_etag == revision.etag
    assert len(proposal.suggestions) == len(proposal.decisions) == 1
    assert proposal.decisions[0].accept is True
    assert proposal.proposal_hash.startswith("sha256:")
    assert application.get_revision(revision.id) == before

    with pytest.raises(RevisionConflictError, match="proposal is stale"):
        application.apply_ai_modeling_proposal(
            revision_id=revision.id,
            proposal_id=proposal.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            expected_proposal_etag=proposal.etag,
            expected_proposal_hash=proposal.proposal_hash,
            reviewed_by="analyst-1",
        )


def test_one_click_artifact_failure_does_not_persist_orphan_run_or_proposal(
    schema_snapshot,
):
    application = _application(schema_snapshot, ai_modeller=_FailingAliasModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )

    with pytest.raises(RuntimeError, match="alias model unavailable"):
        application.create_ai_modeling_proposal(
            revision_id=revision.id,
            expected_etag=revision.etag,
            created_by="analyst-1",
        )

    with application.catalog._engine.connect() as connection:
        run_count = connection.scalar(select(func.count()).select_from(modeling_runs))
        proposal_count = connection.scalar(select(func.count()).select_from(modeling_proposals))
    assert run_count == 0
    assert proposal_count == 0
    assert application.get_revision(revision.id) == revision


def test_m3_ai_modeling_proposal_saves_human_overrides_then_applies_atomically(
    schema_snapshot,
):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        source=ModelingRunSource.UI,
        created_by="analyst-1",
    )
    suggestion_id = proposal.suggestions[0].id

    revised_proposal = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=proposal.etag,
        expected_proposal_hash=proposal.proposal_hash,
        decisions=(
            SuggestionDecision(
                suggestion_id=suggestion_id,
                accept=True,
                overrides={"name": "客户主数据"},
            ),
        ),
        alias_reviews=_alias_reviews(proposal),
        saved_by="analyst-1",
    )

    assert revised_proposal.etag == proposal.etag + 1
    assert revised_proposal.reviewed_artifact_hash is None
    assert application.get_revision(revision.id).etag == revision.etag

    reviewed_proposal = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=revised_proposal.etag,
        expected_proposal_hash=revised_proposal.proposal_hash,
        decisions=revised_proposal.decisions,
        alias_reviews=_alias_reviews(revised_proposal),
        saved_by="analyst-1",
    )
    assert reviewed_proposal.reviewed_artifact_hash == (reviewed_proposal.artifact.artifact_hash)

    result = application.apply_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        expected_proposal_etag=reviewed_proposal.etag,
        expected_proposal_hash=reviewed_proposal.proposal_hash,
        reviewed_by="analyst-1",
    )

    assert result.revision.etag == revision.etag + 1
    assert result.revision.semantic_spec.models[0].name == "客户主数据"
    assert result.revision.ai_modeling_artifact_hash == result.proposal.artifact.artifact_hash
    assert result.revision.semantic_context_review_hash
    assert result.revision.semantic_context_reviewed_by == "analyst-1"
    assert result.revision.semantic_context_reviewed_at is not None
    assert result.revision.semantic_spec.analysis_topic_routes[0].default_count_metric_id
    assert application.validate_revision(result.revision.id).state.value == "validated"
    assert result.proposal.status is ModelingProposalStatus.APPLIED
    assert result.proposal.resulting_revision_etag == result.revision.etag
    assert application.get_modeling_run(result.proposal.suggestion_run_id).status.value == "applied"


def test_m3_ai_modeling_proposal_rejects_stale_draft_save(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )

    with pytest.raises(RevisionConflictError, match="proposal.*stale"):
        application.save_ai_modeling_proposal(
            revision_id=revision.id,
            proposal_id=proposal.id,
            expected_proposal_etag=proposal.etag + 1,
            expected_proposal_hash=proposal.proposal_hash,
            decisions=proposal.decisions,
            alias_reviews=_alias_reviews(proposal),
            saved_by="analyst-1",
        )


def test_ai_modeling_proposal_apply_rejects_revision_etag_change_without_spec_change(
    schema_snapshot,
):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )
    concurrently_changed = revision.model_copy(update={"etag": revision.etag + 1})
    application.catalog.update_revision(concurrently_changed, previous_etag=revision.etag)

    with pytest.raises(RevisionConflictError, match="proposal is stale"):
        application.apply_ai_modeling_proposal(
            revision_id=revision.id,
            proposal_id=proposal.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            expected_proposal_etag=proposal.etag,
            expected_proposal_hash=proposal.proposal_hash,
            reviewed_by="analyst-1",
        )


def test_unchanged_one_click_proposal_save_reuses_the_confirmed_alias_artifact(
    schema_snapshot,
):
    modeller = _AiModeller()
    application = _application(schema_snapshot, ai_modeller=modeller)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )
    initial_alias_calls = modeller.alias_calls

    saved = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=proposal.etag,
        expected_proposal_hash=proposal.proposal_hash,
        decisions=proposal.decisions,
        alias_reviews=_alias_reviews(proposal),
        saved_by="analyst-1",
    )

    assert saved.artifact == proposal.artifact
    assert modeller.alias_calls == initial_alias_calls


class _CountingCompleteAiModeller(_CompleteAiModeller):
    def __init__(self) -> None:
        self.alias_calls = 0

    def suggest_alias_batch(self, **kwargs):
        self.alias_calls += 1
        return super().suggest_alias_batch(**kwargs)


def test_saving_a_field_classifying_proposal_unchanged_does_not_call_the_model_again(
    schema_snapshot,
):
    """点「确认并应用全部」不该再调模型。

    已有的同类测试用的 modeller 只改模型名，不分类字段 —— 而真实 AI 会把字段
    分成维度/度量，这会改变 Catalog，进而改变预览 revision 的 spec_hash。
    如果服务端预建产物时用的决策与用户提交的不同，materialize 会判
    AI_MODELING_ARTIFACT_STALE，重建产物（再调一次别名模型）并清空已核对标记。
    """

    modeller = _CountingCompleteAiModeller()
    profiler = _DimensionValueProfiler()
    application = _application(
        schema_snapshot,
        ai_modeller=modeller,
        semantic_profiler=profiler,
        dimension_alias_suggester=_DimensionValueAliases(),
        require_evaluation=False,
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(project_id="sales", schema_snapshot_id=snapshot.id)
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )
    calls_after_create = modeller.alias_calls
    calls_to_profiler = len(profiler.calls)

    saved = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=proposal.etag,
        expected_proposal_hash=proposal.proposal_hash,
        # 前端把建议的 changes 原样塞进 overrides 后再提交（表单可编辑，
        # 未改动时值与 changes 相同）。同值 override 不该被当成"改动"。
        decisions=tuple(
            item.model_copy(
                update={
                    "overrides": dict(
                        next(s.changes for s in proposal.suggestions if s.id == item.suggestion_id)
                    )
                }
            )
            if item.accept
            else item
            for item in proposal.decisions
        ),
        alias_reviews=_alias_reviews(proposal),
        saved_by="analyst-1",
    )

    assert modeller.alias_calls == calls_after_create
    assert saved.artifact == proposal.artifact
    # 用户什么都没改，不该被要求"重新核对"
    assert saved.reviewed_artifact_hash == saved.artifact.artifact_hash
    # 保存不该再打一次库：profile 的结果已经冻结在 artifact.dimension_values 里。
    assert len(profiler.calls) == calls_to_profiler
    # 产物冻结的维度值必须还在 —— 它正是 create 时预览 Catalog 的一部分，
    # 丢了就会 spec_hash 不同、判过期、重建。
    assert proposal.artifact.dimension_values


def test_one_click_modeling_returns_and_applies_all_three_alias_levels_and_count_topic(
    schema_snapshot,
):
    profiler = _DimensionValueProfiler()
    application = _application(
        schema_snapshot,
        ai_modeller=_CompleteAiModeller(),
        semantic_profiler=profiler,
        dimension_alias_suggester=_DimensionValueAliases(),
        require_evaluation=False,
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )

    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )

    assert {item.resource_type for item in proposal.artifact.alias_drafts} == {
        "dimension",
        "metric",
        "dimension_value",
    }
    assert len(proposal.artifact.default_count_metrics) == 1
    assert len(proposal.artifact.analysis_topic_routes) == 1
    assert proposal.artifact.analysis_topic_routes[0].default_count_metric_id == (
        proposal.artifact.default_count_metrics[0].id
    )
    assert proposal.artifact.semantic_context
    assert proposal.artifact.query_scope_compiler_version == "knowflow-query-scope-v1"
    assert proposal.artifact.query_scope_compilation_hash.startswith("sha256:")
    assert len(proposal.artifact.query_scope_diagnostics) == len(
        proposal.artifact.analysis_topic_routes
    )

    with pytest.raises(SemanticValidationError) as raised:
        application.save_ai_modeling_proposal(
            revision_id=revision.id,
            proposal_id=proposal.id,
            expected_proposal_etag=proposal.etag,
            expected_proposal_hash=proposal.proposal_hash,
            decisions=proposal.decisions,
            alias_reviews=(),
            saved_by="analyst-1",
        )
    assert raised.value.code == "AI_ALIAS_REVIEW_INCOMPLETE"

    reviews = []
    for item in proposal.artifact.alias_drafts:
        if item.resource_type == "dimension":
            reviews.append(
                SemanticAliasReview(
                    resource_type=item.resource_type,
                    resource_id=item.resource_id,
                    aliases=("客户类别",),
                )
            )
        elif item.resource_type == "metric":
            reviews.append(
                SemanticAliasReview(
                    resource_type=item.resource_type,
                    resource_id=item.resource_id,
                    aliases=(),
                )
            )
        else:
            reviews.append(
                SemanticAliasReview(
                    resource_type=item.resource_type,
                    resource_id=item.resource_id,
                    display_name=("华东区" if item.resource_name == "华东" else item.display_name),
                    aliases=(("东区",) if item.resource_name == "华东" else item.aliases),
                )
            )
    saved = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=proposal.etag,
        expected_proposal_hash=proposal.proposal_hash,
        decisions=proposal.decisions,
        alias_reviews=tuple(reviews),
        saved_by="analyst-1",
    )

    result = application.apply_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        expected_proposal_etag=saved.etag,
        expected_proposal_hash=saved.proposal_hash,
        reviewed_by="analyst-1",
    )

    release = result.revision.semantic_spec
    assert next(item for item in release.dimensions if item.name == "客户分群").aliases == (
        "客户类别",
    )
    assert next(item for item in release.metrics if item.name.endswith("数量")).aliases == ()
    assert {item.aliases for item in release.dimension_values} == {
        ("东区",),
        ("华南地区",),
    }
    assert {item.display_name for item in release.dimension_values} == {"华东区", "华南"}
    assert len(profiler.calls) == 1


def test_reclassifying_profiled_dimension_returns_a_new_unreviewed_artifact(
    schema_snapshot,
):
    application = _application(
        schema_snapshot,
        ai_modeller=_CompleteAiModeller(),
        semantic_profiler=_DimensionValueProfiler(),
        dimension_alias_suggester=_DimensionValueAliases(),
        require_evaluation=False,
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    proposal = application.create_ai_modeling_proposal(
        revision_id=revision.id,
        expected_etag=revision.etag,
        created_by="analyst-1",
    )
    decisions = tuple(
        SuggestionDecision(suggestion_id=item.id, accept=False) for item in proposal.suggestions
    )

    regenerated = application.save_ai_modeling_proposal(
        revision_id=revision.id,
        proposal_id=proposal.id,
        expected_proposal_etag=proposal.etag,
        expected_proposal_hash=proposal.proposal_hash,
        decisions=decisions,
        alias_reviews=_alias_reviews(proposal),
        saved_by="analyst-1",
    )

    assert regenerated.reviewed_artifact_hash is None
    assert regenerated.artifact.dimension_values == ()
    assert all(
        item.resource_type != "dimension_value" for item in regenerated.artifact.alias_drafts
    )


def test_exact_model_schema_ai_classification_is_deterministically_materialized(
    schema_snapshot,
):
    application = _application(
        schema_snapshot,
        ai_modeller=AiSemanticModeller(
            model_gateway=_ExactModelSchemaGateway(), workflow="single_call"
        ),
    )
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("orders",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    updated = application.apply_ai_suggestion_run(
        revision_id=revision.id,
        run_id=run.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        decisions=tuple(
            SuggestionDecision(suggestion_id=item.id, accept=True) for item in run.suggestions
        ),
        reviewed_by="analyst-1",
    )

    model = updated.semantic_catalog.models[0]
    assert model.biz_name == "sales_orders"
    assert model.model_detail.measures[0].model_dump(mode="json", by_alias=True) == {
        "name": "订单净金额",
        "agg": "SUM",
        "expr": "net_amount",
        "bizName": "net_amount",
        "isCreateMetric": 1,
        "constraint": None,
        "alias": None,
        "unit": "元",
        # 度量的业务口径。此前 MeasureContract 没有这个字段，AI 按提示词写出的
        # 口径在这一步被整条丢掉，指标定义随之回落成名字本身（同义反复），
        # 别名生成锚在空描述上只能产出名字变体。
        "description": "订单净收入候选度量",
    }
    assert len(updated.semantic_catalog.metrics) == 1
    assert updated.semantic_catalog.metrics[0].name == "订单净金额"
    assert updated.semantic_catalog.metrics[0].biz_name == "net_amount"
    assert updated.semantic_catalog.data_sets == ()


def test_page_cannot_supply_an_unbound_knowledge_manifest(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )

    with pytest.raises(SemanticValidationError) as exc_info:
        application.create_ai_suggestion_run(
            revision_id=revision.id,
            expected_etag=revision.etag,
            manifest_hash="sha256:unbound",
            source=ModelingRunSource.UI,
        )

    assert getattr(exc_info.value, "code", None) == "KNOWLEDGE_SCOPE_REQUIRED"


def test_golden_suite_persistence_is_bound_to_the_current_revision(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    suite = GoldenSuite(
        id="suite-1",
        name="拒答边界",
        project_id="sales",
        cases=(
            GoldenCase(
                id="case-1",
                question="明天天气",
                dataset_ids=(),
                expected_state=QueryState.FAILED,
                expected_error_code="OUT_OF_SCOPE",
            ),
            GoldenCase(
                id="case-2",
                question="客户数量",
                dataset_ids=("customer-analysis",),
                memory_status=MemoryStatus.ENABLED,
                memory_review_result=MemoryReviewResult.POSITIVE,
                expected_state=QueryState.COMPLETED,
                expected_metric_ids=("customer-count",),
                expected_rows=((1,),),
            ),
        ),
    )

    saved = application.save_golden_suite(
        revision_id=revision.id,
        expected_etag=revision.etag,
        expected_schema_snapshot_hash=revision.schema_snapshot_hash,
        suite=suite,
        saved_by="analyst-1",
    )

    assert saved.semantic_spec_hash == revision.semantic_spec.spec_hash
    assert application.list_golden_suites(revision.id) == (saved,)

    with pytest.raises(CatalogError) as raised:
        application.catalog.save_golden_suite(
            saved.model_copy(
                update={
                    "project_id": "another-project",
                    "revision_id": "another-revision",
                }
            )
        )
    assert raised.value.code == "GOLDEN_SUITE_SCOPE_CONFLICT"

    assert application.delete_golden_suite(
        revision_id=revision.id,
        suite_id=suite.id,
    )
    assert application.list_golden_suites(revision.id) == ()


def test_legacy_product_plan_applies_ai_suggestion_without_creating_query_scope(
    schema_snapshot,
):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
        source=ModelingRunSource.UI,
    )

    plan = application.create_modeling_plan(
        revision_id=revision.id,
        expected_etag=revision.etag,
        suggestion_run_id=run.id,
    )

    assert plan.phase is ModelingPlanPhase.REVIEWING_SEMANTICS
    assert plan.queue.summary.informational == 0
    assert all(
        item.source_suggestion_ids for item in (*plan.queue.decisions, *plan.queue.information)
    )
    applied = application.apply_modeling_plan(
        revision_id=revision.id,
        plan_id=plan.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        choices=tuple(
            DecisionChoice(decision_id=item.id, option_id="accept") for item in plan.queue.decisions
        ),
        reviewed_by="analyst-1",
    )

    assert applied.plan.status.value == "applied"
    assert all(item.state.value != "pending" for item in applied.revision.suggestions)
    assert application.get_modeling_run(run.id).status.value == "applied"
    assert applied.revision.semantic_spec.dimensions == ()
    assert applied.revision.semantic_spec.metrics == ()

    blocked_plan = application.create_modeling_plan(
        revision_id=applied.revision.id,
        expected_etag=applied.revision.etag,
    )
    assert blocked_plan.phase is ModelingPlanPhase.BLOCKED
    assert blocked_plan.queue.decisions[0].id == "decision:empty-query-scope"
    summary = application.get_modeling_summary(project_id="sales")
    assert summary.stage == "blocked"
    assert summary.counts.datasets == 0


def test_blocked_legacy_plan_remains_idempotently_addressable(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    blocked_plan = application.create_modeling_plan(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    assert blocked_plan.phase is ModelingPlanPhase.BLOCKED

    rejected = application.apply_modeling_plan(
        revision_id=revision.id,
        plan_id=blocked_plan.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        choices=(
            DecisionChoice(
                decision_id=blocked_plan.queue.decisions[0].id,
                option_id="open_advanced",
            ),
        ),
        reviewed_by="analyst-1",
    )
    repeated = application.create_modeling_plan(
        revision_id=rejected.revision.id,
        expected_etag=rejected.revision.etag,
    )

    assert rejected.revision.etag == revision.etag
    assert repeated.id == rejected.plan.id
    assert repeated.status.value == "applied"


def test_plan_review_rolls_back_revision_when_plan_cas_fails(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    plan = application.create_modeling_plan(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    with application.catalog._engine.begin() as connection:
        connection.execute(
            update(modeling_plans).where(modeling_plans.c.id == plan.id).values(status="applied")
        )

    with pytest.raises(CatalogError, match="concurrently reviewed"):
        application.apply_modeling_plan(
            revision_id=revision.id,
            plan_id=plan.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            choices=tuple(
                DecisionChoice(
                    decision_id=item.id,
                    option_id="open_advanced",
                )
                for item in plan.queue.decisions
            ),
            reviewed_by="analyst-1",
        )

    assert application.get_revision(revision.id).etag == revision.etag


def test_product_plan_cannot_be_replayed_after_revision_changes(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    plan = application.create_modeling_plan(
        revision_id=revision.id,
        expected_etag=revision.etag,
        suggestion_run_id=run.id,
    )
    model = revision.semantic_catalog.models[0]
    changed = application.upsert_catalog_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        model=model.model_copy(update={"description": "人工更新以使建模计划过期"}),
    )

    with pytest.raises(RevisionConflictError, match="stale"):
        application.apply_modeling_plan(
            revision_id=changed.id,
            plan_id=plan.id,
            expected_etag=changed.etag,
            schema_snapshot_hash=changed.schema_snapshot_hash,
            choices=tuple(
                DecisionChoice(decision_id=item.id, option_id="accept")
                for item in plan.queue.decisions
            ),
            reviewed_by="analyst-1",
        )


def test_api_first_import_creates_an_fk_candidate_without_a_suggestion_queue(
    schema_snapshot,
):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    for table_name in ("customers", "orders"):
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table_name,
        )
    plan = application.create_modeling_plan(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    assert revision.suggestions == ()
    assert len(revision.semantic_spec.relations) == 1
    assert revision.semantic_catalog is not None
    assert revision.semantic_catalog.model_relations[0].knowflow_cardinality is None
    assert plan.phase is ModelingPlanPhase.BLOCKED
    assert not any(item.kind == "relation_cardinality" for item in plan.queue.decisions)


def test_product_projection_http_contract(schema_snapshot):
    application = _application(schema_snapshot, ai_modeller=_AiModeller())
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers",)},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="customers",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id,
        expected_etag=revision.etag,
    )
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }

    scope = _ok(
        client.get(
            "/v1/analytics/projects/sales/datasources/default/scope-recommendations",
            headers=headers,
            params={"schema_name": "sales"},
        )
    )
    assert scope["groups"][0]["tables"] == ["customers", "orders"]

    plan = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision.id}/modeling-plans",
            headers=headers,
            json={"expected_etag": revision.etag, "suggestion_run_id": run.id},
        )
    )
    queue = _ok(
        client.get(
            f"/v1/analytics/projects/sales/revisions/{revision.id}"
            f"/modeling-plans/{plan['id']}/decisions",
            headers=headers,
        )
    )
    assert queue["revision_etag"] == revision.etag
    assert queue["decisions"]

    applied = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision.id}"
            f"/modeling-plans/{plan['id']}/decisions:apply",
            headers=headers,
            json={
                "expected_etag": revision.etag,
                "schema_snapshot_hash": revision.schema_snapshot_hash,
                "choices": [
                    {"decision_id": item["id"], "option_id": "accept"}
                    for item in queue["decisions"]
                ],
            },
        )
    )
    assert applied["plan"]["status"] == "applied"

    summary = _ok(
        client.get(
            "/v1/analytics/projects/sales/modeling-summary",
            headers=headers,
        )
    )
    assert summary["revision_id"] == revision.id


def test_product_api_builds_validates_publishes_and_reloads_catalog(
    schema_snapshot,
):
    application = _application(
        schema_snapshot,
        require_evaluation=False,
        require_quality_report=False,
    )
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }

    _ok(
        client.post(
            "/v1/analytics/projects",
            headers=headers,
            json={"name": "销售分析", "project_id": "sales"},
        )
    )
    snapshot = _ok(
        client.post(
            "/v1/analytics/projects/sales/schema-snapshots",
            headers=headers,
            json={
                "schemas": ["sales"],
                "selected_tables": {"sales": ["customers", "orders"]},
            },
        )
    )
    revision = _ok(
        client.post(
            "/v1/analytics/projects/sales/revisions",
            headers=headers,
            json={"schema_snapshot_id": snapshot["id"]},
        )
    )
    for table_name in ("customers", "orders"):
        revision = _ok(
            client.post(
                f"/v1/analytics/projects/sales/revisions/{revision['id']}/models:from-table",
                headers=headers,
                json={
                    "expected_etag": revision["etag"],
                    "schema_snapshot_hash": revision["schema_snapshot_hash"],
                    "schema_name": "sales",
                    "table_name": table_name,
                },
            )
        )

    decisions = [
        {
            "suggestion_id": item["id"],
            "accept": item["source"] == "database_constraint",
        }
        for item in revision["suggestions"]
    ]
    revision = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/decisions",
            headers=headers,
            json={
                "expected_etag": revision["etag"],
                "schema_snapshot_hash": revision["schema_snapshot_hash"],
                "decisions": decisions,
            },
        )
    )
    models = {item["table"]: item["id"] for item in revision["semantic_spec"]["models"]}
    orders_model_id = models["orders"]
    relation_candidate = revision["semantic_catalog"]["modelRelations"][0]
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        f"catalog/relations/{relation_candidate['id']}",
        "relation",
        {
            **relation_candidate,
            "knowflowCardinality": "many_to_one",
        },
    )

    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        f"catalog/models/{orders_model_id}/dimensions/region",
        "dimension",
        {
            "name": "区域",
            "type": "categorical",
            "expr": "region",
            "bizName": "region",
            "dataType": "TEXT",
            "isCreateDimension": 0,
            "description": "订单所属区域",
        },
    )
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        f"catalog/models/{orders_model_id}/dimensions/order_date",
        "dimension",
        {
            "name": "下单日期",
            "type": "partition_time",
            "expr": "order_date",
            "bizName": "order_date",
            "dataType": "DATE",
            "typeParams": {"isPrimary": "true", "timeGranularity": "day"},
            "isCreateDimension": 0,
            "description": "订单业务日期",
        },
    )
    revenue_measure = {
        "name": "净收入",
        "agg": "SUM",
        "expr": "net_amount",
        "bizName": "net_amount",
        "isCreateMetric": 0,
        "alias": "收入,销售额",
        "unit": "元",
    }
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        f"catalog/models/{orders_model_id}/measures/net_amount",
        "measure",
        revenue_measure,
    )
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        "catalog/dimensions/dimension_region",
        "dimension",
        {
            "id": "dimension_region",
            "name": "区域",
            "bizName": "region",
            "description": "订单所属区域",
            "modelId": orders_model_id,
            "type": "categorical",
            "expr": "region",
            "semanticType": "CATEGORY",
            "alias": "大区",
            "dataType": "TEXT",
        },
    )
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        "catalog/dimensions/dimension_order_date",
        "dimension",
        {
            "id": "dimension_order_date",
            "name": "下单日期",
            "bizName": "order_date",
            "description": "订单业务日期",
            "modelId": orders_model_id,
            "type": "partition_time",
            "expr": "order_date",
            "semanticType": "DATE",
            "alias": "订单日期",
            "dataType": "DATE",
            "typeParams": {"isPrimary": "true", "timeGranularity": "day"},
        },
    )
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        "catalog/metrics/metric_net_revenue",
        "metric",
        {
            "id": "metric_net_revenue",
            "name": "净收入",
            "bizName": "net_revenue",
            "description": "订单净金额合计",
            "modelId": orders_model_id,
            "alias": "收入,销售额",
            "metricDefineType": "MEASURE",
            "metricDefineByMeasureParams": {
                "expr": "net_amount",
                "measures": [revenue_measure],
            },
        },
    )
    generated_scope = next(
        item
        for item in revision["semantic_spec"]["datasets"]
        if "metric_net_revenue" in item["metric_ids"]
    )
    assert {"dimension_region", "dimension_order_date"}.issubset(generated_scope["dimension_ids"])

    validated = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/validate",
            headers=headers,
        )
    )
    assert validated["state"] == "validated"
    published = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/publish",
            headers=headers,
            json={"confirmation": "publish"},
        )
    )
    loaded = _ok(
        client.get(
            f"/v1/analytics/projects/sales/releases/{published['release']['id']}",
            headers=headers,
        )
    )

    assert loaded["release"]["spec_hash"] == published["release"]["spec_hash"]
    catalog = loaded["release"]["modeling_catalog"]
    assert catalog["contractVersion"] == "knowflow-modeling-v1"
    assert (
        next(item for item in catalog["metrics"] if item["id"] == "metric_net_revenue")[
            "metricDefineType"
        ]
        == "MEASURE"
    )
    assert any(
        "metric_net_revenue"
        in {
            metric_id
            for config in item["dataSetDetail"]["dataSetModelConfigs"]
            for metric_id in config["metrics"]
        }
        for item in catalog["dataSets"]
    )


def test_sql_model_contract_round_trips_through_the_same_http_model_resource(
    schema_snapshot,
):
    application = _application(
        schema_snapshot,
        require_evaluation=False,
        require_quality_report=False,
    )
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }
    _ok(
        client.post(
            "/v1/analytics/projects",
            headers=headers,
            json={"name": "销售分析", "project_id": "sales"},
        )
    )
    snapshot = _ok(
        client.post(
            "/v1/analytics/projects/sales/schema-snapshots",
            headers=headers,
            json={"schemas": ["sales"]},
        )
    )
    revision = _ok(
        client.post(
            "/v1/analytics/projects/sales/revisions",
            headers=headers,
            json={"schema_snapshot_id": snapshot["id"]},
        )
    )
    revision = _ok(
        client.put(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/catalog/models/sql_orders",
            headers=headers,
            json={
                "expected_etag": revision["etag"],
                "schema_snapshot_hash": revision["schema_snapshot_hash"],
                "model": {
                    "id": "sql_orders",
                    "name": "订单 SQL 模型",
                    "bizName": "sql_orders",
                    "description": "验证 SQL 模型合同无损保存",
                    "modelDetail": {
                        "queryType": "sql_query",
                        "dbType": "postgresql",
                        "sqlQuery": (
                            "SELECT id, net_amount FROM sales.orders WHERE region = {{region}}"
                        ),
                        "fields": [
                            {"fieldName": "id", "dataType": "BIGINT"},
                            {"fieldName": "net_amount", "dataType": "NUMERIC(18,2)"},
                        ],
                        "sqlVariables": [
                            {
                                "name": "region",
                                "valueType": "STRING",
                                "defaultValues": ["华东"],
                            }
                        ],
                    },
                },
            },
        )
    )
    catalog = _ok(
        client.get(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/catalog",
            headers=headers,
        )
    )

    assert catalog["models"][0]["modelDetail"]["sqlVariables"][0]["valueType"] == ("STRING")
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        "catalog/dimensions/dimension_sql_order_id",
        "dimension",
        {
            "id": "dimension_sql_order_id",
            "name": "订单编号",
            "bizName": "order_id",
            "description": "SQL 模型中的订单编号",
            "modelId": "sql_orders",
            "type": "categorical",
            "expr": "id",
            "semanticType": "CATEGORY",
            "dataType": "BIGINT",
        },
    )
    revision = _put_catalog_resource(
        client,
        headers,
        revision,
        "catalog/metrics/metric_sql_net_amount",
        "metric",
        {
            "id": "metric_sql_net_amount",
            "name": "净额",
            "bizName": "net_amount",
            "description": "SQL 模型净额合计",
            "modelId": "sql_orders",
            "metricDefineType": "FIELD",
            "metricDefineByFieldParams": {
                "expr": "SUM(net_amount)",
                "fields": [{"fieldName": "net_amount"}],
            },
        },
    )
    generated_scope = next(
        item
        for item in revision["semantic_spec"]["datasets"]
        if "metric_sql_net_amount" in item["metric_ids"]
    )
    assert "dimension_sql_order_id" in generated_scope["dimension_ids"]

    validated = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/validate",
            headers=headers,
        )
    )
    assert validated["state"] == "validated"
    published = _ok(
        client.post(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/publish",
            headers=headers,
            json={"confirmation": "publish"},
        )
    )
    loaded = _ok(
        client.get(
            f"/v1/analytics/projects/sales/releases/{published['release']['id']}",
            headers=headers,
        )
    )
    sql_model = loaded["release"]["modeling_catalog"]["models"][0]
    assert sql_model["modelDetail"]["queryType"] == "sql_query"
    assert sql_model["modelDetail"]["sqlVariables"][0]["defaultValues"] == ["华东"]


def test_physical_field_resource_cannot_drift_from_the_schema_snapshot(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(project_id="sales", schemas=("sales",))
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    model = next(item for item in revision.semantic_catalog.models if item.name == "orders")
    field = next(item for item in model.model_detail.fields if item.field_name == "net_amount")

    with pytest.raises(SemanticValidationError) as exc_info:
        application.upsert_catalog_model_field(
            revision_id=revision.id,
            model_id=model.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            field=field.model_copy(update={"data_type": "TEXT"}),
        )

    assert exc_info.value.code == "MODEL_FIELDS_DIFFER_FROM_SNAPSHOT"


def test_exact_model_resource_cannot_bind_one_physical_table_twice(schema_snapshot):
    application = _application(schema_snapshot)
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(project_id="sales", schemas=("sales",))
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="orders",
    )
    existing = revision.semantic_catalog.models[0]

    with pytest.raises(SemanticValidationError) as exc_info:
        application.upsert_catalog_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            model=existing.model_copy(
                update={
                    "id": "duplicate_orders",
                    "name": "重复订单模型",
                    "biz_name": "duplicate_orders",
                }
            ),
        )

    assert exc_info.value.code == "MODEL_SOURCE_ALREADY_BOUND"


def _put_catalog_resource(client, headers, revision, path, key, resource):
    return _ok(
        client.put(
            f"/v1/analytics/projects/sales/revisions/{revision['id']}/{path}",
            headers=headers,
            json={
                "expected_etag": revision["etag"],
                "schema_snapshot_hash": revision["schema_snapshot_hash"],
                key: resource,
            },
        )
    )


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()
