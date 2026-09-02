from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.modeling.catalog_contracts import (
    DataSetContract,
    DataSetDetailContract,
    DataSetModelConfigContract,
    DimensionContract,
    ModelRelationContract,
)
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.semantic.index import EmbeddingBatch
from tests.support import create_sales_fixture


class _EmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="api-first-test",
            dimension=2,
            vectors=tuple((1.0, 0.0) for _item in texts),
        )


@pytest.mark.postgres
def test_api_first_modeling_uses_explicit_table_and_dataset_commands():
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)

    catalog_engine = create_engine("sqlite+pysqlite:///:memory:")
    datasource_engine = create_engine(database_url)
    catalog = CatalogStore(catalog_engine)
    catalog.create_schema()
    executor = SqlExecutor(database_url)
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=PostgreSqlIntrospector(datasource_engine),
        executor=executor,
        embedding_gateway=_EmbeddingGateway(),
    )

    try:
        application.create_project(project_id="api-first-sales", name="销售分析")
        assert "analytics_v0" in application.list_datasource_schemas(project_id="api-first-sales")
        table_names = {
            table.name
            for table in application.list_datasource_tables(
                project_id="api-first-sales",
                schema_name="analytics_v0",
            )
        }
        assert {"customers", "orders"}.issubset(table_names)

        snapshot = application.create_schema_snapshot(
            project_id="api-first-sales",
            schemas=("analytics_v0",),
            selected_tables={"analytics_v0": ("customers", "orders")},
        )
        revision = application.create_empty_revision(
            project_id="api-first-sales",
            schema_snapshot_id=snapshot.id,
        )
        assert revision.semantic_spec.models == ()
        assert revision.semantic_spec.datasets == ()

        for table_name in ("customers", "orders"):
            revision = application.add_table_model(
                revision_id=revision.id,
                expected_etag=revision.etag,
                schema_snapshot_hash=revision.schema_snapshot_hash,
                schema_name="analytics_v0",
                table_name=table_name,
            )
        assert {model.table for model in revision.semantic_spec.models} == {
            "customers",
            "orders",
        }
        assert revision.semantic_spec.datasets == ()

        assert revision.suggestions == ()
        assert revision.semantic_catalog is not None
        relation_candidate = revision.semantic_catalog.model_relations[0]
        revision = application.upsert_catalog_relation(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            relation=ModelRelationContract.model_validate(
                {
                    **relation_candidate.model_dump(mode="python"),
                    "knowflow_cardinality": "many_to_one",
                }
            ),
        )
        customers_model = next(
            model for model in revision.semantic_spec.models if model.table == "customers"
        )
        customer_identifier = DimensionContract(
            id="customer_identifier",
            name="客户编号",
            biz_name="customer_identifier",
            model_id=customers_model.id,
            type="identifier",
            expr="id",
            semantic_type="ID",
            data_type="BIGINT",
        )
        revision = application.upsert_catalog_dimension(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            dimension=customer_identifier,
        )

        dataset = DataSetContract(
            id="sales_dataset",
            name="销售分析",
            biz_name="sales_dataset",
            data_set_detail=DataSetDetailContract(
                data_set_model_configs=(
                    DataSetModelConfigContract(
                        id=customers_model.id,
                        dimensions=(customer_identifier.id,),
                    ),
                )
            ),
        )
        revision = application.upsert_catalog_dataset(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            data_set=dataset,
        )
        validated = application.validate_revision(revision.id)

        assert validated.state.value == "validated"
        assert validated.semantic_spec.datasets[0].id == dataset.id
        assert validated.semantic_spec.datasets[0].dimension_ids == (customer_identifier.id,)
    finally:
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()
