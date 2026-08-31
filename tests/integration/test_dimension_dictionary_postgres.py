from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.execution.postgres import PostgresExecutor
from knowflow_analytics.modeling.catalog_contracts import (
    ModelDimensionContract,
    ModelDimensionType,
)
from knowflow_analytics.modeling.contracts import (
    DimensionDictionaryStatus,
    DimensionValueDecision,
)
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.modeling.profiler import PostgreSqlSemanticProfiler
from knowflow_analytics.modeling.revision import RevisionConflictError
from knowflow_analytics.semantic.index import EmbeddingBatch


class _EmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="dictionary-test",
            dimension=2,
            vectors=tuple((1.0, 0.0) for _item in texts),
        )


@pytest.mark.postgres
def test_dimension_dictionary_preview_is_reviewed_before_catalog_write():
    """Holdout coverage for the dimension-dictionary catalog workflow."""

    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    datasource_engine = create_engine(database_url)
    _create_dictionary_holdout(datasource_engine)
    catalog_engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(catalog_engine)
    catalog.create_schema()
    executor = PostgresExecutor(database_url)
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=PostgreSqlIntrospector(datasource_engine),
        semantic_profiler=PostgreSqlSemanticProfiler(datasource_engine),
        executor=executor,
        embedding_gateway=_EmbeddingGateway(),
    )

    try:
        application.create_project(project_id="dictionary-holdout", name="工单分析")
        snapshot = application.create_schema_snapshot(
            project_id="dictionary-holdout",
            schemas=("analytics_dictionary_holdout",),
            selected_tables={"analytics_dictionary_holdout": ("tickets",)},
        )
        revision = application.create_empty_revision(
            project_id="dictionary-holdout",
            schema_snapshot_id=snapshot.id,
        )
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="analytics_dictionary_holdout",
            table_name="tickets",
        )
        model = revision.semantic_catalog.models[0]
        reviewed_model = model.model_copy(
            update={
                "model_detail": model.model_detail.model_copy(
                    update={
                        "dimensions": (
                            ModelDimensionContract(
                                name="优先级",
                                type=ModelDimensionType.CATEGORICAL,
                                expr="priority_code",
                                biz_name="priority_code",
                                data_type="TEXT",
                                is_create_dimension=1,
                            ),
                        )
                    }
                )
            }
        )
        reviewed = application.upsert_catalog_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            model=reviewed_model,
        )
        priority_dimension = next(
            item
            for item in reviewed.semantic_spec.dimensions
            if item.model_id == model.id and item.name == "优先级"
        )
        # Product extension: a complete deterministic
        # dictionary fetch is applied when the Dimension is materialized; no
        # analysis topic or raw-value confirmation is required.
        assert {
            item.value
            for item in reviewed.semantic_spec.dimension_values
            if item.dimension_id == priority_dimension.id
        } == {"L0", "L1", "L2"}
        assert all(
            item.display_name == item.value and item.aliases == () and item.enabled
            for item in reviewed.semantic_spec.dimension_values
            if item.dimension_id == priority_dimension.id
        )
        complete_profile = PostgreSqlSemanticProfiler(datasource_engine).profile(
            snapshot=snapshot,
            semantic_spec=reviewed.semantic_spec,
            dimension_ids=(priority_dimension.id,),
        )

        assert complete_profile.dimensions[0].source_rows_truncated is False
        assert complete_profile.dimensions[0].sampled_rows == 4
        assert {item.value: item.frequency for item in complete_profile.dimensions[0].values} == {
            "L0": 2,
            "L1": 1,
            "L2": 1,
        }

        preview = application.generate_dimension_dictionary_preview(
            revision_id=reviewed.id,
            expected_etag=reviewed.etag,
            schema_snapshot_hash=reviewed.schema_snapshot_hash,
            dimension_ids=(priority_dimension.id,),
        )

        assert catalog.get_revision(reviewed.id) == reviewed
        assert preview.status is DimensionDictionaryStatus.COMPLETED
        decisions = tuple(
            DimensionValueDecision(
                candidate_id=item.id,
                accept=item.value != "L2",
                display_name="紧急" if item.value == "L0" else None,
                aliases=("最高级",) if item.value == "L0" else None,
            )
            for item in preview.candidates
        )
        with pytest.raises(SemanticValidationError) as incomplete_review:
            application.apply_dimension_dictionary_preview(
                preview_id=preview.id,
                expected_etag=reviewed.etag,
                schema_snapshot_hash=reviewed.schema_snapshot_hash,
                decisions=decisions[:-1],
                reviewed_by="holdout-owner",
            )
        assert incomplete_review.value.code == "INCOMPLETE_DICTIONARY_REVIEW"
        result = application.apply_dimension_dictionary_preview(
            preview_id=preview.id,
            expected_etag=reviewed.etag,
            schema_snapshot_hash=reviewed.schema_snapshot_hash,
            decisions=decisions,
            reviewed_by="holdout-owner",
        )

        values = {
            item.value: item
            for item in result.revision.semantic_catalog.dimension_values
            if item.dimension_id == priority_dimension.id
        }
        assert set(values) == {"L0", "L1"}
        assert values["L0"].display_name == "紧急"
        assert values["L0"].aliases == ("最高级",)
        assert result.preview.status is DimensionDictionaryStatus.APPLIED
        assert result.preview.reviewed_by == "holdout-owner"
        with pytest.raises(RevisionConflictError, match="already reviewed"):
            application.apply_dimension_dictionary_preview(
                preview_id=preview.id,
                expected_etag=result.revision.etag,
                schema_snapshot_hash=reviewed.schema_snapshot_hash,
                decisions=decisions,
                reviewed_by="holdout-owner",
            )
    finally:
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()


def _create_dictionary_holdout(engine) -> None:
    statements = (
        "CREATE SCHEMA IF NOT EXISTS analytics_dictionary_holdout",
        "DROP TABLE IF EXISTS analytics_dictionary_holdout.tickets",
        """
        CREATE TABLE analytics_dictionary_holdout.tickets (
            ticket_id bigint PRIMARY KEY,
            priority_code text NOT NULL,
            workflow_state text NOT NULL,
            handling_minutes numeric NOT NULL
        )
        """,
        """
        INSERT INTO analytics_dictionary_holdout.tickets VALUES
          (1, 'L0', 'OPEN', 10),
          (2, 'L0', 'CLOSED', 20),
          (3, 'L1', 'OPEN', 30),
          (4, 'L2', 'PENDING', 40)
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
