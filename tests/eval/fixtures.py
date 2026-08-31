"""评测基线用的 schema、画像与人工标注。

挑的是建模最容易错的那些列：年份 / 状态码 / 邮编被 SUM、比率被 SUM、维度表里的
数值被当度量、布尔、近唯一的业务单号。标注是一个有经验的建模者会给的答案。
"""

from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile


def _c(name, dtype, *, pk=False, comment=""):
    return SchemaColumnSnapshot(
        name=name,
        data_type=dtype,
        nullable=not pk,
        comment=comment,
        ordinal_position=0,
        primary_key=pk,
    )


def _fk(col, table):
    return ForeignKeySnapshot(
        constrained_columns=(col,),
        referred_schema="shop",
        referred_table=table,
        referred_columns=("id",),
    )


SNAPSHOT = SchemaSnapshot.create(
    database_name="shop",
    captured_at=datetime(2026, 8, 23, tzinfo=UTC),
    tables=(
        TableSnapshot(
            schema_name="shop",
            name="customers",
            columns=(
                _c("id", "BIGINT", pk=True),
                _c("customer_no", "VARCHAR(32)"),
                _c("segment", "VARCHAR(16)"),
                _c("vip_level", "SMALLINT"),
                _c("age", "INTEGER"),
                _c("city", "VARCHAR(64)"),
                _c("registered_at", "TIMESTAMP"),
                _c("is_active", "BOOLEAN"),
            ),
        ),
        TableSnapshot(
            schema_name="shop",
            name="products",
            columns=(
                _c("id", "BIGINT", pk=True),
                _c("sku", "VARCHAR(32)"),
                _c("category", "VARCHAR(32)"),
                _c("list_price", "NUMERIC(12,2)"),
                _c("weight_kg", "NUMERIC(8,3)"),
                _c("shelf_life_days", "INTEGER"),
            ),
        ),
        TableSnapshot(
            schema_name="shop",
            name="orders",
            columns=(
                _c("id", "BIGINT", pk=True),
                _c("order_no", "VARCHAR(32)"),
                _c("customer_id", "BIGINT"),
                _c("product_id", "BIGINT"),
                _c("order_date", "DATE"),
                _c("year", "INTEGER"),
                _c("month", "INTEGER"),
                _c("status_code", "SMALLINT"),
                _c("channel", "VARCHAR(16)"),
                _c("zip_code", "INTEGER"),
                _c("qty", "INTEGER"),
                _c("unit_price", "NUMERIC(12,2)"),
                _c("gross_amount", "NUMERIC(18,2)"),
                _c("discount_rate", "NUMERIC(5,4)"),
                _c("net_amount", "NUMERIC(18,2)"),
                _c("refund_amount", "NUMERIC(18,2)"),
                _c("is_gift", "BOOLEAN"),
                _c("created_at", "TIMESTAMP"),
            ),
            foreign_keys=(_fk("customer_id", "customers"), _fk("product_id", "products")),
        ),
    ),
)


def _p(col, rows, distinct, *, nn=None, mn=None, mx=None, values=()):
    return ColumnProfile(
        column=col,
        row_count=rows,
        non_null_count=nn if nn is not None else rows,
        distinct_count=distinct,
        min_value=mn,
        max_value=mx,
        sample_values=tuple(values),
    )


PROFILES = {
    ("shop", "customers"): TableProfile(
        schema_name="shop",
        table="customers",
        row_count=80_000,
        columns=(
            _p("id", 80_000, 80_000),
            _p("customer_no", 80_000, 80_000),
            _p("segment", 80_000, 4, values=("VIP", "普通", "流失", "新客")),
            _p("vip_level", 80_000, 5, mn="0", mx="4"),
            _p("age", 80_000, 70, mn="18", mx="88"),
            _p("city", 80_000, 340),
            _p("registered_at", 80_000, 79_000, mn="2019-01-02", mx="2026-08-20"),
            _p("is_active", 80_000, 2),
        ),
    ),
    ("shop", "products"): TableProfile(
        schema_name="shop",
        table="products",
        row_count=12_000,
        columns=(
            _p("id", 12_000, 12_000),
            _p("sku", 12_000, 12_000),
            _p("category", 12_000, 18, values=("食品", "饮料", "日用品")),
            _p("list_price", 12_000, 9_800, mn="1.00", mx="12999.00"),
            _p("weight_kg", 12_000, 3_100, mn="0.050", mx="45.000"),
            _p("shelf_life_days", 12_000, 40, mn="7", mx="720"),
        ),
    ),
    ("shop", "orders"): TableProfile(
        schema_name="shop",
        table="orders",
        row_count=2_400_000,
        columns=(
            _p("id", 2_400_000, 2_400_000),
            _p("order_no", 2_400_000, 2_400_000),
            _p("customer_id", 2_400_000, 78_000),
            _p("product_id", 2_400_000, 11_900),
            _p("order_date", 2_400_000, 2_700, mn="2019-01-01", mx="2026-08-22"),
            _p("year", 2_400_000, 8, mn="2019", mx="2026"),
            _p("month", 2_400_000, 12, mn="1", mx="12"),
            _p("status_code", 2_400_000, 6, mn="0", mx="5"),
            _p("channel", 2_400_000, 5, values=("APP", "小程序", "H5", "门店")),
            _p("zip_code", 2_400_000, 2_900, mn="100000", mx="999999"),
            _p("qty", 2_400_000, 120, mn="1", mx="500"),
            _p("unit_price", 2_400_000, 9_700, mn="1.00", mx="12999.00"),
            _p("gross_amount", 2_400_000, 410_000, mn="1.00", mx="98210.50"),
            _p("discount_rate", 2_400_000, 400, mn="0.0000", mx="0.9000"),
            _p("net_amount", 2_400_000, 405_000, mn="0.00", mx="98210.50"),
            _p("refund_amount", 2_400_000, 31_000, nn=210_000, mn="0.00", mx="50000.00"),
            _p("is_gift", 2_400_000, 2),
            _p("created_at", 2_400_000, 2_350_000, mn="2019-01-01", mx="2026-08-22"),
        ),
    ),
}

# (table, column) → (kind, aggregation|None)。这是"正确答案"。
LABELS: dict[tuple[str, str], tuple[str, str | None]] = {
    ("customers", "id"): ("identifier", None),
    ("customers", "customer_no"): ("identifier", None),
    ("customers", "segment"): ("dimension", None),
    ("customers", "vip_level"): ("dimension", None),
    ("customers", "age"): ("dimension", None),
    ("customers", "city"): ("dimension", None),
    ("customers", "registered_at"): ("time", None),
    ("customers", "is_active"): ("dimension", None),
    ("products", "id"): ("identifier", None),
    ("products", "sku"): ("identifier", None),
    ("products", "category"): ("dimension", None),
    ("products", "list_price"): ("dimension", None),  # 标价是属性，相加无意义
    ("products", "weight_kg"): ("dimension", None),
    ("products", "shelf_life_days"): ("dimension", None),
    ("orders", "id"): ("identifier", None),
    ("orders", "order_no"): ("identifier", None),
    ("orders", "customer_id"): ("identifier", None),
    ("orders", "product_id"): ("identifier", None),
    ("orders", "order_date"): ("time", None),
    ("orders", "year"): ("dimension", None),  # 经典错：被 SUM
    ("orders", "month"): ("dimension", None),
    ("orders", "status_code"): ("dimension", None),  # 经典错：被 SUM
    ("orders", "channel"): ("dimension", None),
    ("orders", "zip_code"): ("dimension", None),  # 经典错：被 SUM
    ("orders", "qty"): ("measure", "sum"),
    ("orders", "unit_price"): ("measure", "avg"),  # 单价相加无意义
    ("orders", "gross_amount"): ("measure", "sum"),
    ("orders", "discount_rate"): ("measure", "avg"),  # 经典错：比率被 SUM
    ("orders", "net_amount"): ("measure", "sum"),
    ("orders", "refund_amount"): ("measure", "sum"),
    ("orders", "is_gift"): ("dimension", None),
    ("orders", "created_at"): ("time", None),
}
