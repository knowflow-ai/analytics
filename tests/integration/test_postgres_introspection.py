from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.modeling.introspector import (
    SchemaIntrospectionError,
    SchemaIntrospector,
)


@pytest.mark.postgres
def test_scans_columns_keys_and_foreign_keys_from_postgres():
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    engine = create_engine(database_url)
    try:
        _create_fixture(engine)
        snapshot = SchemaIntrospector(engine).scan(
            schemas=("analytics_modeling_v0",),
            selected_tables={"analytics_modeling_v0": ("customers", "orders")},
        )
    finally:
        engine.dispose()

    orders = next(table for table in snapshot.tables if table.name == "orders")
    assert next(column for column in orders.columns if column.name == "id").primary_key
    assert orders.foreign_keys[0].referred_table == "customers"
    assert orders.foreign_keys[0].constrained_columns == ("customer_id",)


@pytest.mark.postgres
def test_scan_rejects_selected_tables_outside_the_declared_schema_scope():
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    engine = create_engine(database_url)
    try:
        with pytest.raises(SchemaIntrospectionError) as exc_info:
            SchemaIntrospector(engine).scan(
                schemas=("analytics_modeling_v0",),
                selected_tables={"another_schema": ("orders",)},
            )
    finally:
        engine.dispose()

    assert exc_info.value.code == "UNKNOWN_SCHEMA_SCOPE"


def _create_fixture(engine) -> None:
    statements = (
        "CREATE SCHEMA IF NOT EXISTS analytics_modeling_v0",
        "DROP TABLE IF EXISTS analytics_modeling_v0.orders",
        "DROP TABLE IF EXISTS analytics_modeling_v0.customers",
        "CREATE TABLE analytics_modeling_v0.customers (id bigint PRIMARY KEY, name text)",
        """
        CREATE TABLE analytics_modeling_v0.orders (
          id bigint PRIMARY KEY,
          customer_id bigint NOT NULL REFERENCES analytics_modeling_v0.customers(id),
          amount numeric(18, 2) NOT NULL,
          created_at timestamp NOT NULL
        )
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
