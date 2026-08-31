from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.contracts import (
    SuggestionPatch,
    SuggestionSource,
    TableCatalogEntry,
)
from knowflow_analytics.modeling.jobs import ModelingJobStage, ModelingJobStatus
from knowflow_analytics.modeling.revision import RevisionConflictError
from knowflow_analytics.semantic.index import EmbeddingBatch


class _Introspector:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def list_schemas(self):
        return tuple(sorted({item.schema_name for item in self.snapshot.tables}))

    def list_tables(self, *, schema_name, include_views=False):
        return tuple(
            TableCatalogEntry(
                schema_name=item.schema_name,
                name=item.name,
                source_type=item.source_type,
                comment=item.comment,
            )
            for item in self.snapshot.tables
            if item.schema_name == schema_name
        )

    def describe_table(self, *, schema_name, table_name, include_views=False):
        return next(
            item
            for item in self.snapshot.tables
            if item.schema_name == schema_name and item.name == table_name
        )

    def scan(self, **_kwargs):
        return self.snapshot


class _Embedding:
    def encode(self, texts):
        return EmbeddingBatch(model_id="t", dimension=1, vectors=tuple((1.0,) for _ in texts))


class _GatedModeller:
    """每张表阻塞在 gate 上，让测试能观察 running 中的进度、按需放行或注入失败。"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.gate = threading.Event()
        self.fail_on = fail_on
        self.calls: list[str] = []

    def suggest(self, *, revision, progress=None, should_stop=None, **_kwargs):
        patches = []
        for model in revision.semantic_spec.models:
            if should_stop is not None and should_stop():
                from knowflow_analytics.modeling.ai_modeller import ModelingCancelled

                raise ModelingCancelled(model.id)
            if progress:
                progress(model.id, model.name, "running", None)
            self.gate.wait(timeout=5)
            self.calls.append(model.id)
            if self.fail_on == model.id:
                if progress:
                    progress(model.id, model.name, "failed", "boom")
                raise RuntimeError("boom")
            if progress:
                progress(model.id, model.name, "completed", None)
            patches.append(
                SuggestionPatch(
                    id=f"suggestion:{revision.id}:{model.id}",
                    target_kind="model",
                    target_id=model.id,
                    changes={"name": f"AI {model.name}"},
                    source=SuggestionSource.AI_SCHEMA,
                    confidence=0.8,
                    reason="test",
                )
            )
        return tuple(patches)

    def suggest_alias_batch(self, *, resources, **_kwargs):
        from knowflow_analytics.modeling.ai_modeller import AliasSuggestionOutput

        return {str(item["resource_id"]): AliasSuggestionOutput(aliases=()) for item in resources}


def _app(schema_snapshot, modeller, tmp_path) -> AnalyticsApplication:
    # 文件库 + 普通连接池：job 在 worker 线程里写进度，主线程同时读 / 写；
    # :memory: + StaticPool 只有一条连接，两个线程一撞就丢行或报 API misuse。
    catalog = CatalogStore(
        create_engine(
            f"sqlite+pysqlite:///{tmp_path / 'jobs.db'}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
    )
    catalog.create_schema()
    return AnalyticsApplication(
        catalog=catalog,
        introspector=_Introspector(schema_snapshot),
        executor=object(),
        embedding_gateway=_Embedding(),
        ai_modeller=modeller,
        require_evaluation_for_publish=False,
        require_quality_report_for_publish=False,
        # sqlite StaticPool 单连接，两个 writer 线程会撞；生产是 Postgres。
        modeling_job_workers=1,
    )


def _revision_with_tables(application, *tables: str):
    application.create_project(project_id="sales", name="销售")
    snapshot = application.create_schema_snapshot(
        project_id="sales", schemas=("sales",), selected_tables={"sales": tables}
    )
    revision = application.create_empty_revision(project_id="sales", schema_snapshot_id=snapshot.id)
    for table in tables:
        revision = application.add_table_model(
            revision_id=revision.id,
            expected_etag=revision.etag,
            schema_snapshot_hash=revision.schema_snapshot_hash,
            schema_name="sales",
            table_name=table,
        )
    return revision


def _wait_terminal(application, job_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = application.get_modeling_job(job_id)
        if job.is_terminal:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal state")


def test_job_returns_immediately_and_reports_per_table_progress(schema_snapshot, tmp_path):
    """此前整条链路同步：HTTP 请求阻塞到所有 LLM 调用结束，唯一的 DB 写在最后，
    前端只有一个纯客户端秒表。"""

    modeller = _GatedModeller()
    application = _app(schema_snapshot, modeller, tmp_path)
    revision = _revision_with_tables(application, "customers", "orders")

    job = application.start_ai_modeling_job(
        revision_id=revision.id, expected_etag=revision.etag, created_by="analyst-1"
    )
    assert job.status is ModelingJobStatus.QUEUED
    assert {t.name for t in job.progress.tables} == {"customers", "orders"}
    assert modeller.calls == []  # 返回时模型还没跑

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = application.get_modeling_job(job.id)
        if current.status is ModelingJobStatus.RUNNING and any(
            t.status == "running" for t in current.progress.tables
        ):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("never observed a running table")
    assert current.stage is ModelingJobStage.MODELING

    modeller.gate.set()
    done = _wait_terminal(application, job.id)

    assert done.status is ModelingJobStatus.COMPLETED
    assert done.stage is ModelingJobStage.DONE
    assert done.proposal_id is not None
    assert {t.status for t in done.progress.tables} == {"completed"}
    assert all(t.attempts == 1 for t in done.progress.tables)
    assert application.get_modeling_proposal(done.proposal_id).revision_id == revision.id


def test_a_failed_table_lands_in_the_job_instead_of_leaving_it_running_forever(
    schema_snapshot, tmp_path
):
    modeller = _GatedModeller(fail_on=None)
    application = _app(schema_snapshot, modeller, tmp_path)
    revision = _revision_with_tables(application, "customers")
    modeller.fail_on = revision.semantic_spec.models[0].id
    modeller.gate.set()

    job = application.start_ai_modeling_job(
        revision_id=revision.id, expected_etag=revision.etag, created_by="analyst-1"
    )
    done = _wait_terminal(application, job.id)

    assert done.status is ModelingJobStatus.FAILED
    assert "boom" in (done.error or "")
    failed = next(t for t in done.progress.tables if t.status == "failed")
    assert failed.error == "boom"
    assert done.proposal_id is None


def test_cancel_stops_before_the_next_table_and_never_produces_a_proposal(
    schema_snapshot, tmp_path
):
    modeller = _GatedModeller()
    application = _app(schema_snapshot, modeller, tmp_path)
    revision = _revision_with_tables(application, "customers", "orders")

    job = application.start_ai_modeling_job(
        revision_id=revision.id, expected_etag=revision.etag, created_by="analyst-1"
    )
    # 等第一张表进入 running，再取消，再放行
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if any(t.status == "running" for t in application.get_modeling_job(job.id).progress.tables):
            break
        time.sleep(0.02)
    application.cancel_modeling_job(job.id)
    modeller.gate.set()
    done = _wait_terminal(application, job.id)

    assert done.status is ModelingJobStatus.CANCELLED
    assert done.proposal_id is None
    # 第二张表没被发起
    assert len(modeller.calls) == 1


def test_cancelling_a_queued_job_finishes_it_without_running(schema_snapshot, tmp_path):
    modeller = _GatedModeller()
    application = _app(schema_snapshot, modeller, tmp_path)
    revision = _revision_with_tables(application, "customers")
    # 占住唯一的 worker，让第二个 job 停在 queued
    blockers = [
        application.start_ai_modeling_job(
            revision_id=revision.id, expected_etag=revision.etag, created_by="a"
        )
    ]
    queued = application.start_ai_modeling_job(
        revision_id=revision.id, expected_etag=revision.etag, created_by="a"
    )
    time.sleep(0.05)
    assert application.get_modeling_job(queued.id).status is ModelingJobStatus.QUEUED

    cancelled = application.cancel_modeling_job(queued.id)
    assert cancelled.status is ModelingJobStatus.CANCELLED

    modeller.gate.set()
    for job in blockers:
        _wait_terminal(application, job.id)
    assert application.get_modeling_job(queued.id).status is ModelingJobStatus.CANCELLED
    assert len(modeller.calls) == 1


def test_stale_etag_is_rejected_before_anything_is_queued(schema_snapshot, tmp_path):
    application = _app(schema_snapshot, _GatedModeller(), tmp_path)
    revision = _revision_with_tables(application, "customers")

    with pytest.raises(RevisionConflictError):
        application.start_ai_modeling_job(
            revision_id=revision.id, expected_etag=revision.etag + 1, created_by="a"
        )
