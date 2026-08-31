from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import PhysicalQuery, SemanticRelease
from knowflow_analytics.errors import QueryGuardError, SemanticValidationError
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
    """Fail-closed physical SQL validation performed immediately before execution."""

    def validate(self, *, query: PhysicalQuery, release: SemanticRelease) -> None:
        if _COMMENT_RE.search(query.sql):
            raise QueryGuardError("SQL comments are not allowed")
        try:
            statements = sqlglot.parse(query.sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise QueryGuardError("physical SQL is not valid PostgreSQL") from exc
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
