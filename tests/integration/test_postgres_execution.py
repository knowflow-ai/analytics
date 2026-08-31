from __future__ import annotations

import os

import pytest

from knowflow_analytics.contracts import QueryOrder, SemanticQuery, SortDirection
from knowflow_analytics.execution import PostgresExecutor
from knowflow_analytics.semantic import SemanticTranslator
from tests.support import create_sales_fixture


@pytest.mark.postgres
def test_executes_translated_query_against_postgres(sales_release):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
            order_by=(QueryOrder(element_id="net_revenue", direction=SortDirection.DESC),),
            limit=10,
        ),
    )
    executor = PostgresExecutor(database_url)
    try:
        result = executor.execute(query=physical, release=sales_release)
        plan = executor.explain(query=physical, release=sales_release)
    finally:
        executor.close()

    assert result.columns == ("region", "net_revenue")
    assert result.rows == (("华东", 300), ("华南", 80))
    assert result.truncated is False
    assert isinstance(plan, list)


@pytest.mark.postgres
def test_reports_when_postgres_results_are_truncated(sales_release):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    physical = SemanticTranslator().translate(
        release=sales_release,
        query=SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
            limit=1,
        ),
    )
    executor = PostgresExecutor(database_url)
    try:
        result = executor.execute(query=physical, release=sales_release)
    finally:
        executor.close()

    assert result.row_count == 1
    assert result.truncated is True
