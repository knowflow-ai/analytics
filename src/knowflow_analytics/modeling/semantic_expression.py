from __future__ import annotations

from collections.abc import Callable, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from knowflow_analytics.errors import SemanticValidationError, TranslationError


def validate_dimension_expression(
    expression: str,
    *,
    available_fields: Iterable[str],
) -> tuple[str, ...]:
    """Validate a dimension expression and return the fields it references.

    Parity source: ``DimExpressionParser`` extracts every field referenced by
    ``Dimension.expr`` and replaces the dimension business name with that
    expression.  The Python runtime keeps the same expression contract while
    rejecting queries and unknown physical fields before publication.
    """

    parsed = _parse_expression(expression, code="DIMENSION_EXPRESSION_INVALID")
    if any(isinstance(node, (exp.AggFunc, exp.Window)) for node in parsed.walk()):
        raise SemanticValidationError(
            "dimension expressions cannot contain aggregate or window functions",
            code="DIMENSION_EXPRESSION_INVALID",
        )
    return _validate_columns(
        parsed,
        available_fields=available_fields,
        selected_fields=None,
        code="DIMENSION_EXPRESSION_INVALID",
    )


def validate_field_metric_expression(
    expression: str,
    *,
    available_fields: Iterable[str],
    selected_fields: Iterable[str],
) -> tuple[str, ...]:
    """Validate FIELD metric parameters.

    Parity source: ``MetricCheckUtils.checkParam`` requires at least one
    aggregate function for FIELD metrics, while ``MetricExpressionParser``
    expands the configured expression verbatim and records its referenced
    fields.
    """

    parsed = _parse_expression(expression, code="FIELD_METRIC_EXPRESSION_INVALID")
    if not any(isinstance(node, exp.AggFunc) for node in parsed.walk()):
        raise SemanticValidationError(
            "FIELD metric expressions require an aggregate function",
            code="FIELD_METRIC_EXPRESSION_INVALID",
        )
    if any(isinstance(node, exp.Window) for node in parsed.walk()):
        raise SemanticValidationError(
            "FIELD metric expressions cannot contain window functions",
            code="FIELD_METRIC_EXPRESSION_INVALID",
        )
    return _validate_columns(
        parsed,
        available_fields=available_fields,
        selected_fields=selected_fields,
        code="FIELD_METRIC_EXPRESSION_INVALID",
    )


def simple_field_metric(expression: str) -> tuple[str, str] | None:
    """Return the field and aggregate when a FIELD expression is atomic.

    This is an execution projection optimization only; the authoritative
    catalog continues to retain FIELD as its original define type.
    """

    parsed = _parse_expression(expression, code="FIELD_METRIC_EXPRESSION_INVALID")
    columns = tuple(parsed.find_all(exp.Column))
    if len(columns) != 1 or columns[0].table:
        return None
    aggregation = {
        exp.Sum: "sum",
        exp.Count: "count",
        exp.Avg: "avg",
        exp.Min: "min",
        exp.Max: "max",
    }.get(type(parsed))
    if aggregation is None:
        return None
    if isinstance(parsed, exp.Count) and isinstance(parsed.this, exp.Distinct):
        aggregation = "count_distinct"
        inner = parsed.this.expressions
        if len(inner) != 1 or not isinstance(inner[0], exp.Column):
            return None
    elif not isinstance(parsed.this, exp.Column):
        # 「恰好一列」不等于「实参就是那根裸列」:SUM(net_amount * 2) 也只有一列,
        # 此前被判成 simple 并编译成裸 SUM(net_amount)——* 2 被静默丢掉,数字对半
        # 错(实测 300 对 600)。带任何包装的表达式都必须走完整 FIELD 编译。
        return None
    return columns[0].name, aggregation


def validate_measure_metric_expression(
    expression: str,
    *,
    selected_measures: Iterable[str],
) -> tuple[str, ...]:
    """Validate MEASURE metric parameters.

    ``MetricCheckUtils`` forbids aggregate functions in this form because every
    selected model measure already owns its aggregate.  The expression may only
    reference the selected measure business names.
    """

    parsed = _parse_expression(expression, code="MEASURE_METRIC_EXPRESSION_INVALID")
    if any(isinstance(node, (exp.AggFunc, exp.Window)) for node in parsed.walk()):
        raise SemanticValidationError(
            "MEASURE metric expressions cannot contain aggregate or window functions",
            code="MEASURE_METRIC_EXPRESSION_INVALID",
        )
    return _validate_columns(
        parsed,
        available_fields=selected_measures,
        selected_fields=selected_measures,
        code="MEASURE_METRIC_EXPRESSION_INVALID",
    )


def validate_metric_metric_expression(
    expression: str,
    *,
    dependencies: Iterable[str],
) -> tuple[str, ...]:
    """Validate METRIC metric parameters (a metric composed of other metrics).

    ``MetricCheckUtils`` forbids aggregate functions here for the same reason as
    the MEASURE form: every dependency metric already owns its aggregate, so
    wrapping one again expands to a nested aggregate at translation time.

    Before this validator the METRIC branch was the only modeling path with no
    expression parsing at all: ``SUM(metricA)`` passed modeling and expanded to
    ``SUM((SUM(x))))`` downstream, and an undeclared token travelled straight
    into the physical SQL.
    """

    parsed = _parse_expression(expression, code="METRIC_METRIC_EXPRESSION_INVALID")
    if any(isinstance(node, (exp.AggFunc, exp.Window)) for node in parsed.walk()):
        raise SemanticValidationError(
            "METRIC metric expressions cannot contain aggregate or window functions",
            code="METRIC_METRIC_EXPRESSION_INVALID",
        )
    return _validate_columns(
        parsed,
        available_fields=dependencies,
        selected_fields=dependencies,
        code="METRIC_METRIC_EXPRESSION_INVALID",
    )


def render_semantic_expression(
    expression: str,
    *,
    resolve_column: Callable[[str], str],
    code: str,
) -> str:
    """Render a governed expression by replacing semantic columns only."""

    try:
        parsed = sqlglot.parse_one(expression, read="postgres")
    except ParseError as exc:
        raise TranslationError("semantic expression is invalid", code=code) from exc
    if isinstance(parsed, exp.Query):
        raise TranslationError("semantic expression cannot be a query", code=code)

    def replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Column):
            return node
        if node.table:
            raise TranslationError(
                "semantic expression cannot reference a table qualifier",
                code=code,
            )
        try:
            return sqlglot.parse_one(resolve_column(node.name), read="postgres")
        except (ParseError, KeyError) as exc:
            raise TranslationError(
                f"semantic expression references an unknown field: {node.name}",
                code=code,
            ) from exc

    return parsed.transform(replace, copy=True).sql(dialect="postgres")


def _parse_expression(expression: str, *, code: str) -> exp.Expression:
    source = expression.strip()
    if not source:
        raise SemanticValidationError("semantic expression is empty", code=code)
    try:
        statements = sqlglot.parse(source, read="postgres")
    except ParseError as exc:
        raise SemanticValidationError("semantic expression is invalid", code=code) from exc
    statements = [item for item in statements if item is not None]
    if len(statements) != 1 or isinstance(statements[0], exp.Query):
        raise SemanticValidationError(
            "semantic expression must contain exactly one expression",
            code=code,
        )
    forbidden = (exp.Subquery, exp.DML, exp.DDL)
    if any(isinstance(node, forbidden) for node in statements[0].walk()):
        raise SemanticValidationError(
            "semantic expression contains an unsupported query operation",
            code=code,
        )
    return statements[0]


def _validate_columns(
    expression: exp.Expression,
    *,
    available_fields: Iterable[str],
    selected_fields: Iterable[str] | None,
    code: str,
) -> tuple[str, ...]:
    available = {item.casefold(): item for item in available_fields}
    referenced: list[str] = []
    for column in expression.find_all(exp.Column):
        if column.table:
            raise SemanticValidationError(
                "semantic expressions cannot reference table-qualified fields",
                code=code,
            )
        canonical = available.get(column.name.casefold())
        if canonical is None:
            raise SemanticValidationError(
                f"semantic expression references an unknown field: {column.name}",
                code=code,
            )
        if canonical not in referenced:
            referenced.append(canonical)
    if not referenced:
        raise SemanticValidationError(
            "semantic expression must reference at least one governed field: "
            f"{expression.sql(dialect='postgres')!r}",
            code=code,
        )
    if selected_fields is not None:
        selected = {item.casefold() for item in selected_fields}
        actual = {item.casefold() for item in referenced}
        if selected != actual:
            raise SemanticValidationError(
                "selected fields must exactly match expression references",
                code=code,
            )
    return tuple(referenced)
