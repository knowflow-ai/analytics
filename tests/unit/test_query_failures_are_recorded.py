from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.catalog.store import CatalogStore, PublishedRelease
from knowflow_analytics.query.contracts import QueryFailureRecord, QueryRequest, QueryState
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _CapturingFailures:
    def __init__(self) -> None:
        self.saved: list[tuple[QueryFailureRecord, str, str]] = []

    def save_failure(self, record, *, actor_id, project_id):
        self.saved.append((record, actor_id, project_id))


class _ExplodingFailures:
    def save_failure(self, record, *, actor_id, project_id):
        raise RuntimeError("disk full")


def _service(sales_release, sales_index, failures):
    return AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=None,
        query_failures=failures,
    )


def test_a_question_nobody_understood_is_kept_with_what_did_match(sales_release, sales_index):
    """失败问句此前直接丢弃，"系统听不懂哪些说法"这份数据从未落地。

    映射失败是最值钱的一种：它发生在多轮改写之前，effective_question 那时还没
    赋值 —— 记录它不能把一次干净的拒答变成 UnboundLocalError。
    """

    failures = _CapturingFailures()
    response = _service(sales_release, sales_index, failures).query(
        QueryRequest(project_id="sales", question="火星殖民地人口", dataset_ids=("sales_dataset",)),
        actor_id="  analyst-1  ",
    )

    assert response.state is QueryState.FAILED
    assert response.error.code == "NO_SEMANTIC_MAPPING"
    assert len(failures.saved) == 1
    record, actor_id, project_id = failures.saved[0]
    assert record.question == "火星殖民地人口"
    assert record.effective_question == "火星殖民地人口"
    assert record.stage == "CANDIDATE_DISCOVERY"
    assert record.code == "NO_SEMANTIC_MAPPING"
    assert record.dataset_ids == ("sales_dataset",)
    # 挖掘需要知道这一轮命中了什么，MappingError 把各次尝试放在 details 里。
    assert "mapping_attempts" in record.details
    assert actor_id == "analyst-1"
    assert project_id == "sales"


def test_a_broken_failure_log_never_turns_a_refusal_into_a_500(sales_release, sales_index):
    response = _service(sales_release, sales_index, _ExplodingFailures()).query(
        QueryRequest(project_id="sales", question="火星殖民地人口", dataset_ids=("sales_dataset",))
    )

    assert response.state is QueryState.FAILED
    assert response.error.code == "NO_SEMANTIC_MAPPING"


def test_nothing_is_recorded_when_no_store_is_configured(sales_release, sales_index):
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=None,
    )
    response = service.query(
        QueryRequest(project_id="sales", question="火星殖民地人口", dataset_ids=("sales_dataset",))
    )
    assert response.state is QueryState.FAILED


def test_catalog_store_round_trips_a_failure_newest_first():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = CatalogStore(engine)
    store.create_schema()

    def _record(question: str) -> QueryFailureRecord:
        return QueryFailureRecord(
            question=question,
            stage="CANDIDATE_DISCOVERY",
            code="NO_SEMANTIC_MAPPING",
            release_id="rel",
            spec_hash="sha256:spec",
            index_snapshot_id="idx",
            details={"mapping_attempts": []},
        )

    store.save_failure(_record("第一条"), actor_id="a", project_id="sales")
    store.save_failure(_record("第二条"), actor_id="a", project_id="sales")
    store.save_failure(_record("别的项目"), actor_id="a", project_id="other")

    listed = store.list_failures(project_id="sales")
    assert [item.question for item in listed] == ["第二条", "第一条"]
    assert listed[0].details == {"mapping_attempts": []}
