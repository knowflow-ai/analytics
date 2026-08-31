from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.topology import build_topology, related_payload


def _col(name, dtype="TEXT", *, pk=False):
    return SchemaColumnSnapshot(
        name=name, data_type=dtype, nullable=not pk, comment="", ordinal_position=0, primary_key=pk
    )


def _fk(column, table, remote="id"):
    return ForeignKeySnapshot(
        constrained_columns=(column,),
        referred_schema="s",
        referred_table=table,
        referred_columns=(remote,),
    )


# customers ← orders → products；audit_log 谁也不连
_SNAPSHOT = SchemaSnapshot.create(
    database_name="db",
    captured_at=datetime(2026, 8, 23, tzinfo=UTC),
    tables=(
        TableSnapshot(
            schema_name="s",
            name="orders",
            columns=(
                _col("id", "BIGINT", pk=True),
                _col("customer_id", "BIGINT"),
                _col("product_id", "BIGINT"),
                _col("amount", "NUMERIC"),
            ),
            foreign_keys=(_fk("customer_id", "customers"), _fk("product_id", "products")),
        ),
        TableSnapshot(
            schema_name="s",
            name="customers",
            columns=(_col("id", "BIGINT", pk=True), _col("segment")),
        ),
        TableSnapshot(
            schema_name="s",
            name="products",
            columns=(
                _col("id", "BIGINT", pk=True),
                _col("category"),
                _col("secret_cost", "NUMERIC"),
            ),
        ),
        TableSnapshot(
            schema_name="s",
            name="audit_log",
            columns=(_col("id", "BIGINT", pk=True), _col("payload")),
        ),
    ),
)


def test_dimension_tables_are_ordered_before_the_fact_that_references_them():
    """事实表的 customer_id 被命名时「客户」必须已经存在，跨表命名才能累积。"""

    topology = build_topology(_SNAPSHOT)
    order = {key[1]: item.order for key, item in topology.items()}

    assert order["customers"] < order["orders"]
    assert order["products"] < order["orders"]
    assert topology[("s", "orders")].out_degree == 2
    assert topology[("s", "customers")].in_degree == 1


def test_related_payload_carries_join_columns_but_not_the_other_tables_columns():
    orders = build_topology(_SNAPSHOT)[("s", "orders")]
    payload = related_payload(orders)

    assert {item["table"] for item in payload} == {"s.customers", "s.products"}
    customers = next(item for item in payload if item["table"] == "s.customers")
    assert customers["joinColumns"] == [{"local": "customer_id", "remote": "id"}]
    assert customers["direction"] == "references"
    assert "secret_cost" not in str(payload)
    assert "audit_log" not in str(payload)


def test_a_dimension_sees_who_references_it():
    customers = build_topology(_SNAPSHOT)[("s", "customers")]
    payload = related_payload(customers)
    assert payload == [
        {
            "table": "s.orders",
            "direction": "referenced_by",
            "joinColumns": [{"local": "id", "remote": "customer_id"}],
        },
    ]


def test_the_prompt_no_longer_ships_every_other_tables_column_list():
    """此前 OtherRelatedDBSchema 带上其它所有表的完整列清单，N 表 = N 次调用各带 N 表。"""

    orders = next(t for t in _SNAPSHOT.tables if t.name == "orders")
    content = AiSemanticModeller._messages(table=orders, snapshot=_SNAPSHOT)[1]["content"]

    assert "OtherRelatedDBSchema" not in content
    assert "RelatedTables=" in content
    assert "s.customers" in content and "customer_id" in content
    # 关联表的列不在；完全无关的表不在
    assert "secret_cost" not in content
    assert "segment" not in content
    assert "audit_log" not in content
