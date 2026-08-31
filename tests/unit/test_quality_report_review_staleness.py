from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.hashing import semantic_evidence_hash
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.modeling.quality import (
    ModelingQualityReport,
    ModelingQualityReportStatus,
    modeling_quality_report_is_stale,
)

CONTENT_HASH = "sha256:content"


def _revision(sales_release, *, etag: int = 3) -> ModelingRevision:
    return ModelingRevision(
        id="revision-1",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=etag,
        state=RevisionState.DRAFT,
        semantic_spec=sales_release,
    )


def _report(revision, sales_release, **overrides) -> ModelingQualityReport:
    payload = {
        "id": "report-1",
        "project_id": revision.project_id,
        "revision_id": revision.id,
        "revision_etag": revision.etag,
        "schema_snapshot_hash": revision.schema_snapshot_hash,
        # 生成侧写入的是 evidence hash，不是 semantic_spec.spec_hash。
        "semantic_spec_hash": semantic_evidence_hash(sales_release),
        "etag": 1,
        "content_hash": CONTENT_HASH,
        "status": ModelingQualityReportStatus.COMPLETED,
        "ready": True,
        "blocking_count": 0,
        "warning_count": 0,
        "created_at": datetime(2026, 8, 22, tzinfo=UTC),
    }
    payload.update(overrides)
    return ModelingQualityReport(**payload)


def test_a_freshly_generated_report_can_be_reviewed(sales_release) -> None:
    """报告的 semantic_spec_hash 存的是 evidence hash，而 Revision 上是
    spec_hash。此前拿两者直接比对，必然不等，于是刚跑完体检、点「提交指标
    样本核对」就报 stale；而发布门禁又要求先完成核对，用户被彻底卡死。
    """

    revision = _revision(sales_release)
    report = _report(revision, sales_release)

    assert not modeling_quality_report_is_stale(
        report,
        revision,
        expected_etag=report.etag,
        expected_content_hash=CONTENT_HASH,
    )


def test_editing_the_draft_still_invalidates_the_report(sales_release) -> None:
    """etag 才是「草稿被编辑过」的判据，这条不能一起放松。"""

    report = _report(_revision(sales_release, etag=3), sales_release)
    edited = _revision(sales_release, etag=4)

    assert modeling_quality_report_is_stale(
        report,
        edited,
        expected_etag=report.etag,
        expected_content_hash=CONTENT_HASH,
    )


def test_a_changed_schema_snapshot_invalidates_the_report(sales_release) -> None:
    revision = _revision(sales_release)
    report = _report(revision, sales_release, schema_snapshot_hash="sha256:other")

    assert modeling_quality_report_is_stale(
        report,
        revision,
        expected_etag=report.etag,
        expected_content_hash=CONTENT_HASH,
    )


def test_a_tampered_content_hash_invalidates_the_report(sales_release) -> None:
    revision = _revision(sales_release)
    report = _report(revision, sales_release)

    assert modeling_quality_report_is_stale(
        report,
        revision,
        expected_etag=report.etag,
        expected_content_hash="sha256:someone-else",
    )
