from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import AnalysisTopicRouteSpec
from knowflow_analytics.execution import PostgresExecutor
from knowflow_analytics.query.contracts import QueryRequest, QueryState
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator
from tests.support import create_sales_fixture


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _AverageGateway:
    def generate_json(self, **_kwargs):
        return {
            "thought": "用净收入总额除以受治理订单数",
            "sql": ('SELECT SUM("净收入") / NULLIF(COUNT(*), 0) AS "平均净收入" FROM "销售经营"'),
        }


@pytest.mark.postgres
def test_runs_natural_language_through_fixed_pipeline_against_postgres(sales_release, sales_index):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    executor = PostgresExecutor(database_url)
    try:
        service = AnalyticsQueryService(
            releases=_ReleaseProvider(sales_release, sales_index),
            orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
            translator=SemanticTranslator(),
            executor=executor,
        )
        response = service.query(
            QueryRequest(
                project_id="sales",
                question="本季度华东各区域净收入",
                dataset_ids=("sales_dataset",),
                include_debug_sql=True,
            ),
            now=datetime(2026, 8, 13, tzinfo=UTC),
        )
    finally:
        executor.close()

    assert response.state is QueryState.COMPLETED
    assert response.data.rows == (("华东", 300),)
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert response.semantic_query.dimension_ids == ("region",)
    assert response.physical_sql is not None


@pytest.mark.postgres
def test_executes_textual_calculation_without_query_struct_collapse(
    sales_release,
    sales_index,
):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders",),
            "dimension_ids": ("region", "channel", "order_date"),
        }
    )
    release = sales_release.model_copy(
        update={
            "datasets": (dataset,),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=dataset.id,
                    root_model_id="orders",
                    default_count_metric_id="order_count",
                ),
            ),
        }
    )
    executor = PostgresExecutor(database_url)
    try:
        service = AnalyticsQueryService(
            releases=_ReleaseProvider(release, sales_index),
            orchestrator=CandidateOrchestrator(
                mapper=SemanticMapper(),
                llm_parser=LlmS2SqlParser(_AverageGateway()),
            ),
            translator=SemanticTranslator(),
            executor=executor,
        )
        response = service.query(
            QueryRequest(
                project_id="sales",
                question="平均每笔订单的净收入是多少",
                dataset_ids=(dataset.id,),
                include_debug_sql=True,
            )
        )
    finally:
        executor.close()

    assert response.state is QueryState.COMPLETED
    assert float(response.data.rows[0][0]) == pytest.approx(380 / 3)
    assert response.data.columns == ("平均净收入",)
    assert response.physical_sql is not None
    assert "COUNT(DISTINCT" in response.physical_sql.upper()
