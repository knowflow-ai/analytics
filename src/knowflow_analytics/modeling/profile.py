"""S1 画像：建模前读每列的统计量，给分类规则和命名 prompt 当证据。

dbt Wizard 在建模前 profile（行数 / 分布 / NULL 率）；WrenAI 不给画像，它的分类
只能靠类型和列名。``year``、``status_code`` 被标成可加度量这类静默错数，没有任何
prompt 能防 —— 模型不知道 ``distinct_count=8``。只有画像知道。

敏感度分层：统计量不含任何原始值，永远给；``sample_values`` 只对低基数的非数值列
采样，受开关控制，且列名像个人信息的列永不采样。
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.execution.dialect import SqlDialect, to_dialect_sql
from knowflow_analytics.modeling.contracts import SchemaColumnSnapshot, TableSnapshot
from knowflow_analytics.modeling.type_system import is_numeric_type, is_temporal_type

# 低于这个基数的非数值列才取实际值给模型看；再多就是自由文本，没有命名价值还占上下文。
SAMPLE_VALUES_MAX_DISTINCT = 50
SAMPLE_VALUES_LIMIT = 20

# 列名像个人信息的永不采样 —— 采样值会进 prompt、离开内网。
_SENSITIVE_COLUMN = re.compile(
    r"(name|phone|mobile|email|mail|id_?card|passport|address|addr|身份证|手机|邮箱|姓名|地址)",
    re.IGNORECASE,
)


class ColumnProfile(FrozenModel):
    column: str
    row_count: int = Field(ge=0)
    non_null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    min_value: str | None = Field(default=None, max_length=256)
    max_value: str | None = Field(default=None, max_length=256)
    sample_values: tuple[str, ...] = Field(default=(), max_length=SAMPLE_VALUES_LIMIT)

    @property
    def null_rate(self) -> float:
        return 0.0 if self.row_count == 0 else 1 - self.non_null_count / self.row_count

    @property
    def distinct_ratio(self) -> float:
        return 0.0 if self.non_null_count == 0 else self.distinct_count / self.non_null_count


class TableProfile(FrozenModel):
    schema_name: str
    table: str
    row_count: int = Field(ge=0)
    columns: tuple[ColumnProfile, ...] = ()
    truncated: bool = False  # 行数超过采样上限，统计量来自 TABLESAMPLE
    error: str | None = Field(default=None, max_length=1_000)

    def column(self, name: str) -> ColumnProfile | None:
        return next((item for item in self.columns if item.column == name), None)


class ColumnProfileError(AnalyticsError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="COLUMN_PROFILE_FAILED", stage="MODELING")


class ColumnProfiler(Protocol):
    def profile_table(self, table: TableSnapshot) -> TableProfile: ...


def column_is_sensitive(column_name: str) -> bool:
    return _SENSITIVE_COLUMN.search(column_name) is not None


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class ColumnStatisticsProfiler:
    """一张表一条 SQL：COUNT(*) + 每列 COUNT(col) / COUNT(DISTINCT col) / MIN / MAX。

    大表用 TABLESAMPLE SYSTEM 把扫描量压到上限附近 —— 分类只需要量级
    （"distinct 是 8 还是 80 万"），不需要精确值。采样值单独再查一次，只对低基数列。
    """

    def __init__(
        self,
        engine: Engine,
        *,
        sample_rows: int = 200_000,
        statement_timeout_ms: int = 10_000,
        sample_values: bool = True,
        dialect: SqlDialect = SqlDialect.POSTGRES,
    ) -> None:
        self._engine = engine
        self._sample_rows = sample_rows
        self._statement_timeout_ms = statement_timeout_ms
        self._sample_values_enabled = sample_values
        self._dialect = dialect

    def profile_table(self, table: TableSnapshot) -> TableProfile:
        source = f"{_quote(table.schema_name)}.{_quote(table.name)}"
        try:
            row_count = self._row_count(source)
            truncated = row_count > self._sample_rows
            sample_source = source
            if truncated:
                # SYSTEM 采样按页，很快；百分比留 20% 余量保证至少采到目标行数。
                # MySQL 没有 TABLESAMPLE，方言层会退化成有界 LIMIT（见其注释）。
                percent = min(100.0, 100.0 * self._sample_rows / row_count * 1.2)
                sample_source = self._dialect.sampled_source_sql(
                    source, percent=percent, rows=self._sample_rows
                )
            stats = self._column_stats(sample_source, table.columns)
            samples = (
                self._sample_values(sample_source, table.columns, stats)
                if self._sample_values_enabled
                else {}
            )
        except SQLAlchemyError as exc:
            return TableProfile(
                schema_name=table.schema_name,
                table=table.name,
                row_count=0,
                error=f"{exc.__class__.__name__}: {exc}"[:1_000],
            )
        columns = tuple(
            ColumnProfile(
                column=column.name,
                row_count=stats["__rows__"],
                non_null_count=stats[f"nn_{index}"],
                distinct_count=stats[f"nd_{index}"],
                min_value=_clip(stats.get(f"mn_{index}")),
                max_value=_clip(stats.get(f"mx_{index}")),
                sample_values=tuple(samples.get(column.name, ())),
            )
            for index, column in enumerate(table.columns)
        )
        return TableProfile(
            schema_name=table.schema_name,
            table=table.name,
            row_count=row_count,
            columns=columns,
            truncated=truncated,
        )

    def _execute(self, query: str, params: dict | None = None):
        """画像语句按内部记法（PostgreSQL 写法）拼，在这里统一落到数据源的方言。

        底下那些 SQL 是手写字符串，绕过了翻译器；不在这一处收口的话，MySQL 数据源
        连建模都进不去——``::bigint`` 是语法错、``FILTER`` 是语法错。
        """

        with self._engine.connect() as connection, connection.begin():
            for statement in self._dialect.read_only_session_sql(
                statement_timeout_ms=self._statement_timeout_ms, lock_timeout_ms=1_000
            ):
                connection.exec_driver_sql(statement)
            return connection.execute(
                text(to_dialect_sql(query, self._dialect)), params or {}
            ).all()

    def _row_count(self, source: str) -> int:
        rows = self._execute(f"SELECT COUNT(*)::bigint AS n FROM {source}")
        return int(rows[0][0]) if rows else 0

    def _column_stats(
        self, source: str, columns: tuple[SchemaColumnSnapshot, ...]
    ) -> dict[str, object]:
        selects = ["COUNT(*)::bigint AS __rows__"]
        for index, column in enumerate(columns):
            quoted = _quote(column.name)
            selects.append(f"COUNT({quoted})::bigint AS nn_{index}")
            selects.append(f"COUNT(DISTINCT {quoted})::bigint AS nd_{index}")
            if is_numeric_type(column.data_type) or is_temporal_type(column.data_type):
                selects.append(f"MIN({quoted})::text AS mn_{index}")
                selects.append(f"MAX({quoted})::text AS mx_{index}")
        rows = self._execute(f"SELECT {', '.join(selects)} FROM {source}")
        if not rows:
            return {
                "__rows__": 0,
                **{f"nn_{i}": 0 for i in range(len(columns))},
                **{f"nd_{i}": 0 for i in range(len(columns))},
            }
        return dict(rows[0]._mapping)

    def _sample_values(
        self,
        source: str,
        columns: tuple[SchemaColumnSnapshot, ...],
        stats: dict[str, object],
    ) -> dict[str, tuple[str, ...]]:
        out: dict[str, tuple[str, ...]] = {}
        for index, column in enumerate(columns):
            if is_numeric_type(column.data_type) or is_temporal_type(column.data_type):
                continue
            if column_is_sensitive(column.name):
                continue
            if int(stats.get(f"nd_{index}", 0) or 0) > SAMPLE_VALUES_MAX_DISTINCT:
                continue
            quoted = _quote(column.name)
            rows = self._execute(
                f"SELECT {quoted}::text AS v, COUNT(*) AS c FROM {source} "
                f"WHERE {quoted} IS NOT NULL GROUP BY {quoted} "
                f"ORDER BY c DESC, v LIMIT :limit",
                {"limit": SAMPLE_VALUES_LIMIT},
            )
            out[column.name] = tuple(str(row[0])[:64] for row in rows)
        return out


def _clip(value: object) -> str | None:
    if value is None:
        return None
    return str(value)[:256]
