from datetime import UTC, datetime

from knowflow_analytics.modeling.contracts import (
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.drift import SchemaDriftAnalyzer, SchemaDriftSeverity


def test_schema_drift_reports_semantic_impact_and_is_order_invariant(sales_release):
    baseline = SchemaSnapshot.create(
        database_name="analytics",
        tables=(
            TableSnapshot(
                schema_name="analytics_v0",
                name="orders",
                columns=(
                    _column("id", "INTEGER", primary=True, nullable=False),
                    _column("net_amount", "NUMERIC", nullable=False),
                ),
            ),
        ),
        captured_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    current = (
        TableSnapshot(
            schema_name="analytics_v0",
            name="orders",
            columns=(
                _column("note", "TEXT"),
                _column("net_amount", "TEXT", nullable=False),
            ),
        ),
    )

    report = SchemaDriftAnalyzer().analyze(
        project_id=sales_release.project_id,
        revision_id="revision-1",
        revision_etag=4,
        baseline=baseline,
        current_tables=current,
        available_table_keys=(("analytics_v0", "orders"),),
        semantic_spec=sales_release,
    )
    reordered = SchemaDriftAnalyzer().analyze(
        project_id=sales_release.project_id,
        revision_id="revision-1",
        revision_etag=4,
        baseline=baseline,
        current_tables=tuple(reversed(current)),
        available_table_keys=(("analytics_v0", "orders"),),
        semantic_spec=sales_release,
    )

    changes = {(item.change_type, item.column_name): item for item in report.changes}
    assert changes[("column_removed", "id")].severity is SchemaDriftSeverity.BLOCKING
    assert changes[("column_type_changed", "net_amount")].severity is SchemaDriftSeverity.BLOCKING
    assert changes[("column_added", "note")].severity is SchemaDriftSeverity.WARNING
    impacted = {
        (item.resource_kind, item.resource_id)
        for item in changes[("column_type_changed", "net_amount")].impacts
    }
    assert ("field", "orders.net_amount") in impacted
    assert ("metric", "net_revenue") in impacted
    assert ("dataset", "sales_dataset") in impacted
    assert report.blocking_count == 2
    assert report.ready is False
    assert reordered.content_hash == report.content_hash


def _column(
    name: str,
    data_type: str,
    *,
    nullable: bool = True,
    primary: bool = False,
) -> SchemaColumnSnapshot:
    return SchemaColumnSnapshot(
        name=name,
        data_type=data_type,
        nullable=nullable,
        ordinal_position=0,
        primary_key=primary,
    )
