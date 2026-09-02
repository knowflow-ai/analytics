from __future__ import annotations

from types import SimpleNamespace

from knowflow_analytics.execution.executor import SqlExecutor


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Result:
    @staticmethod
    def scalar_one():
        return [{"Plan": {"Node Type": "Result"}}]


class _Connection:
    def __init__(self) -> None:
        self.driver_sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @staticmethod
    def begin():
        return _Transaction()

    def exec_driver_sql(self, statement: str) -> None:
        self.driver_sql.append(statement)

    @staticmethod
    def execute(*_args, **_kwargs):
        return _Result()


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self):
        return self.connection

    @staticmethod
    def dispose() -> None:
        return None


class _Guard:
    @staticmethod
    def validate(**_kwargs) -> None:
        return None


def test_explain_uses_the_same_read_only_timeouts_as_execution() -> None:
    connection = _Connection()
    executor = SqlExecutor(
        "postgresql://unused",
        engine=_Engine(connection),
        guard=_Guard(),
        statement_timeout_ms=1_234,
        lock_timeout_ms=567,
    )

    plan = executor.explain(
        query=SimpleNamespace(sql="SELECT 1 LIMIT 1", parameters={}),
        release=object(),
    )

    assert plan == [{"Plan": {"Node Type": "Result"}}]
    assert connection.driver_sql == [
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 1234",
        "SET LOCAL lock_timeout = 567",
    ]
