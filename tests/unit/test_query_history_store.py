from __future__ import annotations

from sqlalchemy import create_engine

from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.query.contracts import MapMode, MappingResult
from knowflow_analytics.query.multi_turn import QueryHistoryTurn


def _turn(*, question: str, query_id: str) -> QueryHistoryTurn:
    return QueryHistoryTurn(
        question=question,
        effective_question=question,
        corrected_s2sql=f'SELECT SUM("净收入") FROM "销售经营" /* {query_id} */',
        mapping=MappingResult(
            dataset_id="sales_dataset",
            mode=MapMode.STRICT,
            normalized_question=question,
            matches=(),
            config_version="test",
        ),
        dataset_id="sales_dataset",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
    )


def test_catalog_query_history_is_actor_release_and_topic_scoped() -> None:
    catalog = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    catalog.create_schema()
    catalog.save_success(
        _turn(question="第一问", query_id="q1"),
        actor_id="user-1",
        project_id="project-1",
        conversation_id="chat-1",
    )
    catalog.save_success(
        _turn(question="第二问", query_id="q2"),
        actor_id="user-1",
        project_id="project-1",
        conversation_id="chat-1",
    )

    latest = catalog.last_success(
        actor_id="user-1",
        project_id="project-1",
        conversation_id="chat-1",
        release_id="release-1",
        spec_hash="spec-1",
        index_snapshot_id="index-1",
        dataset_id="sales_dataset",
    )

    assert latest is not None
    assert latest.question == "第二问"
    assert (
        catalog.last_success(
            actor_id="user-2",
            project_id="project-1",
            conversation_id="chat-1",
            release_id="release-1",
            spec_hash="spec-1",
            index_snapshot_id="index-1",
            dataset_id="sales_dataset",
        )
        is None
    )
    assert (
        catalog.last_success(
            actor_id="user-1",
            project_id="project-1",
            conversation_id="chat-1",
            release_id="release-1",
            spec_hash="changed-spec",
            index_snapshot_id="index-1",
            dataset_id="sales_dataset",
        )
        is None
    )
