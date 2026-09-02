from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.quality import (
    PostgreSqlModelingQualityProfiler,
    QualityStatus,
)
from tests.support import create_sales_fixture


def _quality_revision(sales_release, *, revision_id: str) -> ModelingRevision:
    release = sales_release.model_copy(
        update={
            "fields": tuple(
                field.model_copy(
                    update={
                        "identifier_type": (
                            "primary"
                            if field.id in {"orders.id", "customers.id", "order_items.id"}
                            else "foreign"
                            if field.id in {"orders.customer_id", "order_items.order_id"}
                            else None
                        )
                    }
                )
                for field in sales_release.fields
            )
        }
    )
    return ModelingRevision(
        id=revision_id,
        project_id=release.project_id,
        schema_snapshot_hash="sha256:quality-postgres",
        etag=9,
        semantic_spec=release,
    )


@pytest.mark.postgres
def test_m3_profiles_grain_relations_metrics_and_dataset_matrix_against_postgres(
    sales_release,
):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    revision = _quality_revision(
        sales_release,
        revision_id="revision-quality-postgres",
    )
    engine = create_engine(database_url)
    executor = SqlExecutor(database_url)
    profiler = PostgreSqlModelingQualityProfiler(engine, executor)
    try:
        report = profiler.profile(revision)
    finally:
        executor.close()
        engine.dispose()

    grains = {item.model_id: item for item in report.model_grains}
    assert grains["orders"].total_rows == 3
    assert grains["orders"].uniqueness_rate == 1.0
    assert grains["orders"].null_rate == 0.0
    assert grains["orders"].status is QualityStatus.PASSED

    relations = {item.relation_id: item for item in report.relations}
    assert relations["orders_customer"].status is QualityStatus.PASSED
    assert relations["orders_customer"].left_join_coverage == 1.0
    assert relations["orders_customer"].max_left_key_multiplicity == 2
    assert relations["orders_customer"].max_right_key_multiplicity == 1
    assert relations["orders_items"].status is QualityStatus.WARNING
    assert relations["orders_items"].orphan_left_rows == 1
    assert relations["orders_items"].right_fanout_factor == 1.0

    previews = {item.metric_id: item for item in report.metric_previews}
    assert previews["net_revenue"].rows == ((380,),)
    assert previews["order_count"].rows == ((3,),)
    assert all(item.status is QualityStatus.PENDING_REVIEW for item in previews.values())
    assert any(
        item.metric_id == "net_revenue"
        and item.dimension_id == "product"
        and item.reason_code == "FANOUT_RISK"
        for item in report.reachability
    )


@pytest.mark.postgres
def test_relation_coverage_counts_null_join_keys_as_unmatched_rows(sales_release):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE analytics_v0.orders ALTER COLUMN customer_id DROP NOT NULL")
        )
        connection.execute(
            text(
                """
                INSERT INTO analytics_v0.orders
                    (id, customer_id, region, channel, net_amount, refund_amount, order_date)
                VALUES
                    (4, NULL, '华北', '直营', 0, 0, DATE '2026-08-04')
                """
            )
        )
    revision = _quality_revision(
        sales_release,
        revision_id="revision-quality-null-join-key",
    )
    executor = SqlExecutor(database_url)
    profiler = PostgreSqlModelingQualityProfiler(engine, executor)
    try:
        report = profiler.profile(revision)
    finally:
        executor.close()
        engine.dispose()

    relation = next(item for item in report.relations if item.relation_id == "orders_customer")
    assert relation.left_rows == 4
    assert relation.matched_left_rows == 3
    assert relation.orphan_left_rows == 1
    assert relation.left_join_coverage == 0.75
    assert relation.status is QualityStatus.WARNING
