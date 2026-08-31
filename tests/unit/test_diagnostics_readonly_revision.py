from __future__ import annotations

from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState
from knowflow_analytics.modeling.diagnostics import ModelingDiagnosticsAnalyzer
from knowflow_analytics.modeling.revision import RevisionEditor


def _revision(sales_release, state: RevisionState) -> ModelingRevision:
    return ModelingRevision(
        id="revision-readonly",
        project_id=sales_release.project_id,
        schema_snapshot_hash="sha256:snapshot",
        etag=3,
        state=state,
        semantic_spec=sales_release,
    )


def test_published_revision_does_not_report_a_fake_blocker(sales_release) -> None:
    """诊断末尾兜底调用 validate_for_publish，而 _require_editable 对
    FROZEN/PUBLISHED 一律抛 RevisionConflictError。

    结果是：打开任何已发布版本，待办清单里都会稳定出现一条
    "published revisions are immutable" 的阻断项 —— 那不是建模问题，
    是版本状态问题，用户对它无能为力。
    """

    analyzer = ModelingDiagnosticsAnalyzer(RevisionEditor())
    report = analyzer.analyze(_revision(sales_release, RevisionState.PUBLISHED))

    codes = {item.diagnostic_code for item in report.diagnostics}
    assert "REVISION_CONFLICT" not in codes


def test_frozen_revision_is_treated_the_same(sales_release) -> None:
    analyzer = ModelingDiagnosticsAnalyzer(RevisionEditor())
    report = analyzer.analyze(_revision(sales_release, RevisionState.FROZEN))
    assert "REVISION_CONFLICT" not in {item.diagnostic_code for item in report.diagnostics}


def test_a_draft_still_gets_the_publish_validation_preview(sales_release) -> None:
    """草稿仍要保留兜底预演 —— 它是发布校验的只读预告，不能一并关掉。"""

    analyzer = ModelingDiagnosticsAnalyzer(RevisionEditor())
    report = analyzer.analyze(_revision(sales_release, RevisionState.DRAFT))
    assert isinstance(report.ready, bool)
