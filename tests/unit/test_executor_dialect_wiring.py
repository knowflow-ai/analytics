"""执行器与守卫的方言接线。

一处没接上就是「PostgreSQL 上好好的、换到 MySQL 静默出错或直接打不开」，
所以这里逐条钉住每个接线点。
"""

from __future__ import annotations

from typing import Any

import pytest

from knowflow_analytics.contracts import OutputColumn, PhysicalQuery
from knowflow_analytics.errors import QueryGuardError
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.execution.executor import (
    SqlExecutor,
    _safe_database_error_details,
)
from knowflow_analytics.execution.guard import PhysicalSqlGuard


class _RecordingConnection:
    """只记录发出去的语句，不连数据库。"""

    def __init__(self) -> None:
        self.driver_statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> None:
        self.driver_statements.append(statement)


class TestSessionConfiguration:
    @staticmethod
    def _statements(dialect: SqlDialect) -> list[str]:
        executor = SqlExecutor(
            "postgresql+psycopg://x/y",
            dialect=dialect,
            statement_timeout_ms=30_000,
            lock_timeout_ms=2_000,
            engine=object(),
        )
        connection = _RecordingConnection()
        executor._configure_transaction(connection)
        return connection.driver_statements

    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_every_dialect_opens_a_read_only_transaction(self, dialect: SqlDialect):
        # 只读是安全边界：漏掉某个方言 = 那个方言上问数可以写库。
        assert any("READ ONLY" in item for item in self._statements(dialect))

    def test_postgres_keeps_its_original_statements(self):
        """PostgreSQL 侧必须与改造前逐字相同。"""

        assert self._statements(SqlDialect.POSTGRES) == [
            "SET TRANSACTION READ ONLY",
            "SET LOCAL statement_timeout = 30000",
            "SET LOCAL lock_timeout = 2000",
        ]

    def test_mysql_uses_its_own_syntax(self):
        statements = self._statements(SqlDialect.MYSQL)

        assert "SET LOCAL statement_timeout = 30000" not in statements
        assert "SET SESSION max_execution_time = 30000" in statements

    def test_mysql_read_only_is_not_session_scoped(self):
        # 加 SESSION 会把只读粘在池化连接上，污染后续拿到它的写入方。
        assert "SET SESSION TRANSACTION READ ONLY" not in self._statements(SqlDialect.MYSQL)


class TestGuardDialect:
    @staticmethod
    def _query(sql: str) -> PhysicalQuery:
        return PhysicalQuery(
            release_id="r",
            dataset_id="d",
            sql=sql,
            parameters={},
            columns=(OutputColumn(element_id="a", name="a", kind="dimension"),),
            relation_ids=(),
            result_limit=10,
        )

    def test_mysql_guard_accepts_backtick_quoting(self):
        """反引号在 PostgreSQL 方言下根本解析不了。

        守卫用错方言不会放过危险语句（禁止节点表是方言无关的），但会把合法的
        MySQL SQL 判成语法错——整条链路停在这里，表现为"MySQL 数据源什么都问不了"。
        """

        guard = PhysicalSqlGuard(dialect=SqlDialect.MYSQL)

        # 解析得过；后面因为 release 为空而以别的理由拒绝，这里只验证解析这一步。
        with pytest.raises(QueryGuardError) as excinfo:
            guard.validate(query=self._query("SELECT `a` FROM `t` LIMIT 10"), release=_release())

        assert "not valid" not in str(excinfo.value)

    def test_postgres_guard_rejects_backticks(self):
        guard = PhysicalSqlGuard(dialect=SqlDialect.POSTGRES)

        with pytest.raises(QueryGuardError) as excinfo:
            guard.validate(query=self._query("SELECT `a` FROM `t` LIMIT 10"), release=_release())

        assert "not valid" in str(excinfo.value)

    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_write_statements_are_refused_in_every_dialect(self, dialect: SqlDialect):
        guard = PhysicalSqlGuard(dialect=dialect)
        quoted = "`t`" if dialect is SqlDialect.MYSQL else '"t"'

        with pytest.raises(QueryGuardError):
            guard.validate(query=self._query(f"DELETE FROM {quoted}"), release=_release())

    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_missing_limit_is_refused_in_every_dialect(self, dialect: SqlDialect):
        guard = PhysicalSqlGuard(dialect=dialect)
        quoted = "`t`" if dialect is SqlDialect.MYSQL else '"t"'

        with pytest.raises(QueryGuardError):
            guard.validate(query=self._query(f"SELECT * FROM {quoted}"), release=_release())

    def test_executor_hands_its_dialect_to_the_default_guard(self):
        """执行器和守卫必须用同一个方言。

        分别配置就会出现"执行器按 MySQL 发、守卫按 PostgreSQL 校"这种谁也发现不了
        的错配。
        """

        executor = SqlExecutor("mysql+pymysql://x/y", dialect=SqlDialect.MYSQL, engine=object())

        assert executor._guard._dialect is SqlDialect.MYSQL


class TestDatabaseErrorDetails:
    def test_reads_psycopg_diagnostics(self):
        exc = _fake_error(
            sqlstate="42P01",
            diag=type("D", (), {"message_primary": 'relation "x" does not exist'})(),
        )

        details = _safe_database_error_details(exc)

        assert details == {
            "sqlstate": "42P01",
            "database_message": 'relation "x" does not exist',
        }

    def test_reads_pymysql_errno_message_pairs(self):
        """pymysql 没有 ``diag``，消息在 ``args[1]``。

        不处理的话 MySQL 上的每个数据库错误都只剩一个 sqlstate，用户看不到原因。
        """

        exc = _fake_error(sqlstate="42S02", args=(1146, "Table 'db.t' doesn't exist"))

        details = _safe_database_error_details(exc)

        assert details["database_message"] == "Table 'db.t' doesn't exist"

    def test_never_falls_back_to_the_raw_first_argument(self):
        """psycopg 的 ``args[0]`` 里带着出错的 SQL 原文和 LINE 定位（实测）。

        笼统兜底会把物理 SQL 漏进面向用户的错误信息——普通 wire 上不能出现它。
        """

        exc = _fake_error(
            sqlstate="42P01",
            args=('relation "x" does not exist\nLINE 1: SELECT secret_column FROM x\n  ^',),
        )

        details = _safe_database_error_details(exc)

        assert "database_message" not in details
        assert "secret_column" not in str(details)

    def test_unknown_driver_shapes_yield_nothing(self):
        assert _safe_database_error_details(_fake_error()) == {}

    def test_message_is_length_capped(self):
        exc = _fake_error(args=(1146, "x" * 5_000))

        assert len(_safe_database_error_details(exc)["database_message"]) == 1_000


def _fake_error(**attributes: Any):
    from sqlalchemy.exc import SQLAlchemyError

    original = type("Orig", (), attributes)()
    error = SQLAlchemyError("boom")
    error.orig = original  # type: ignore[attr-defined]
    return error


def _release():
    from knowflow_analytics.contracts import SemanticRelease

    return SemanticRelease.model_construct(datasets=(), models=())
