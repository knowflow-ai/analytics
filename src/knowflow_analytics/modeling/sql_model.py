from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_contracts import (
    SqlVariableContract,
    VariableValueType,
)

_DOLLAR_VARIABLE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\$")
_MUSTACHE_VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORBIDDEN_AST_KEYS = {
    "alter",
    "attach",
    "command",
    "commit",
    "copy",
    "create",
    "delete",
    "detach",
    "drop",
    "grant",
    "insert",
    "into",
    "lock",
    "merge",
    "rollback",
    "set",
    "transaction",
    "truncate",
    "update",
    "use",
}


def validate_sql_model(
    sql_query: str,
    sql_variables: tuple[dict[str, Any], ...],
) -> str:
    """Render defaults and validate one governed PostgreSQL SELECT.

    Template substitution plus a short sensitive-word regex is not enough here:
    SQL models are persisted product resources rather than ad-hoc browser SQL, so
    the boundary uses typed rendering and AST validation.
    """

    source = sql_query.strip()
    if not source:
        raise SemanticValidationError("SQL model query is empty", code="SQL_MODEL_EMPTY")
    variables = tuple(SqlVariableContract.model_validate(item) for item in sql_variables)
    names = [item.name.strip() for item in variables]
    if len(names) != len(set(names)):
        raise SemanticValidationError(
            "SQL model variable names must be unique",
            code="SQL_MODEL_VARIABLE_INVALID",
        )
    placeholders = set(_DOLLAR_VARIABLE.findall(source)) | set(_MUSTACHE_VARIABLE.findall(source))
    declared = set(names)
    if placeholders != declared:
        raise SemanticValidationError(
            "SQL model variables must exactly match query placeholders",
            code="SQL_MODEL_VARIABLE_INVALID",
        )
    rendered = source
    for variable in variables:
        replacement = _render_variable(variable)
        rendered = rendered.replace(f"${variable.name}$", replacement)
        rendered = re.sub(
            rf"\{{\{{\s*{re.escape(variable.name)}\s*\}}\}}",
            lambda _, value=replacement: value,
            rendered,
        )
    if "$" in rendered or "{{" in rendered or "}}" in rendered:
        raise SemanticValidationError(
            "SQL model contains an unsupported template expression",
            code="SQL_MODEL_TEMPLATE_UNSUPPORTED",
        )
    try:
        statements = sqlglot.parse(rendered, read="postgres")
    except ParseError as exc:
        raise SemanticValidationError(
            "SQL model query is not valid PostgreSQL SELECT syntax",
            code="SQL_MODEL_PARSE_FAILED",
        ) from exc
    statements = [item for item in statements if item is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise SemanticValidationError(
            "SQL model must contain exactly one SELECT query",
            code="SQL_MODEL_NOT_READ_ONLY",
        )
    forbidden = sorted(
        {node.key for node in statements[0].walk() if node.key in _FORBIDDEN_AST_KEYS}
    )
    if forbidden:
        raise SemanticValidationError(
            f"SQL model contains forbidden operations: {forbidden}",
            code="SQL_MODEL_NOT_READ_ONLY",
        )
    return rendered.rstrip().removesuffix(";").rstrip()


def compile_sql_model_source(
    sql_query: str,
    sql_variables: tuple[dict[str, Any], ...],
) -> str:
    return f"({validate_sql_model(sql_query, sql_variables)})"


def _render_variable(variable: SqlVariableContract) -> str:
    if not variable.default_values:
        raise SemanticValidationError(
            f"SQL model variable {variable.name} requires a default value",
            code="SQL_MODEL_VARIABLE_INVALID",
        )
    values = tuple(_render_scalar(variable.value_type, item) for item in variable.default_values)
    return ",".join(values)


def _render_scalar(value_type: VariableValueType, value: Any) -> str:
    if value_type is VariableValueType.STRING:
        return exp.Literal.string(str(value)).sql(dialect="postgres")
    if value_type is VariableValueType.NUMBER:
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise SemanticValidationError(
                "SQL model NUMBER variables require numeric defaults",
                code="SQL_MODEL_VARIABLE_INVALID",
            ) from exc
        if not math.isfinite(float(number)):
            raise SemanticValidationError(
                "SQL model NUMBER variables must be finite",
                code="SQL_MODEL_VARIABLE_INVALID",
            )
        return str(number)
    candidate = str(value).strip()
    if not _SAFE_IDENTIFIER.fullmatch(candidate):
        raise SemanticValidationError(
            "SQL model EXPR variables are limited to one SQL identifier",
            code="SQL_MODEL_VARIABLE_INVALID",
        )
    return '"' + candidate.replace('"', '""') + '"'
