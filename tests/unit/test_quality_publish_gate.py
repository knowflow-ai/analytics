from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from knowflow_analytics.catalog.release import ReleasePublisher
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.quality import (
    ModelingQualityReport,
    ModelingQualityReportStatus,
)


def _revision(sales_release) -> ModelingRevision:
    return ModelingRevision(
        id="revision-publish-gate",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=3,
        semantic_spec=sales_release,
    )


def _report(revision: ModelingRevision, *, ready: bool, blocking: int) -> ModelingQualityReport:
    return ModelingQualityReport(
        id="quality-gate-1",
        project_id=revision.project_id,
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_spec_hash=revision.semantic_spec.spec_hash,
        etag=1,
        status=ModelingQualityReportStatus.REVIEWED,
        content_hash="sha256:quality",
        ready=ready,
        blocking_count=blocking,
        warning_count=0,
        metric_previews=(),
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        reviewed_by="analyst-1",
        reviewed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def _publisher(catalog, *, require_quality: bool = True) -> ReleasePublisher:
    revision_editor = Mock()
    revision_editor.validate_for_publish.side_effect = lambda revision: revision
    index_builder = Mock()
    index_builder.build.return_value = Mock(release_spec_hash=None)
    return ReleasePublisher(
        catalog=catalog,
        revision_editor=revision_editor,
        index_builder=index_builder,
        require_evaluation=False,
        require_quality_report=require_quality,
    )


def test_publish_is_blocked_when_the_quality_report_has_blocking_findings(sales_release):
    """Duplicate primary keys and unreachable metrics are detected by the quality
    profiler; a release must not go live while they stand."""

    revision = _revision(sales_release)
    catalog = Mock()
    catalog.get_latest_modeling_quality_report.return_value = _report(
        revision, ready=False, blocking=2
    )

    with pytest.raises(SemanticValidationError) as raised:
        _publisher(catalog).publish(revision)

    assert raised.value.code == "QUALITY_GATE_FAILED"
    catalog.publish.assert_not_called()


def test_publish_is_blocked_when_no_quality_report_exists(sales_release):
    revision = _revision(sales_release)
    catalog = Mock()
    catalog.get_latest_modeling_quality_report.return_value = None

    with pytest.raises(SemanticValidationError) as raised:
        _publisher(catalog).publish(revision)

    assert raised.value.code == "QUALITY_GATE_FAILED"
    catalog.publish.assert_not_called()


def test_publish_proceeds_when_the_quality_report_is_ready(sales_release):
    revision = _revision(sales_release)
    catalog = Mock()
    catalog.get_latest_modeling_quality_report.return_value = _report(
        revision, ready=True, blocking=0
    )

    _publisher(catalog).publish(revision)

    catalog.publish.assert_called_once()


def test_quality_gate_can_be_disabled_for_deployments_that_opt_out(sales_release):
    revision = _revision(sales_release)
    catalog = Mock()
    catalog.get_latest_modeling_quality_report.return_value = None

    _publisher(catalog, require_quality=False).publish(revision)

    catalog.publish.assert_called_once()


def test_quality_report_lookup_uses_the_real_store(sales_release):
    """The gate mocks the catalog elsewhere, so exercise the actual SQL once."""

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from knowflow_analytics.catalog.store import CatalogStore

    catalog = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    catalog.create_schema()
    revision = _revision(sales_release)
    report = _report(revision, ready=True, blocking=0)
    catalog.save_modeling_quality_report(report)

    found = catalog.get_latest_modeling_quality_report(revision.semantic_spec.spec_hash)
    assert found is not None
    assert found.id == report.id
    assert catalog.get_latest_modeling_quality_report("sha256:other") is None


def test_gate_looks_up_evidence_by_the_evidence_hash(sales_release):
    """门禁必须按证据哈希取报告，否则改一个中文名就会说"缺少质量报告"。"""

    from knowflow_analytics.hashing import semantic_evidence_hash

    revision = _revision(sales_release)
    catalog = Mock()
    catalog.get_latest_modeling_quality_report.return_value = None

    with pytest.raises(SemanticValidationError):
        _publisher(catalog).publish(revision)

    catalog.get_latest_modeling_quality_report.assert_called_once_with(
        semantic_evidence_hash(revision.semantic_spec)
    )


def test_publisher_exposes_the_gate_thresholds_it_enforces():
    """前端此前写死 30 条 / 100%；运维通过 KNOWFLOW_ANALYTICS_MINIMUM_* 调低后，
    UI 仍卡在 30。门槛必须能从判定它的同一个对象上读出来。"""

    publisher = ReleasePublisher(
        catalog=None,
        revision_editor=None,
        index_builder=None,
        require_evaluation=True,
        minimum_evaluation_cases=12,
        minimum_accuracy=0.95,
    )

    assert publisher.gate_thresholds == {
        "minimum_evaluation_cases": 12,
        "minimum_accuracy": 0.95,
    }
