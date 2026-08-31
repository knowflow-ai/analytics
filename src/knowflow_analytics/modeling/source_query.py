"""Compile one governed model source for offline profiling and diagnostics."""

from __future__ import annotations

from typing import Any

from knowflow_analytics.contracts import (
    FilterOperator,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.modeling.sql_model import compile_sql_model_source


def compile_governed_model_source(
    model: ModelSpec,
    release: SemanticRelease,
    *,
    parameter_prefix: str = "model_filter",
) -> tuple[str, dict[str, Any]]:
    """Return the same model row scope consumed by runtime semantic queries."""

    if model.query_type == "sql_query":
        relation = (
            f"{compile_sql_model_source(model.sql_query or '', model.sql_variables)} "
            "AS governed_sql_model"
        )
    else:
        relation = f"{_quote_identifier(model.schema_name)}.{_quote_identifier(model.table)}"
    source = f"SELECT * FROM {relation}"
    if not model.filters:
        return source, {}

    field_by_id = {item.id: item for item in release.fields}
    clauses: list[str] = []
    parameters: dict[str, Any] = {}
    for index, item in enumerate(model.filters):
        field = field_by_id[item.field_id]
        clause, values = _compile_filter(
            column=_quote_identifier(field.column),
            operator=item.operator,
            value=item.value,
            parameter_prefix=f"{parameter_prefix}_{index}",
        )
        clauses.append(clause)
        parameters.update(values)
    return f"{source} WHERE {' AND '.join(clauses)}", parameters


def _quote_identifier(value: str | None) -> str:
    if not value or "\x00" in value or len(value.encode("utf-8")) > 63:
        raise ValueError("model source requires a valid PostgreSQL identifier")
    return '"' + value.replace('"', '""') + '"'


def _compile_filter(
    *,
    column: str,
    operator: FilterOperator,
    value: Any,
    parameter_prefix: str,
) -> tuple[str, dict[str, Any]]:
    if operator is FilterOperator.IS_NULL:
        return f"{column} IS NULL", {}
    if operator is FilterOperator.IS_NOT_NULL:
        return f"{column} IS NOT NULL", {}
    if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
        values = list(value)
        names = [f"{parameter_prefix}_{index}" for index in range(len(values))]
        keyword = "IN" if operator is FilterOperator.IN else "NOT IN"
        return (
            f"{column} {keyword} ({', '.join(':' + name for name in names)})",
            dict(zip(names, values, strict=True)),
        )
    if operator is FilterOperator.BETWEEN:
        lower, upper = value
        return (
            f"{column} BETWEEN :{parameter_prefix}_0 AND :{parameter_prefix}_1",
            {f"{parameter_prefix}_0": lower, f"{parameter_prefix}_1": upper},
        )
    keyword = {
        FilterOperator.EQ: "=",
        FilterOperator.NE: "!=",
        FilterOperator.GT: ">",
        FilterOperator.GTE: ">=",
        FilterOperator.LT: "<",
        FilterOperator.LTE: "<=",
        FilterOperator.LIKE: "LIKE",
    }[operator]
    return f"{column} {keyword} :{parameter_prefix}", {parameter_prefix: value}
