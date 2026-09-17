"""建模页的就地查数：读缓存不碰库，核对一次只打一条语句。

这两条路是分开的，因为代价差三个数量级：翻一屏编辑器要显示十几个对象的状态，
那必须是纯读；真去量一遍是全表扫描，必须是用户主动点的、限流的、一次一个。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.modeling.fact_checks import (
    FACT_CHECK_STATEMENT_TIMEOUT_MS,
    FactCheckKind,
    metric_subject_id,
)
from knowflow_analytics.modeling.quality import (
    ModelGrainProfile,
    ModelingQualityError,
    ModelingQualityProfiler,
    QualityStatus,
)


class _Profiler:
    """只回答单对象核对，并记下用了多长的语句上限。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.statement_timeouts: list[int] = []
        self.grain_calls: list[str] = []

    def with_statement_timeout(self, statement_timeout_ms: int) -> _Profiler:
        self.statement_timeouts.append(statement_timeout_ms)
        return self

    def profile_grain(self, model, release) -> ModelGrainProfile:
        if self.fail:
            raise ModelingQualityError("太慢了，先没量完", code="MODELING_QUALITY_FAILED")
        self.grain_calls.append(model.id)
        return ModelGrainProfile(
            model_id=model.id,
            identifier_field_ids=("orders.id",),
            total_rows=44224,
            null_rows=0,
            distinct_non_null_keys=411,
            duplicate_rows=43813,
            uniqueness_rate=411 / 44224,
            null_rate=0.0,
            status=QualityStatus.BLOCKING,
            message="主标识在当前数据中重复。",
        )


def _application(sales_catalog, *, profiler: _Profiler):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    catalog = CatalogStore(engine)
    catalog.create_schema()
    catalog.create_project(project_id="sales", name="销售")
    semantic_catalog = sales_catalog.model_copy(update={"revision_id": "revision-fact"})
    revision = ModelingRevision(
        id="revision-fact",
        project_id="sales",
        schema_snapshot_hash="sha256:schema",
        etag=7,
        state=RevisionState.VALIDATED,
        semantic_catalog=semantic_catalog,
        semantic_spec=compile_semantic_catalog(semantic_catalog),
    )
    catalog.save_revision(revision)
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=object(),
        executor=Mock(),
        embedding_gateway=Mock(),
        quality_profiler=profiler,
    )
    return application, revision, engine


@pytest.fixture()
def app(sales_catalog):
    profiler = _Profiler()
    application, revision, engine = _application(sales_catalog, profiler=profiler)
    try:
        yield application, revision, profiler
    finally:
        application.close()
        engine.dispose()


def test_listing_never_touches_the_customer_database(app) -> None:
    application, revision, profiler = app

    entries = application.list_fact_checks(revision_id="revision-fact")

    assert profiler.grain_calls == []
    assert profiler.statement_timeouts == []
    kinds = {item.kind for item in entries}
    assert FactCheckKind.GRAIN in kinds
    assert all(item.result is None for item in entries), "还没量过，就是没量过"
    # 样例行没有结论，不进列表。
    assert FactCheckKind.ROWS not in kinds


def test_listing_covers_every_model_relation_and_open_metric(app, sales_catalog) -> None:
    application, revision, _ = app
    release = revision.semantic_spec

    entries = application.list_fact_checks(revision_id="revision-fact")
    by_kind: dict[FactCheckKind, set[str]] = {}
    for item in entries:
        by_kind.setdefault(item.kind, set()).add(item.subject_id)

    assert by_kind[FactCheckKind.GRAIN] == {item.id for item in release.models}
    assert by_kind[FactCheckKind.RELATION] == {item.id for item in release.relations}
    assert by_kind[FactCheckKind.METRIC] == {
        metric_subject_id(dataset.id, metric_id)
        for dataset in release.datasets
        for metric_id in dataset.metric_ids
    }


def test_a_check_is_cached_and_the_next_listing_shows_it(app) -> None:
    application, revision, profiler = app
    model_id = revision.semantic_spec.models[0].id

    ran = application.run_fact_check(
        revision_id="revision-fact",
        kind=FactCheckKind.GRAIN,
        subject_id=model_id,
        expected_etag=7,
        schema_snapshot_hash="sha256:schema",
    )

    assert ran.result is not None
    assert ran.result.payload["duplicate_rows"] == 43813
    # 建模页点一下不能等三十秒。
    assert profiler.statement_timeouts == [FACT_CHECK_STATEMENT_TIMEOUT_MS]

    listed = next(
        item
        for item in application.list_fact_checks(revision_id="revision-fact")
        if item.kind is FactCheckKind.GRAIN and item.subject_id == model_id
    )
    assert listed.result is not None
    assert listed.subject_hash == ran.subject_hash
    assert listed.expired is False
    assert profiler.grain_calls == [model_id], "第二次是读缓存，不再扫库"


def test_an_old_measurement_still_shows_but_says_it_may_have_drifted(app) -> None:
    """键没变不代表数字还是昨天那个。数据会漂移，所以时间也是结论的一部分。"""

    application, revision, _ = app
    model_id = revision.semantic_spec.models[0].id
    ran = application.run_fact_check(
        revision_id="revision-fact",
        kind=FactCheckKind.GRAIN,
        subject_id=model_id,
        expected_etag=7,
        schema_snapshot_hash="sha256:schema",
    )
    stale = ran.result.model_copy(update={"computed_at": datetime.now(UTC) - timedelta(days=3)})
    application.catalog.save_fact_check(project_id="sales", record=stale)

    listed = next(
        item
        for item in application.list_fact_checks(revision_id="revision-fact")
        if item.subject_hash == ran.subject_hash
    )

    assert listed.result is not None, "过期不是消失——旧数字仍然比没有数字有用"
    assert listed.expired is True


def test_a_query_that_is_too_slow_says_so_and_is_not_remembered(sales_catalog) -> None:
    """量不完是给建模者看的结论，但不能进缓存：否则这个对象在改动之前再也量不到。"""

    profiler = _Profiler(fail=True)
    application, revision, engine = _application(sales_catalog, profiler=profiler)
    model_id = revision.semantic_spec.models[0].id
    try:
        entry = application.run_fact_check(
            revision_id="revision-fact",
            kind=FactCheckKind.GRAIN,
            subject_id=model_id,
            expected_etag=7,
            schema_snapshot_hash="sha256:schema",
        )

        assert entry.result is not None
        assert entry.result.status is QualityStatus.WARNING
        assert "太慢" in entry.result.payload["message"]
        assert (
            application.catalog.load_fact_checks(
                project_id="sales", subject_hashes=(entry.subject_hash,)
            )
            == {}
        )
    finally:
        application.close()
        engine.dispose()


def test_checking_against_a_stale_draft_is_refused(app) -> None:
    application, revision, _ = app

    with pytest.raises(Exception) as raised:
        application.run_fact_check(
            revision_id="revision-fact",
            kind=FactCheckKind.GRAIN,
            subject_id=revision.semantic_spec.models[0].id,
            expected_etag=6,
            schema_snapshot_hash="sha256:schema",
        )

    assert "etag" in str(raised.value).lower()


def test_the_real_profiler_hands_out_a_shorter_statement_timeout() -> None:
    profiler = ModelingQualityProfiler(Mock(), Mock(), statement_timeout_ms=30_000)

    quick = profiler.with_statement_timeout(FACT_CHECK_STATEMENT_TIMEOUT_MS)

    assert quick._statement_timeout_ms == FACT_CHECK_STATEMENT_TIMEOUT_MS
    assert profiler._statement_timeout_ms == 30_000


def test_the_two_routes_hand_the_page_what_it_needs(app) -> None:
    application, revision, _ = app
    client = TestClient(
        create_api(application=application, service_secret="s" * 32),
        raise_server_exceptions=False,
    )
    headers = {
        "X-KnowFlow-Service-Token": "s" * 32,
        "X-KnowFlow-Actor-Id": "model-owner",
        "X-KnowFlow-Project-Id": "sales",
        "X-KnowFlow-Permission-Scope-Hash": "modeling-scope-v1",
    }
    base = f"/v1/analytics/projects/sales/revisions/{revision.id}/fact-checks"
    model_id = revision.semantic_spec.models[0].id

    listed = client.get(base, headers=headers)
    assert listed.status_code == 200, listed.text
    assert all(item["result"] is None for item in listed.json()["entries"])

    ran = client.post(
        base,
        headers=headers,
        json={
            "kind": "grain",
            "subject_id": model_id,
            "expected_etag": revision.etag,
            "schema_snapshot_hash": revision.schema_snapshot_hash,
        },
    )
    assert ran.status_code == 200, ran.text
    entry = ran.json()["entry"]
    assert entry["result"]["status"] == "blocking"
    assert entry["result"]["payload"]["duplicate_rows"] == 43813

    again = client.get(base, headers=headers)
    hit = next(
        item for item in again.json()["entries"] if item["subject_hash"] == entry["subject_hash"]
    )
    assert hit["result"]["payload"]["duplicate_rows"] == 43813
