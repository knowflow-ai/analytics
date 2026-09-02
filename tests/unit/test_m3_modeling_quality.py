from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.quality import (
    MetricPreview,
    MetricPreviewDecision,
    ModelingQualityError,
    ModelingQualityProfiler,
    ModelingQualityReport,
    ModelingQualityReportStatus,
    QualityStatus,
)


def test_metric_dimension_matrix_exposes_safe_and_fanout_paths(sales_release):
    cells = ModelingQualityProfiler._reachability_matrix(sales_release)
    index = {(item.metric_id, item.dimension_id): item for item in cells}

    assert index[("net_revenue", "customer_segment")].status is QualityStatus.PASSED
    assert index[("net_revenue", "customer_segment")].relation_ids == ("orders_customer",)
    assert index[("net_revenue", "product")].status is QualityStatus.BLOCKING
    assert index[("net_revenue", "product")].reason_code == "FANOUT_RISK"
    assert index[("net_revenue", "product")].relation_ids == ("orders_items",)


def test_reachability_is_invariant_to_business_name_changes(sales_release):
    renamed = sales_release.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"name": f"renamed-{index}"})
                for index, item in enumerate(sales_release.models)
            ),
            "metrics": tuple(
                item.model_copy(update={"name": f"metric-{index}"})
                for index, item in enumerate(sales_release.metrics)
            ),
            "dimensions": tuple(
                item.model_copy(update={"name": f"dimension-{index}"})
                for index, item in enumerate(sales_release.dimensions)
            ),
        }
    )

    original = ModelingQualityProfiler._reachability_matrix(sales_release)
    changed = ModelingQualityProfiler._reachability_matrix(renamed)

    assert [
        (item.metric_id, item.dimension_id, item.status, item.reason_code) for item in changed
    ] == [(item.metric_id, item.dimension_id, item.status, item.reason_code) for item in original]


def test_metric_sample_review_requires_one_decision_per_preview(sales_release):
    revision = ModelingRevision(
        id="revision-quality",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=7,
        semantic_spec=sales_release,
    )
    previews = (
        MetricPreview(
            id="preview-revenue",
            dataset_id="sales_dataset",
            metric_id="net_revenue",
            columns=("net_revenue",),
            rows=((380,),),
        ),
        MetricPreview(
            id="preview-orders",
            dataset_id="sales_dataset",
            metric_id="order_count",
            columns=("order_count",),
            rows=((3,),),
        ),
    )
    report = ModelingQualityReport(
        id="quality-1",
        project_id=revision.project_id,
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_spec_hash=revision.semantic_spec.spec_hash,
        etag=1,
        content_hash="sha256:quality",
        ready=False,
        blocking_count=0,
        warning_count=0,
        metric_previews=previews,
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    profiler = ModelingQualityProfiler(Mock(), Mock())

    with pytest.raises(ModelingQualityError) as exc_info:
        profiler.review(
            report,
            decisions=(MetricPreviewDecision(preview_id="preview-revenue", confirm=True),),
            reviewed_by="analyst-1",
        )

    assert exc_info.value.code == "QUALITY_REVIEW_INCOMPLETE"

    reviewed = profiler.review(
        report,
        decisions=tuple(
            MetricPreviewDecision(preview_id=item.id, confirm=True) for item in previews
        ),
        reviewed_by="analyst-1",
    )
    assert reviewed.status is ModelingQualityReportStatus.REVIEWED
    assert reviewed.ready is True
    assert all(item.status is QualityStatus.CONFIRMED for item in reviewed.metric_previews)


def test_metric_sample_review_cannot_override_execution_failure(sales_release):
    revision = ModelingRevision(
        id="revision-quality-error",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:schema",
        etag=3,
        semantic_spec=sales_release,
    )
    report = ModelingQualityReport(
        id="quality-error",
        project_id=revision.project_id,
        revision_id=revision.id,
        revision_etag=revision.etag,
        schema_snapshot_hash=revision.schema_snapshot_hash,
        semantic_spec_hash=revision.semantic_spec.spec_hash,
        etag=1,
        content_hash="sha256:quality-error",
        ready=False,
        blocking_count=1,
        warning_count=0,
        metric_previews=(
            MetricPreview(
                id="preview-ok",
                dataset_id="sales_dataset",
                metric_id="net_revenue",
                rows=((380,),),
            ),
            MetricPreview(
                id="preview-failed",
                dataset_id="sales_dataset",
                metric_id="order_count",
                status=QualityStatus.BLOCKING,
                error_code="QUERY_FAILED",
                message="preview failed",
            ),
        ),
        created_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    profiler = ModelingQualityProfiler(Mock(), Mock())

    with pytest.raises(ModelingQualityError) as exc_info:
        profiler.review(
            report,
            decisions=(
                MetricPreviewDecision(preview_id="preview-ok", confirm=True),
                MetricPreviewDecision(preview_id="preview-failed", confirm=True),
            ),
            reviewed_by="analyst-1",
        )
    assert exc_info.value.code == "QUALITY_REVIEW_INVALID"

    reviewed = profiler.review(
        report,
        decisions=(MetricPreviewDecision(preview_id="preview-ok", confirm=True),),
        reviewed_by="analyst-1",
    )

    assert reviewed.ready is False
    assert reviewed.blocking_count == 1
    assert reviewed.metric_previews[0].status is QualityStatus.CONFIRMED
    assert reviewed.metric_previews[1].status is QualityStatus.BLOCKING
