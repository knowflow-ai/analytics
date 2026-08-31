"""Propose model relations for databases that declare no foreign keys.

Private deployments frequently run schemas with no FK constraints at all: they
were dropped for load performance, lost in a migration, or never existed in an
ODS/warehouse layer. ``relation_candidates`` only reads real constraints, so on
those databases the semantic model degrades to a set of disconnected single-table
topics and every cross-table question fails with ``MISSING_JOIN_PATH``.

This module closes that gap without weakening governance. It produces *proposals*
only: like a database foreign key, an inferred edge carries no cardinality and
cannot reach an active release until a human confirms direction and cardinality
through the normal relation resource API. It never overrides a declared
constraint, and it deliberately reports ambiguous role columns rather than
picking one, because choosing between a shipping and a billing address is a
modeling decision and not a naming puzzle.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from enum import StrEnum

from pydantic import Field

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.modeling.contracts import SchemaSnapshot, TableSnapshot
from knowflow_analytics.modeling.rule_modeller import stable_id
from knowflow_analytics.modeling.type_system import types_can_join

_ID_SUFFIXES: tuple[str, ...] = ("_id", "_key", "_code", "_no", "id")


class RelationInferenceEvidence(StrEnum):
    """Why an edge was proposed. Database constraints are a separate, stronger path."""

    NAME_CONVENTION = "name_convention"


class InferredRelationCandidate(FrozenModel):
    """One proposed join edge awaiting human confirmation."""

    id: str = Field(min_length=1, max_length=128)
    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str
    evidence: RelationInferenceEvidence
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(max_length=1_000)


def infer_relation_candidates(
    *,
    snapshot: SchemaSnapshot,
    table_scope: AbstractSet[str] | None = None,
) -> tuple[InferredRelationCandidate, ...]:
    """Propose FK-shaped edges that the database does not declare.

    An edge is proposed only when every condition holds: the source column name
    resolves to another in-scope table, the target column is a primary key or a
    unique column, the physical types can be compared, and no declared foreign key
    already covers the pair. The uniqueness requirement is what keeps a proposal
    from introducing silent fan-out, which would multiply metric values.
    """

    tables = [
        table for table in snapshot.tables if table_scope is None or table.name in table_scope
    ]
    by_name: dict[str, TableSnapshot] = {}
    for table in tables:
        # A bare table name is ambiguous across schemas; such a target cannot be
        # resolved from a column name alone, so it is left for manual modeling.
        by_name.setdefault(table.name.casefold(), table)
        by_name.setdefault(_singularize(table.name.casefold()), table)

    declared = _declared_foreign_key_pairs(tables)
    candidates: list[InferredRelationCandidate] = []
    for table in sorted(tables, key=lambda item: (item.schema_name, item.name)):
        for column in sorted(table.columns, key=lambda item: item.name):
            target = _resolve_target(column.name, by_name)
            if target is None or target.name == table.name:
                continue
            if (table.name, column.name) in declared:
                continue
            target_column = _unique_join_column(target)
            if target_column is None:
                continue
            if not types_can_join(column.data_type, target_column.data_type):
                continue
            candidates.append(
                InferredRelationCandidate(
                    id=stable_id(
                        "inferred_relation",
                        f"{table.schema_name}.{table.name}.{column.name}",
                        f"{target.schema_name}.{target.name}.{target_column.name}",
                    ),
                    from_schema=table.schema_name,
                    from_table=table.name,
                    from_column=column.name,
                    to_schema=target.schema_name,
                    to_table=target.name,
                    to_column=target_column.name,
                    evidence=RelationInferenceEvidence.NAME_CONVENTION,
                    confidence=0.6,
                    rationale=(
                        f"列 {column.name} 按命名约定指向 {target.name}."
                        f"{target_column.name}，且目标列具备唯一性；"
                        "方向与基数仍需人工确认。"
                    ),
                )
            )
    return tuple(candidates)


def _declared_foreign_key_pairs(
    tables: list[TableSnapshot],
) -> set[tuple[str, str]]:
    return {
        (table.name, column)
        for table in tables
        for foreign_key in table.foreign_keys
        for column in foreign_key.constrained_columns
    }


def _resolve_target(
    column_name: str,
    by_name: dict[str, TableSnapshot],
) -> TableSnapshot | None:
    """Resolve the table a column points at, tolerating a role prefix.

    ``ship_address_id`` and ``bill_address_id`` both target ``addresses``; the
    prefix names the role, not a different table. Progressively dropping leading
    segments finds the referenced table while keeping each column a separate
    candidate, so the roles stay distinguishable for the human who confirms them.
    """

    stem = _referenced_table_hint(column_name)
    if stem is None:
        return None
    segments = stem.split("_")
    for start in range(len(segments)):
        candidate = "_".join(segments[start:])
        if not candidate:
            continue
        target = by_name.get(candidate) or by_name.get(_singularize(candidate))
        if target is not None:
            return target
    return None


def _referenced_table_hint(column_name: str) -> str | None:
    """Derive the table a column name points at, or None when it names none."""

    lowered = column_name.casefold()
    for suffix in _ID_SUFFIXES:
        if not lowered.endswith(suffix) or len(lowered) <= len(suffix):
            continue
        stem = lowered[: -len(suffix)].strip("_")
        if stem:
            return stem
    return None


def _unique_join_column(table: TableSnapshot):
    """Return the single column a join may safely target.

    A composite primary key has no single safe target, and several unique columns
    leave the intended key ambiguous, so both are left to manual modeling.
    """

    primary = [item for item in table.columns if item.primary_key]
    if len(primary) == 1:
        return primary[0]
    if primary:
        return None
    unique = [item for item in table.columns if item.unique]
    return unique[0] if len(unique) == 1 else None


def _singularize(name: str) -> str:
    if name.endswith("ies") and len(name) > 3:
        return f"{name[:-3]}y"
    if name.endswith("ses") and len(name) > 3:
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss") and len(name) > 1:
        return name[:-1]
    return name
