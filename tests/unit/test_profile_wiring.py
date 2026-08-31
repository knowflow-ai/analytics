from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.contracts import (
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableCatalogEntry,
    TableSnapshot,
)
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.semantic.index import EmbeddingBatch


def _col(name, dtype, *, pk=False):
    return SchemaColumnSnapshot(
        name=name, data_type=dtype, nullable=not pk, comment="", ordinal_position=0, primary_key=pk
    )


_SNAPSHOT = SchemaSnapshot.create(
    database_name="analytics",
    captured_at=datetime(2026, 8, 23, tzinfo=UTC),
    tables=(
        TableSnapshot(
            schema_name="sales",
            name="sales_by_year",
            columns=(
                _col("id", "BIGINT", pk=True),
                _col("year", "INTEGER"),
                _col("net_amount", "NUMERIC(18,2)"),
            ),
        ),
    ),
)


class _Introspector:
    def list_schemas(self):
        return ("sales",)

    def list_tables(self, *, schema_name, include_views=False):
        return tuple(
            TableCatalogEntry(
                schema_name=t.schema_name, name=t.name, source_type=t.source_type, comment=""
            )
            for t in _SNAPSHOT.tables
        )

    def describe_table(self, *, schema_name, table_name, include_views=False):
        return next(t for t in _SNAPSHOT.tables if t.name == table_name)

    def scan(self, **_kwargs):
        return _SNAPSHOT


class _Embedding:
    def encode(self, texts):
        return EmbeddingBatch(model_id="t", dimension=1, vectors=tuple((1.0,) for _ in texts))


class _Profiler:
    """year 只有 8 个取值；net_amount 几乎每行不同。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def profile_table(self, table):
        self.calls.append(table.name)
        return TableProfile(
            schema_name=table.schema_name,
            table=table.name,
            row_count=50_000,
            columns=(
                ColumnProfile(
                    column="id", row_count=50_000, non_null_count=50_000, distinct_count=50_000
                ),
                ColumnProfile(
                    column="year",
                    row_count=50_000,
                    non_null_count=50_000,
                    distinct_count=8,
                    min_value="2019",
                    max_value="2026",
                ),
                ColumnProfile(
                    column="net_amount",
                    row_count=50_000,
                    non_null_count=50_000,
                    distinct_count=41_200,
                ),
            ),
        )


def _app(profiler):
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
        introspector=_Introspector(),
        executor=object(),
        embedding_gateway=_Embedding(),
        column_profiler=profiler,
        ai_modeller=AiSemanticModeller(model_gateway=_SumEverythingGateway()),
        require_evaluation_for_publish=False,
        require_quality_report_for_publish=False,
    )


class _SumEverythingGateway:
    """模拟 32B 的典型错误：看到数值就 SUM。year 只有 8 个取值它不知道。"""

    def generate_json(self, **_kwargs):
        def col(name, dtype, filed_type, agg, cn):
            return {
                "columnName": name,
                "dataType": dtype,
                "comment": "",
                "filedType": filed_type,
                "agg": agg,
                "name": cn,
                "expr": name,
            }

        return {
            "name": "年度销售",
            "bizName": "sales_by_year",
            "description": "按年汇总的销售",
            "semanticColumns": [
                col("id", "BIGINT", "primary_key", "NONE", "ID"),
                col("year", "INTEGER", "measure", "SUM", "年份"),
                col("net_amount", "NUMERIC(18,2)", "measure", "SUM", "净金额"),
            ],
        }


def _run_ai(application):
    application.create_project(project_id="sales", name="销售")
    snapshot = application.create_schema_snapshot(
        project_id="sales", schemas=("sales",), selected_tables={"sales": ("sales_by_year",)}
    )
    revision = application.create_empty_revision(project_id="sales", schema_snapshot_id=snapshot.id)
    revision = application.add_table_model(
        revision_id=revision.id,
        expected_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        schema_name="sales",
        table_name="sales_by_year",
    )
    run = application.create_ai_suggestion_run(
        revision_id=revision.id, expected_etag=revision.etag, persist=False
    )
    fields = {f.id: f.column for f in revision.semantic_spec.fields}
    return {fields[s.target_id]: s for s in run.suggestions if s.target_kind == "field"}


def test_profile_guardrail_stops_the_model_from_summing_a_year_column():
    """32B 看到数值就 SUM：「各年销售额」会把 2024+2025 相加。模型不知道
    distinct=8，画像知道 —— S3 护栏把它改回分类维度。金额不受影响。"""

    profiler = _Profiler()
    by_column = _run_ai(_app(profiler))

    assert profiler.calls == ["sales_by_year"]
    year = by_column["year"]
    assert year.changes["kind"] == "dimension"
    assert "aggregation" not in year.changes
    # 分阶段工作流下模型根本没机会对 year 说 SUM：分类由画像规则定，理由写明取值数。
    assert "年份" in year.reason or "8 个取值" in year.reason
    amount = by_column["net_amount"]
    assert amount.changes["kind"] == "measure"
    assert amount.changes["aggregation"] == "sum"


def test_without_a_profiler_the_column_name_rule_still_catches_year():
    """画像是证据不是门禁。没有 profiler 时，列名 year 本身就够规则拦下。"""

    by_column = _run_ai(_app(None))
    assert by_column["year"].changes["kind"] == "dimension"
    assert "aggregation" not in by_column["year"].changes


def test_a_failing_profiler_does_not_block_modeling():
    class _Exploding:
        def profile_table(self, table):
            raise RuntimeError("connection refused")

    by_column = _run_ai(_app(_Exploding()))
    assert by_column["net_amount"].changes["kind"] == "measure"
