from __future__ import annotations

from collections.abc import Set as AbstractSet

from knowflow_analytics.modeling.catalog_contracts import (
    JoinConditionContract,
    ModelRelationContract,
    SemanticCatalog,
)
from knowflow_analytics.modeling.contracts import SchemaSnapshot
from knowflow_analytics.modeling.relation_inference import infer_relation_candidates
from knowflow_analytics.modeling.rule_modeller import stable_id


def synchronize_database_relation_candidates(
    *,
    catalog: SemanticCatalog,
    snapshot: SchemaSnapshot,
    changed_model_ids: AbstractSet[str] | None = None,
) -> SemanticCatalog:
    """Add missing FK-backed ``ModelRela`` candidates to an editable Catalog.

    The AI ``ModelSchema`` contract carries no model relations, so only
    database-backed candidates are derived here, after both endpoint models
    have been imported.  Cardinality deliberately stays
    unset: the Candidate may display the FK edge, but publication remains blocked
    until a human confirms direction and cardinality through the normal relation
    resource API.
    """

    imported_model_ids = {model.id for model in catalog.models}
    existing_signatures = {
        _relation_signature(
            from_model_id=relation.from_model_id,
            to_model_id=relation.to_model_id,
            conditions=relation.join_conditions,
        )
        for relation in catalog.model_relations
    }
    existing_ids = {relation.id for relation in catalog.model_relations}
    candidates: list[ModelRelationContract] = []

    for table in snapshot.tables:
        from_model_id = stable_id("model", table.schema_name, table.name)
        if from_model_id not in imported_model_ids:
            continue
        for foreign_key in table.foreign_keys:
            to_model_id = stable_id(
                "model",
                foreign_key.referred_schema,
                foreign_key.referred_table,
            )
            if to_model_id not in imported_model_ids:
                continue
            if changed_model_ids is not None and not (
                {from_model_id, to_model_id} & changed_model_ids
            ):
                continue
            conditions = tuple(
                JoinConditionContract(
                    left_field=left,
                    right_field=right,
                    operator="=",
                )
                for left, right in zip(
                    foreign_key.constrained_columns,
                    foreign_key.referred_columns,
                    strict=True,
                )
            )
            signature = _relation_signature(
                from_model_id=from_model_id,
                to_model_id=to_model_id,
                conditions=conditions,
            )
            relation_id = stable_id(
                "relation",
                from_model_id,
                to_model_id,
                *(
                    f"{left}={right}"
                    for left, right in zip(
                        foreign_key.constrained_columns,
                        foreign_key.referred_columns,
                        strict=True,
                    )
                ),
            )
            if signature in existing_signatures or relation_id in existing_ids:
                continue
            candidates.append(
                ModelRelationContract(
                    id=relation_id,
                    from_model_id=from_model_id,
                    to_model_id=to_model_id,
                    join_type="left join",
                    join_conditions=conditions,
                    knowflow_cardinality=None,
                    knowflow_evidence="database_foreign_key",
                )
            )
            existing_signatures.add(signature)
            existing_ids.add(relation_id)

    # Databases that declare no foreign keys would otherwise produce a model of
    # disconnected single-table topics. Inference runs after the constraint pass
    # so a declared key always wins, and its output is an identical pending
    # candidate: same list, same confirmation, weaker stated evidence.
    candidates.extend(
        _inferred_candidates(
            snapshot=snapshot,
            imported_model_ids=imported_model_ids,
            changed_model_ids=changed_model_ids,
            existing_signatures=existing_signatures,
            existing_ids=existing_ids,
        )
    )

    if not candidates:
        return catalog
    return SemanticCatalog.model_validate(
        catalog.model_copy(
            update={"model_relations": (*catalog.model_relations, *candidates)}
        ).model_dump(mode="python")
    )


def _inferred_candidates(
    *,
    snapshot: SchemaSnapshot,
    imported_model_ids: set[str],
    changed_model_ids: AbstractSet[str] | None,
    existing_signatures: set[tuple[str, str, tuple[tuple[str, str], ...]]],
    existing_ids: set[str],
) -> list[ModelRelationContract]:
    """Turn name-convention proposals into pending relation candidates."""

    results: list[ModelRelationContract] = []
    for inferred in infer_relation_candidates(snapshot=snapshot):
        from_model_id = stable_id("model", inferred.from_schema, inferred.from_table)
        to_model_id = stable_id("model", inferred.to_schema, inferred.to_table)
        if from_model_id not in imported_model_ids or to_model_id not in imported_model_ids:
            continue
        if changed_model_ids is not None and not ({from_model_id, to_model_id} & changed_model_ids):
            continue
        conditions = (
            JoinConditionContract(
                left_field=inferred.from_column,
                right_field=inferred.to_column,
                operator="=",
            ),
        )
        signature = _relation_signature(
            from_model_id=from_model_id,
            to_model_id=to_model_id,
            conditions=conditions,
        )
        relation_id = stable_id(
            "relation",
            from_model_id,
            to_model_id,
            f"{inferred.from_column}={inferred.to_column}",
        )
        if signature in existing_signatures or relation_id in existing_ids:
            continue
        results.append(
            ModelRelationContract(
                id=relation_id,
                from_model_id=from_model_id,
                to_model_id=to_model_id,
                join_type="left join",
                join_conditions=conditions,
                knowflow_cardinality=None,
                knowflow_evidence="name_convention",
                knowflow_rationale=inferred.rationale,
            )
        )
        existing_signatures.add(signature)
        existing_ids.add(relation_id)
    return results


def _relation_signature(
    *,
    from_model_id: str,
    to_model_id: str,
    conditions: tuple[JoinConditionContract, ...],
) -> tuple[tuple[tuple[str, str], tuple[str, str]], ...]:
    """Treat the same equality relation as identical in either UI direction."""

    return tuple(
        sorted(
            tuple(
                sorted(
                    (
                        (from_model_id, condition.left_field),
                        (to_model_id, condition.right_field),
                    )
                )
            )
            for condition in conditions
        )
    )
