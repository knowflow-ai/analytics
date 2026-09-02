from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import PhysicalQuery, SemanticRelease
from knowflow_analytics.errors import QueryGuardError, SemanticValidationError
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.modeling.sql_model import validate_sql_model

_COMMENT_RE = re.compile(r"(--|/\*)")
_FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Merge,
    exp.Grant,
    exp.Revoke,
)


class PhysicalSqlGuard:
    """Fail-closed physical SQL validation performed immediately before execution.

    守卫要按**数据源自己的方言**解析待执行的 SQL。用错方言不会漏放危险语句（禁止
    节点那张表是方言无关的），但会把合法 SQL 判成语法错——MySQL 的反引号在
    PostgreSQL 方言下根本解析不了，整条链路会停在这里。
    """

    def __init__(self, *, dialect: SqlDialect = SqlDialect.POSTGRES) -> None:
        self._dialect = dialect

    def validate(self, *, query: PhysicalQuery, release: SemanticRelease) -> None:
        if _COMMENT_RE.search(query.sql):
            raise QueryGuardError("SQL comments are not allowed")
        try:
            statements = sqlglot.parse(query.sql, read=self._dialect.value)
        except sqlglot.errors.ParseError as exc:
            raise QueryGuardError(f"physical SQL is not valid {self._dialect.value} SQL") from exc
        if len(statements) != 1:
            raise QueryGuardError("exactly one SQL statement is required")
        statement = statements[0]
        if not isinstance(statement, (exp.Select, exp.SetOperation)):
            raise QueryGuardError("only SELECT and governed set queries are allowed")
        for forbidden in _FORBIDDEN_NODES:
            if statement.find(forbidden) is not None:
                raise QueryGuardError(f"forbidden SQL node: {forbidden.__name__}")
        if statement.args.get("limit") is None:
            raise QueryGuardError("physical SQL must contain a LIMIT")
        cte_names = {item.alias_or_name.casefold() for item in statement.find_all(exp.CTE)}

        dataset = next(
            (item for item in release.datasets if item.id == query.dataset_id),
            None,
        )
        if dataset is None:
            raise QueryGuardError("physical SQL references an unknown dataset")
        allowed: set[tuple[str, str]] = set()
        for model in release.models:
            if model.id not in dataset.model_ids:
                continue
            if model.query_type == "table_query":
                if model.table is not None:
                    allowed.add((model.schema_name or "public", model.table))
                continue
            try:
                governed_sql = validate_sql_model(
                    model.sql_query or "",
                    model.sql_variables,
                )
                # 保持 postgres：这是建模界面里写下的 SQL 模型源，属于内部 S2SQL，
                # 与数据源的执行方言无关，跟着数据源变会让已发布的模型解析不了。
                governed_statement = sqlglot.parse_one(governed_sql, read="postgres")
            except (SemanticValidationError, sqlglot.errors.ParseError) as exc:
                raise QueryGuardError("published SQL model source is invalid") from exc
            allowed.update(
                (table.db or "public", table.name)
                for table in governed_statement.find_all(exp.Table)
            )
        for table in statement.find_all(exp.Table):
            schema_name = table.db or "public"
            if not table.db and table.name.casefold() in cte_names:
                continue
            if (schema_name, table.name) not in allowed:
                raise QueryGuardError(
                    f"physical SQL references an unknown table: {schema_name}.{table.name}"
                )
