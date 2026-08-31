from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.query.confirmation_memory import ConfirmationMemory


def _memory(*, memory_id: str = "mem-1", actor_id: str = "actor-1") -> ConfirmationMemory:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    return ConfirmationMemory(
        id=memory_id,
        actor_id=actor_id,
        project_id="sales",
        release_id="release-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
        detected_text="销售额",
        normalized_phrase="销售额",
        selection_kind="metric",
        semantic_element_id="net_revenue",
        candidate_set_hash="sha256:candidates",
        exact_context_hash="sha256:context",
        created_at=now,
        expires_at=now + timedelta(days=30),
    )


def test_catalog_confirmation_memory_is_version_actor_and_candidate_bound() -> None:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    memory = _memory()
    store.save_confirmation_memory(memory)

    found = store.find_confirmation_memory(
        actor_id="actor-1",
        project_id="sales",
        release_id="release-1",
        spec_hash="sha256:spec",
        index_snapshot_id="idx-1",
        normalized_phrase="销售额",
        candidate_set_hash="sha256:candidates",
        exact_context_hash="sha256:context",
        now=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert found == memory
    for changed in (
        {"actor_id": "actor-2"},
        {"release_id": "release-2"},
        {"spec_hash": "sha256:other"},
        {"index_snapshot_id": "idx-2"},
        {"candidate_set_hash": "sha256:other"},
        {"exact_context_hash": "sha256:other"},
    ):
        assert (
            store.find_confirmation_memory(
                actor_id=changed.get("actor_id", "actor-1"),
                project_id="sales",
                release_id=changed.get("release_id", "release-1"),
                spec_hash=changed.get("spec_hash", "sha256:spec"),
                index_snapshot_id=changed.get("index_snapshot_id", "idx-1"),
                normalized_phrase="销售额",
                candidate_set_hash=changed.get("candidate_set_hash", "sha256:candidates"),
                exact_context_hash=changed.get("exact_context_hash", "sha256:context"),
                now=datetime(2026, 8, 30, tzinfo=UTC),
            )
            is None
        )


def test_confirmation_memory_expires_and_can_be_revoked_without_cross_actor_access() -> None:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    memory = _memory()
    store.save_confirmation_memory(memory)

    assert (
        store.find_confirmation_memory(
            actor_id="actor-1",
            project_id="sales",
            release_id="release-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
            normalized_phrase="销售额",
            candidate_set_hash="sha256:candidates",
            exact_context_hash="sha256:context",
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )
        is None
    )
    assert not store.revoke_confirmation_memory(
        memory_id=memory.id,
        actor_id="actor-2",
        project_id="sales",
        revoked_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert store.revoke_confirmation_memory(
        memory_id=memory.id,
        actor_id="actor-1",
        project_id="sales",
        revoked_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert store.list_confirmation_memories(
        actor_id="actor-1",
        project_id="sales",
        include_revoked=True,
    )[0].revoked_at == datetime(2026, 8, 30, tzinfo=UTC)


def test_confirmation_memory_replay_is_idempotent_and_conflicts_abstain() -> None:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    memory = _memory()
    store.save_confirmation_memory(memory)
    store.save_confirmation_memory(
        memory.model_copy(
            update={
                "created_at": memory.created_at + timedelta(hours=1),
                "expires_at": memory.expires_at + timedelta(hours=1),
            }
        )
    )
    assert len(store.list_confirmation_memories(actor_id="actor-1", project_id="sales")) == 1

    store.save_confirmation_memory(
        memory.model_copy(
            update={
                "id": "mem-conflict",
                "semantic_element_id": "refund_amount",
            }
        )
    )
    assert (
        store.find_confirmation_memory(
            actor_id="actor-1",
            project_id="sales",
            release_id="release-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
            normalized_phrase="销售额",
            candidate_set_hash="sha256:candidates",
            exact_context_hash="sha256:context",
            now=datetime(2026, 8, 30, tzinfo=UTC),
        )
        is None
    )


def test_confirmation_memory_capacity_evicts_complete_conflict_bindings() -> None:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    original = _memory(memory_id="conflict-a")
    store.save_confirmation_memory(original)
    store.save_confirmation_memory(
        original.model_copy(
            update={
                "id": "conflict-b",
                "semantic_element_id": "refund_amount",
            }
        )
    )
    for index in range(499):
        created_at = original.created_at + timedelta(minutes=index + 1)
        store.save_confirmation_memory(
            original.model_copy(
                update={
                    "id": f"other-{index}",
                    "detected_text": f"phrase-{index}",
                    "normalized_phrase": f"phrase-{index}",
                    "candidate_set_hash": f"sha256:candidates-{index}",
                    "created_at": created_at,
                    "expires_at": created_at + timedelta(days=30),
                }
            )
        )

    assert (
        store.find_confirmation_memory(
            actor_id="actor-1",
            project_id="sales",
            release_id="release-1",
            spec_hash="sha256:spec",
            index_snapshot_id="idx-1",
            normalized_phrase="销售额",
            candidate_set_hash="sha256:candidates",
            exact_context_hash="sha256:context",
            now=datetime(2026, 8, 30, tzinfo=UTC),
        )
        is None
    )
