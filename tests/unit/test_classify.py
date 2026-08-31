from __future__ import annotations

from knowflow_analytics.contracts import Aggregation, FieldKind
from knowflow_analytics.modeling.classify import (
    REVIEW_THRESHOLD,
    TableRole,
    classify_table,
    rule_based_role,
)
from knowflow_analytics.modeling.contracts import SchemaColumnSnapshot, TableSnapshot
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile


def _col(name: str, dtype: str, *, pk: bool = False) -> SchemaColumnSnapshot:
    return SchemaColumnSnapshot(
        name=name, data_type=dtype, nullable=not pk, comment="", ordinal_position=0, primary_key=pk
    )


def _table(*columns: SchemaColumnSnapshot) -> TableSnapshot:
    return TableSnapshot(schema_name="s", name="orders", columns=columns)


def _profile(rows: int, **per_column: dict) -> TableProfile:
    return TableProfile(
        schema_name="s",
        table="orders",
        row_count=rows,
        columns=tuple(
            ColumnProfile(
                column=name,
                row_count=rows,
                non_null_count=spec.get("non_null", rows),
                distinct_count=spec["distinct"],
                min_value=spec.get("min"),
                max_value=spec.get("max"),
                sample_values=tuple(spec.get("values", ())),
            )
            for name, spec in per_column.items()
        ),
    )


def _one(table, role, profile, fk=frozenset()):
    return classify_table(table, role=role, profile=profile, foreign_key_columns=fk)[0]


def test_a_year_column_is_never_a_summable_measure():
    """现在的规则是 numeric → measure SUM：「各年销售额」会把 2024+2025 相加。
    没有任何 prompt 能防 —— 模型不知道 distinct=8。只有画像能。"""

    result = _one(
        _table(_col("year", "INTEGER")),
        TableRole.FACT,
        _profile(50_000, year={"distinct": 8, "min": "2019", "max": "2026"}),
    )
    assert result.kind is FieldKind.DIMENSION
    assert result.aggregation is None
    assert result.confidence >= REVIEW_THRESHOLD


def test_a_status_code_integer_is_a_category_not_a_measure():
    result = _one(
        _table(_col("status_code", "SMALLINT")),
        TableRole.FACT,
        _profile(50_000, status_code={"distinct": 5}),
    )
    assert result.kind is FieldKind.DIMENSION
    assert "编码" in result.reason or "分类" in result.reason


def test_a_low_cardinality_integer_without_a_telltale_name_is_still_caught():
    """列名不像编码（比如 region_no 拼成 rg），但只有 12 个取值 —— 画像兜底。"""

    result = _one(
        _table(_col("rg", "INTEGER")),
        TableRole.FACT,
        _profile(50_000, rg={"distinct": 12}),
    )
    assert result.kind is FieldKind.DIMENSION


def test_a_real_amount_in_a_fact_table_is_sum():
    result = _one(
        _table(_col("net_amount", "NUMERIC(18,2)")),
        TableRole.FACT,
        _profile(50_000, net_amount={"distinct": 38_000, "min": "0.00", "max": "98210.50"}),
    )
    assert result.kind is FieldKind.MEASURE
    assert result.aggregation is Aggregation.SUM
    assert result.confidence >= REVIEW_THRESHOLD


def test_a_rate_column_defaults_to_avg_not_sum():
    result = _one(
        _table(_col("discount_rate", "NUMERIC(5,4)")),
        TableRole.FACT,
        _profile(50_000, discount_rate={"distinct": 900}),
    )
    assert result.kind is FieldKind.MEASURE
    assert result.aggregation is Aggregation.AVG


def test_a_numeric_in_a_dimension_table_is_sent_for_review():
    """维度表里的数值（年龄、等级）通常是属性不是度量；规则没把握，交模型确认。"""

    result = _one(
        _table(_col("age", "INTEGER")),
        TableRole.DIMENSION,
        _profile(20_000, age={"distinct": 71, "min": "18", "max": "88"}),
    )
    assert result.kind is FieldKind.DIMENSION
    assert result.needs_review


def test_database_constraints_beat_every_profile_signal():
    table = _table(_col("id", "BIGINT", pk=True), _col("customer_id", "BIGINT"))
    results = classify_table(
        table,
        role=TableRole.FACT,
        profile=_profile(50_000, id={"distinct": 50_000}, customer_id={"distinct": 3}),
        foreign_key_columns=frozenset({"customer_id"}),
    )
    assert results[0].identifier_type == "primary" and results[0].confidence == 1.0
    # 只有 3 个取值的外键仍然是外键，不是分类维度
    assert results[1].identifier_type == "foreign" and results[1].confidence == 1.0


def test_a_near_unique_column_without_a_constraint_is_an_identifier_candidate():
    result = _one(
        _table(_col("order_no", "TEXT")),
        TableRole.FACT,
        _profile(50_000, order_no={"distinct": 49_990}),
    )
    assert result.kind is FieldKind.IDENTIFIER
    assert result.identifier_type == "primary"


def test_low_cardinality_text_is_a_category_with_its_values_known():
    result = _one(
        _table(_col("status", "TEXT")),
        TableRole.FACT,
        _profile(50_000, status={"distinct": 4, "values": ["待付款", "已发货"]}),
    )
    assert result.kind is FieldKind.DIMENSION
    assert result.dimension_type == "categorical"


def test_without_a_profile_the_rules_still_classify_by_type_and_name():
    """画像查询失败时不能让整张表变成"未分类"。"""

    results = classify_table(
        _table(
            _col("year", "INTEGER"),
            _col("net_amount", "NUMERIC"),
            _col("created_at", "TIMESTAMP"),
        ),
        role=TableRole.FACT,
        profile=None,
        foreign_key_columns=frozenset(),
    )
    assert results[0].kind is FieldKind.DIMENSION  # 靠列名
    assert results[1].kind is FieldKind.MEASURE  # 靠角色
    assert results[2].kind is FieldKind.TIME


def test_rule_based_role_fallback():
    customers = TableSnapshot(
        schema_name="s",
        name="customers",
        columns=(_col("id", "BIGINT", pk=True), _col("name", "TEXT"), _col("segment", "TEXT")),
    )
    assert (
        rule_based_role(customers, in_degree=2, out_degree=0, prefills_numeric_non_key=0)
        is TableRole.DIMENSION
    )
    orders = _table(
        _col("id", "BIGINT", pk=True),
        _col("customer_id", "BIGINT"),
        _col("amount", "NUMERIC"),
    )
    assert (
        rule_based_role(orders, in_degree=0, out_degree=1, prefills_numeric_non_key=1)
        is TableRole.FACT
    )


def test_a_near_unique_timestamp_is_time_not_an_identifier():
    """评测基线抓到的：时间戳天然几乎每行不同，唯一率规则若排在时间类型之前，
    created_at 就成了"主键候选"。"""

    result = _one(
        _table(_col("created_at", "TIMESTAMP")),
        TableRole.FACT,
        _profile(50_000, created_at={"distinct": 49_800, "min": "2019-01-01", "max": "2026-08-22"}),
    )
    assert result.kind is FieldKind.TIME


def test_a_unit_price_in_a_fact_table_averages_and_a_list_price_in_a_dimension_does_not_sum():
    unit = _one(
        _table(_col("unit_price", "NUMERIC(12,2)")),
        TableRole.FACT,
        _profile(50_000, unit_price={"distinct": 9_700}),
    )
    assert unit.kind is FieldKind.MEASURE and unit.aggregation is Aggregation.AVG
    listed = _one(
        _table(_col("list_price", "NUMERIC(12,2)")),
        TableRole.DIMENSION,
        _profile(12_000, list_price={"distinct": 9_800}),
    )
    assert listed.kind is not FieldKind.MEASURE


# ---- 中文列名 ------------------------------------------------------------------


def test_chinese_rate_like_names_in_a_fact_table_default_to_avg_not_sum():
    """「首套房贷款利率」在事实表里曾走 numeric→SUM(0.8) 免审——利率求和无意义。"""

    prefills = classify_table(
        _table(
            _col("银行id", "TEXT"),
            _col("首套房贷款利率", "NUMERIC"),
            _col("平均折扣", "NUMERIC"),
        ),
        role=TableRole.FACT,
        profile=None,
        foreign_key_columns=frozenset({"银行id"}),
    )
    by = {item.column: item for item in prefills}
    assert by["首套房贷款利率"].kind is FieldKind.MEASURE
    assert by["首套房贷款利率"].aggregation is Aggregation.AVG
    assert by["平均折扣"].aggregation is Aggregation.AVG


def test_chinese_code_like_names_are_not_measures():
    """「500强排名」「公司类型」相加无业务意义,不得进 SUM 度量。"""

    prefills = classify_table(
        _table(_col("500强排名", "NUMERIC"), _col("状态编码", "INTEGER")),
        role=TableRole.FACT,
        profile=None,
        foreign_key_columns=frozenset(),
    )
    by = {item.column: item for item in prefills}
    assert by["500强排名"].kind is FieldKind.DIMENSION
    assert by["状态编码"].kind is FieldKind.DIMENSION


def test_chinese_time_like_names_ask_for_confirmation():
    prefills = classify_table(
        _table(_col("下单日期", "TEXT")),
        role=TableRole.FACT,
        profile=None,
        foreign_key_columns=frozenset(),
    )
    by = {item.column: item for item in prefills}
    assert by["下单日期"].kind is FieldKind.TIME
