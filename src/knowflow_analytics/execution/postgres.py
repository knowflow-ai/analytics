from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.contracts import PhysicalQuery, QueryResult, SemanticRelease
from knowflow_analytics.errors import QueryExecutionError
from knowflow_analytics.execution.guard import PhysicalSqlGuard


class PostgresExecutor:
    """Execute validated queries in an explicitly read-only PostgreSQL transaction."""

    def __init__(
        self,
        database_url: str,
        *,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 2_000,
        guard: PhysicalSqlGuard | None = None,
        engine: Engine | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("database_url is required")
        if statement_timeout_ms < 1 or lock_timeout_ms < 1:
            raise ValueError("query timeouts must be positive")
        self._engine = engine or create_engine(database_url, pool_pre_ping=True)
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        self._guard = guard or PhysicalSqlGuard()

    def execute(self, *, query: PhysicalQuery, release: SemanticRelease) -> QueryResult:
        self._guard.validate(query=query, release=release)
        try:
            with self._engine.connect() as connection, connection.begin():
                self._configure_transaction(connection)
                result = connection.execute(text(query.sql), query.parameters)
                fetched = tuple(
                    tuple(_normalize_cell(cell) for cell in row) for row in result.fetchall()
                )
                truncated = len(fetched) > query.result_limit
                rows = fetched[: query.result_limit]
                physical_columns = tuple(map(str, result.keys()))
                if len(physical_columns) != len(query.columns):
                    raise QueryExecutionError(
                        "PostgreSQL result does not match the physical query column contract"
                    )
                columns = tuple(item.element_id for item in query.columns)
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                "PostgreSQL query failed",
                details=_safe_database_error_details(exc),
            ) from exc
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )

    def explain(
        self, *, query: PhysicalQuery, release: SemanticRelease
    ) -> Mapping[str, Any] | list[Any]:
        self._guard.validate(query=query, release=release)
        try:
            with self._engine.connect() as connection, connection.begin():
                self._configure_transaction(connection)
                result = connection.execute(
                    text(f"EXPLAIN (FORMAT JSON) {query.sql}"), query.parameters
                )
                row = result.scalar_one()
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                "PostgreSQL query planning failed",
                details=_safe_database_error_details(exc),
            ) from exc
        return row

    def _configure_transaction(self, connection: Any) -> None:
        """Apply the same read-only and timeout contract to execute and EXPLAIN."""

        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}")

    def close(self) -> None:
        self._engine.dispose()


def _normalize_cell(value: Any) -> Any:
    """AVG(NUMERIC) 返回满刻度 Decimal（0.30000000000000000000），原样透传会把
    尾零一路带到前端。只删无意义的尾零，不动数值；整数值取整数刻度，避免
    ``normalize()`` 在 100.00 上给出 1E+2 的科学计数法。非有限值（NaN）原样保留。
    """

    if not isinstance(value, Decimal):
        return value
    if not value.is_finite():
        return value
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _safe_database_error_details(exc: SQLAlchemyError) -> dict[str, object]:
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None)
    diagnostic = getattr(original, "diag", None)
    primary = getattr(diagnostic, "message_primary", None)
    details: dict[str, object] = {}
    if isinstance(sqlstate, str) and sqlstate:
        details["sqlstate"] = sqlstate[:5]
    if isinstance(primary, str) and primary:
        details["database_message"] = primary[:1_000]
    return details
