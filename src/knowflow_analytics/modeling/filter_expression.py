from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from knowflow_analytics.contracts import FieldSpec, FilterOperator, FixedFilter
from knowflow_analytics.errors import SemanticValidationError

_BINARY_OPERATORS: dict[type[exp.Expression], FilterOperator] = {
    exp.EQ: FilterOperator.EQ,
    exp.NEQ: FilterOperator.NE,
    exp.GT: FilterOperator.GT,
    exp.GTE: FilterOperator.GTE,
    exp.LT: FilterOperator.LT,
    exp.LTE: FilterOperator.LTE,
    exp.Like: FilterOperator.LIKE,
}


def compile_fixed_filters(
    expressions: Iterable[str | None],
    *,
    model_id: str,
    fields: Iterable[FieldSpec],
    allowed_qualifiers: Iterable[str] = (),
) -> tuple[FixedFilter, ...]:
    """Compile governed filterSql and constraint expressions.

    ``MetricDefineParams.filterSql`` and ``Measure.constraint`` are preserved in
    the catalog, and unlike a model-level-only filter both metric fields are
    executable here through the same deterministic, parameterized
    predicate contract.  It intentionally accepts only conjunctions of physical
    columns and literals; arbitrary SQL, functions, subqueries, and OR expressions
    remain fail-closed.
    """

    field_index = {field.column.casefold(): field for field in fields if field.model_id == model_id}
    qualifiers = {item.casefold() for item in allowed_qualifiers if item}
    compiled: list[FixedFilter] = []
    for raw in expressions:
        if raw is None or not raw.strip():
            continue
        try:
            expression = parse_one(raw, read="postgres")
        except ParseError as exc:
            raise _unsupported("fixed filter is not valid PostgreSQL syntax") from exc
        for predicate in _flatten_and(expression):
            compiled.append(
                _compile_predicate(
                    predicate,
                    field_index=field_index,
                    allowed_qualifiers=qualifiers,
                )
            )
            if len(compiled) > 50:
                raise _unsupported("fixed filter contains more than 50 predicates")
    return _deduplicate(compiled)


def combine_filter_sql(expressions: Iterable[str | None]) -> str | None:
    parts = [item.strip() for item in expressions if item is not None and item.strip()]
    if not parts:
        return None
    unique = tuple(dict.fromkeys(parts))
    return " AND ".join(f"({item})" for item in unique)


def _flatten_and(expression: exp.Expression) -> tuple[exp.Expression, ...]:
    if isinstance(expression, exp.Paren):
        return _flatten_and(expression.this)
    if isinstance(expression, exp.And):
        return (*_flatten_and(expression.left), *_flatten_and(expression.right))
    if isinstance(expression, exp.Or):
        raise _unsupported("OR is not supported in governed fixed filters")
    return (expression,)


def _compile_predicate(
    expression: exp.Expression,
    *,
    field_index: dict[str, FieldSpec],
    allowed_qualifiers: set[str],
) -> FixedFilter:
    if isinstance(expression, exp.Paren):
        return _compile_predicate(
            expression.this,
            field_index=field_index,
            allowed_qualifiers=allowed_qualifiers,
        )

    negated = isinstance(expression, exp.Not)
    target = expression.this if negated else expression
    if isinstance(target, exp.In):
        field = _resolve_column(target.this, field_index, allowed_qualifiers)
        if target.args.get("query") is not None:
            raise _unsupported("subqueries are not supported in governed fixed filters")
        values = tuple(_literal(item) for item in target.expressions)
        if not values:
            raise _unsupported("IN requires at least one literal")
        if any(value is None for value in values):
            raise _unsupported("IN cannot contain NULL")
        return FixedFilter(
            field_id=field.id,
            operator=FilterOperator.NOT_IN if negated else FilterOperator.IN,
            value=values,
        )
    if isinstance(target, exp.Between):
        if negated:
            raise _unsupported("NOT BETWEEN is not supported in governed fixed filters")
        field = _resolve_column(target.this, field_index, allowed_qualifiers)
        return FixedFilter(
            field_id=field.id,
            operator=FilterOperator.BETWEEN,
            value=(_literal(target.args["low"]), _literal(target.args["high"])),
        )
    if isinstance(target, exp.Is):
        field = _resolve_column(target.this, field_index, allowed_qualifiers)
        if not isinstance(target.expression, exp.Null):
            raise _unsupported("IS only supports NULL in governed fixed filters")
        return FixedFilter(
            field_id=field.id,
            operator=(FilterOperator.IS_NOT_NULL if negated else FilterOperator.IS_NULL),
        )
    if negated:
        raise _unsupported("NOT is only supported for IN and IS NULL")

    for node_type, operator in _BINARY_OPERATORS.items():
        if isinstance(target, node_type):
            field = _resolve_column(target.left, field_index, allowed_qualifiers)
            value = _literal(target.right)
            if value is None and operator not in {FilterOperator.EQ, FilterOperator.NE}:
                raise _unsupported("NULL only supports equality predicates")
            if value is None:
                operator = (
                    FilterOperator.IS_NULL
                    if operator is FilterOperator.EQ
                    else FilterOperator.IS_NOT_NULL
                )
            return FixedFilter(field_id=field.id, operator=operator, value=value)
    raise _unsupported(f"unsupported fixed-filter predicate: {expression.sql(dialect='postgres')}")


def _resolve_column(
    expression: exp.Expression,
    field_index: dict[str, FieldSpec],
    allowed_qualifiers: set[str],
) -> FieldSpec:
    if not isinstance(expression, exp.Column):
        raise _unsupported("the left side of a fixed filter must be a physical column")
    if expression.table and expression.table.casefold() not in allowed_qualifiers:
        raise _unsupported("fixed filter references an unexpected table qualifier")
    field = field_index.get(expression.name.casefold())
    if field is None:
        raise SemanticValidationError(
            f"fixed filter references an unknown physical field: {expression.name}",
            code="FIXED_FILTER_FIELD_NOT_FOUND",
        )
    return field


def _literal(expression: exp.Expression) -> Any:
    if isinstance(expression, exp.Paren):
        return _literal(expression.this)
    if isinstance(expression, exp.Null):
        return None
    if isinstance(expression, exp.Boolean):
        return bool(expression.this)
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return expression.this
        try:
            value = Decimal(expression.this)
        except InvalidOperation as exc:
            raise _unsupported("fixed filter contains an invalid numeric literal") from exc
        return int(value) if value == value.to_integral_value() else value
    if isinstance(expression, exp.Neg):
        value = _literal(expression.this)
        if not isinstance(value, (int, Decimal)):
            raise _unsupported("unary minus requires a numeric literal")
        return -value
    raise _unsupported("fixed filter values must be literals")


def _deduplicate(filters: list[FixedFilter]) -> tuple[FixedFilter, ...]:
    unique: dict[str, FixedFilter] = {}
    for item in filters:
        key = item.model_dump_json()
        unique.setdefault(key, item)
    return tuple(unique.values())


def _unsupported(message: str) -> SemanticValidationError:
    return SemanticValidationError(
        message,
        code="FIXED_FILTER_EXPRESSION_UNSUPPORTED",
    )
