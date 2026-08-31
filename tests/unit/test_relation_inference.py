from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.modeling.contracts import (
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.relation_inference import (
    RelationInferenceEvidence,
    infer_relation_candidates,
)


def _column(name, data_type="integer", *, primary_key=False, unique=False):
    return SchemaColumnSnapshot(
        name=name,
        data_type=data_type,
        nullable=not primary_key,
        ordinal_position=0,
        primary_key=primary_key,
        unique=unique,
    )


def _snapshot(*tables) -> SchemaSnapshot:
    return SchemaSnapshot.create(
        database_name="analytics",
        tables=tuple(tables),
        captured_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def _orders_customers() -> SchemaSnapshot:
    return _snapshot(
        TableSnapshot(
            schema_name="public",
            name="orders",
            columns=(
                _column("id", primary_key=True),
                _column("customer_id"),
                _column("amount", "numeric"),
            ),
        ),
        TableSnapshot(
            schema_name="public",
            name="customers",
            columns=(_column("id", primary_key=True), _column("name", "varchar")),
        ),
    )


def test_foreign_key_style_naming_is_inferred_when_no_constraint_exists():
    """Databases that omit FK constraints are the common private-deployment case;
    without inference the model degrades to disconnected single-table topics."""

    candidates = infer_relation_candidates(snapshot=_orders_customers())

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.from_table == "orders"
    assert candidate.to_table == "customers"
    assert candidate.from_column == "customer_id"
    assert candidate.to_column == "id"
    assert candidate.evidence is RelationInferenceEvidence.NAME_CONVENTION


def test_inference_requires_the_target_column_to_be_unique():
    """A join onto a non-unique column fans out and silently multiplies metrics."""

    snapshot = _snapshot(
        TableSnapshot(
            schema_name="public",
            name="orders",
            columns=(_column("id", primary_key=True), _column("customer_id")),
        ),
        TableSnapshot(
            schema_name="public",
            name="customers",
            columns=(_column("id"), _column("name", "varchar")),
        ),
    )

    assert infer_relation_candidates(snapshot=snapshot) == ()


def test_inference_rejects_incompatible_physical_types():
    snapshot = _snapshot(
        TableSnapshot(
            schema_name="public",
            name="orders",
            columns=(
                _column("id", primary_key=True),
                _column("customer_id", "varchar"),
            ),
        ),
        TableSnapshot(
            schema_name="public",
            name="customers",
            columns=(_column("id", "integer", primary_key=True),),
        ),
    )

    assert infer_relation_candidates(snapshot=snapshot) == ()


def test_existing_foreign_keys_are_never_re_inferred():
    """Database constraints are authoritative; inference must not duplicate them."""

    from knowflow_analytics.modeling.contracts import ForeignKeySnapshot

    snapshot = _snapshot(
        TableSnapshot(
            schema_name="public",
            name="orders",
            columns=(_column("id", primary_key=True), _column("customer_id")),
            foreign_keys=(
                ForeignKeySnapshot(
                    constrained_columns=("customer_id",),
                    referred_schema="public",
                    referred_table="customers",
                    referred_columns=("id",),
                ),
            ),
        ),
        TableSnapshot(
            schema_name="public",
            name="customers",
            columns=(_column("id", primary_key=True),),
        ),
    )

    assert infer_relation_candidates(snapshot=snapshot) == ()


def test_ambiguous_role_columns_are_both_reported():
    """Shipping and billing address columns are a real multi-path schema. Both are
    surfaced so a human resolves the roles instead of one silently disappearing."""

    snapshot = _snapshot(
        TableSnapshot(
            schema_name="public",
            name="orders",
            columns=(
                _column("id", primary_key=True),
                _column("ship_address_id"),
                _column("bill_address_id"),
            ),
        ),
        TableSnapshot(
            schema_name="public",
            name="addresses",
            columns=(_column("id", primary_key=True),),
        ),
    )

    candidates = infer_relation_candidates(snapshot=snapshot)

    assert {item.from_column for item in candidates} == {
        "ship_address_id",
        "bill_address_id",
    }
    assert all(item.to_table == "addresses" for item in candidates)


def test_inference_is_restricted_to_the_selected_table_scope():
    snapshot = _orders_customers()

    assert infer_relation_candidates(snapshot=snapshot, table_scope={"orders"}) == ()


def test_candidates_are_deterministic_and_carry_no_cardinality():
    """Inference proposes an edge; direction and cardinality stay human decisions
    exactly as they do for database foreign keys."""

    first = infer_relation_candidates(snapshot=_orders_customers())
    second = infer_relation_candidates(snapshot=_orders_customers())

    assert [item.id for item in first] == [item.id for item in second]
    assert all(item.confidence <= 1.0 for item in first)


def _stripped(schema_snapshot):
    return schema_snapshot.model_copy(
        update={
            "tables": tuple(
                table.model_copy(update={"foreign_keys": ()}) for table in schema_snapshot.tables
            )
        }
    )


def _import_both_tables(application):
    application.create_project(project_id="sales", name="销售分析")
    snapshot = application.create_schema_snapshot(
        project_id="sales",
        schemas=("sales",),
        selected_tables={"sales": ("customers", "orders")},
    )
    revision = application.create_empty_revision(
        project_id="sales",
        schema_snapshot_id=snapshot.id,
    )
    for table in ("customers", "orders"):
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table,
        )
    return revision


def test_inferred_relations_appear_as_candidates_without_a_user_action(
    schema_snapshot,
):
    """Foreign-key candidates already surface automatically on table import.
    A name-inferred edge must reach the same pending list the same way, so the
    user confirms one kind of proposal instead of hunting for a second entry."""

    from tests.unit.test_api_first_modeling import _application

    revision = _import_both_tables(_application(_stripped(schema_snapshot)))
    relations = revision.semantic_catalog.model_relations

    assert len(relations) == 1
    relation = relations[0]
    assert relation.knowflow_cardinality is None  # still awaiting confirmation
    assert relation.knowflow_evidence == "name_convention"
    assert relation.knowflow_rationale


def test_database_foreign_keys_keep_their_stronger_evidence(schema_snapshot):
    from tests.unit.test_api_first_modeling import _application

    revision = _import_both_tables(_application(schema_snapshot))
    relations = revision.semantic_catalog.model_relations

    assert len(relations) == 1
    assert relations[0].knowflow_evidence == "database_foreign_key"


def test_inference_never_competes_with_a_declared_constraint(schema_snapshot):
    """The declared key already covers orders->customers, so inference must not
    add a second edge for the same pair."""

    from tests.unit.test_api_first_modeling import _application

    revision = _import_both_tables(_application(schema_snapshot))

    assert len(revision.semantic_catalog.model_relations) == 1


def test_relation_evidence_survives_a_catalog_round_trip(schema_snapshot):
    """Evidence drives what the modeling page shows, so it must not be lost when
    the catalog is revalidated."""

    from knowflow_analytics.modeling.catalog_contracts import (
        SemanticCatalog,
    )
    from tests.unit.test_api_first_modeling import _application

    revision = _import_both_tables(_application(_stripped(schema_snapshot)))
    reloaded = SemanticCatalog.model_validate(revision.semantic_catalog.model_dump(mode="python"))

    assert reloaded.model_relations[0].knowflow_evidence == "name_convention"
