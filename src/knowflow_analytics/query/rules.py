from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from knowflow_analytics.contracts import (
    QueryRuleMode,
    QueryRuleType,
    SemanticRelease,
)
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.s2sql_ast import validate_textual_s2sql
from knowflow_analytics.query.symbols import SemanticSymbolTable


class QueryRuleApplication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    corrected_s2sql: str
    applied_rule_ids: tuple[str, ...] = ()


class QueryRuleEngine:
    """Consume immutable QueryRule resources before semantic translation.

    ``ADD_DATE`` and ``ADD_SELECT`` are consumed on the query path here, and the
    mutation stays deterministic, dataset-scoped and semantic-ID based.
    """

    def apply(
        self,
        *,
        release: SemanticRelease,
        dataset_id: str,
        corrected_s2sql: str,
        now: datetime | None = None,
    ) -> QueryRuleApplication:
        dataset = next(item for item in release.datasets if item.id == dataset_id)
        tree = validate_textual_s2sql(corrected_s2sql).copy()
        symbols = SemanticSymbolTable.from_release(release, dataset_id=dataset_id)
        rules = sorted(
            (
                item
                for item in release.query_rules
                if item.enabled and item.dataset_id == dataset_id
            ),
            key=lambda item: (-item.priority, item.id),
        )
        applied: list[str] = []
        date_consumed = False
        original_dimensions = _resolved_dimension_ids(tree, symbols)
        for rule in rules:
            if rule.rule_type is QueryRuleType.ADD_DATE:
                if date_consumed or _has_time_predicate(tree, release, symbols):
                    continue
                dimension_id = dataset.default_time_dimension_id
                if dimension_id is None:
                    continue
                dimension = next(item for item in release.dimensions if item.id == dimension_id)
                count = int(rule.parameters[0])
                current = now or datetime.now(UTC)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=UTC)
                boundary = current.astimezone(ZoneInfo(dataset.timezone)).date() - timedelta(
                    days=count
                )
                column = exp.column(dimension.name, quoted=True)
                literal = exp.cast(exp.Literal.string(boundary.isoformat()), "DATE")
                predicate: exp.Expression = (
                    exp.GTE(this=column, expression=literal)
                    if rule.mode is QueryRuleMode.RECENT
                    else exp.LT(this=column, expression=literal)
                )
                for select in _dataset_selects(tree, symbols):
                    _append_where(select, predicate.copy())
                applied.append(rule.id)
                date_consumed = True
                continue

            triggers = {str(item) for item in rule.parameters}
            if not triggers.issubset(original_dimensions):
                continue
            dimensions = {item.id: item for item in release.dimensions}
            for select in _dataset_selects(tree, symbols):
                aggregate = any(
                    isinstance(item, exp.AggFunc) or item.find(exp.AggFunc) is not None
                    for item in select.expressions
                )
                projected = _resolved_dimension_ids(select, symbols)
                for dimension_id in rule.outputs:
                    if dimension_id in projected:
                        continue
                    column = exp.column(dimensions[dimension_id].name, quoted=True)
                    select.append("expressions", column.copy())
                    if aggregate:
                        group = select.args.get("group")
                        if group is None:
                            select.set("group", exp.Group(expressions=[column.copy()]))
                        else:
                            group.append("expressions", column.copy())
            applied.append(rule.id)
        return QueryRuleApplication(
            corrected_s2sql=tree.sql(dialect="postgres"),
            applied_rule_ids=tuple(applied),
        )


def _dataset_selects(tree: exp.Query, symbols: SemanticSymbolTable) -> tuple[exp.Select, ...]:
    selects = list(tree.find_all(exp.Select))
    if isinstance(tree, exp.Select):
        selects.insert(0, tree)
    return tuple(
        dict.fromkeys(
            select
            for select in selects
            if any(symbols.is_dataset(table.name) for table in select.find_all(exp.Table))
        )
    )


def _resolved_dimension_ids(
    tree: exp.Expression,
    symbols: SemanticSymbolTable,
) -> set[str]:
    results: set[str] = set()
    for column in tree.find_all(exp.Column):
        try:
            resolved = symbols.resolve_first(column.name)
        except SemanticParsingError:  # aliases and CTE columns are not governed source fields
            continue
        if resolved.kind == "dimension":
            results.add(resolved.id)
    return results


def _has_time_predicate(
    tree: exp.Query,
    release: SemanticRelease,
    symbols: SemanticSymbolTable,
) -> bool:
    time_ids = {item.id for item in release.dimensions if item.semantic_type == "time"}
    for select in _dataset_selects(tree, symbols):
        where = select.args.get("where")
        if where is not None and _resolved_dimension_ids(where, symbols).intersection(time_ids):
            return True
    return False


def _append_where(select: exp.Select, predicate: exp.Expression) -> None:
    where = select.args.get("where")
    if where is None:
        select.set("where", exp.Where(this=predicate))
    else:
        where.set("this", exp.and_(where.this, predicate))
