from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.contracts import PhysicalQuery, QueryResult, SemanticRelease
from knowflow_analytics.errors import QueryExecutionError
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.execution.guard import PhysicalSqlGuard


class SqlExecutor:
    """在显式只读事务里执行已校验的查询。

    连接与类型编解码交给 SQLAlchemy，方言差异集中在 ``SqlDialect``——所以这里
    一个引擎一个类是不必要的，只有一个执行器，带上它服务的方言。
    """

    def __init__(
        self,
        database_url: str,
        *,
        dialect: SqlDialect = SqlDialect.POSTGRES,
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
        self._dialect = dialect
        self._statement_timeout_ms = statement_timeout_ms
        self._lock_timeout_ms = lock_timeout_ms
        # 守卫必须和执行器用同一个方言：用错方言会把合法 SQL 判成语法错，
        # 整条链路停在守卫这一步。
        self._guard = guard or PhysicalSqlGuard(dialect=dialect)

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
                        "database result does not match the physical query column contract"
                    )
                columns = tuple(item.element_id for item in query.columns)
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                f"{self._dialect.value} query failed",
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
                    text(self._dialect.explain_sql(query.sql)), query.parameters
                )
                row = result.scalar_one()
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                f"{self._dialect.value} query planning failed",
                details=_safe_database_error_details(exc),
            ) from exc
        return row

    def _configure_transaction(self, connection: Any) -> None:
        """execute 与 EXPLAIN 共用同一套只读与超时约束。

        必须在事务真正落地**之前**发出：SQLAlchemy 的 ``begin()`` 是惰性的，第一条
        语句才触发 BEGIN，所以这几条 SET 正好赶在前面。MySQL 的
        ``SET TRANSACTION READ ONLY`` 只对下一个事务生效，靠的就是这个顺序。
        """

        for statement in self._dialect.read_only_session_sql(
            statement_timeout_ms=self._statement_timeout_ms,
            lock_timeout_ms=self._lock_timeout_ms,
        ):
            connection.exec_driver_sql(statement)

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
    """从驱动异常里取出可以外传的部分。

    两个驱动的形状不同：psycopg 把干净的消息放在 ``diag.message_primary``，pymysql
    没有 ``diag``，消息在 ``args[1]``（``args[0]`` 是 errno）。两边都有 ``sqlstate``。

    **不能笼统地拿 ``args[0]`` 兜底**：psycopg 的 ``args[0]`` 里带着出错的 SQL 原文
    和 ``LINE 1: ...`` 定位（实测），那会把物理 SQL 漏进面向用户的错误信息里。所以
    只认这两种已知形状，认不出就什么都不给。
    """

    original = getattr(exc, "orig", None)
    details: dict[str, object] = {}
    sqlstate = getattr(original, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        details["sqlstate"] = sqlstate[:5]
    primary = getattr(getattr(original, "diag", None), "message_primary", None)
    if not isinstance(primary, str) or not primary:
        # pymysql 的 (errno, message)。只在恰好是这个形状时才取，别的一律不碰。
        args = getattr(original, "args", None)
        if (
            isinstance(args, tuple)
            and len(args) == 2
            and isinstance(args[0], int)
            and isinstance(args[1], str)
        ):
            primary = args[1]
    if isinstance(primary, str) and primary:
        details["database_message"] = primary[:1_000]
    return details
