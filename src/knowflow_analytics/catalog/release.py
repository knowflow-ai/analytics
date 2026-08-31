from __future__ import annotations

from knowflow_analytics.catalog.store import CatalogStore, PublishedRelease
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.hashing import semantic_evidence_hash
from knowflow_analytics.modeling.contracts import ModelingRevision
from knowflow_analytics.modeling.quality import QualityStatus
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.semantic.index import SemanticIndexBuilder


def describe_evaluation_gate_failure(
    *,
    total: int | None,
    accuracy: float | None,
    gate_passed: bool,
    minimum_cases: int,
    minimum_accuracy: float,
) -> str:
    """Say exactly what is missing before publication can proceed.

    The gate previously reported only that it had not passed, leaving the user to
    guess whether they were short on cases, on accuracy, or both.
    """

    if total is None or accuracy is None:
        return (
            f"尚未运行黄金问题评测。发布需要至少 {minimum_cases} 条用例，"
            f"且准确率达到 {minimum_accuracy:.0%}。"
        )
    gaps: list[str] = []
    if total < minimum_cases:
        gaps.append(f"还需要 {minimum_cases - total} 条黄金问题（当前 {total} 条）")
    if accuracy < minimum_accuracy:
        gaps.append(f"当前准确率 {accuracy:.1%}，需要达到 {minimum_accuracy:.0%}")
    if not gaps and not gate_passed:
        gaps.append("评测报告本身未通过门禁，请检查失败用例")
    return "发布被准确率门禁拦截：" + "；".join(gaps) + "。"


class ReleasePublisher:
    def __init__(
        self,
        *,
        catalog: CatalogStore,
        revision_editor: RevisionEditor,
        index_builder: SemanticIndexBuilder,
        require_evaluation: bool = True,
        minimum_evaluation_cases: int = 30,
        minimum_accuracy: float = 1.0,
        require_quality_report: bool = True,
    ) -> None:
        self._catalog = catalog
        self._revision_editor = revision_editor
        self._index_builder = index_builder
        self._require_evaluation = require_evaluation
        self._minimum_evaluation_cases = minimum_evaluation_cases
        self._minimum_accuracy = minimum_accuracy
        self._require_quality_report = require_quality_report

    @property
    def gate_thresholds(self) -> dict[str, int | float]:
        """发布门槛的只读投影，供 UI 显示"还差几条"，不参与判定。"""

        return {
            "minimum_evaluation_cases": self._minimum_evaluation_cases,
            "minimum_accuracy": self._minimum_accuracy,
        }

    def publish(self, revision: ModelingRevision) -> PublishedRelease:
        validated = self._revision_editor.validate_for_publish(revision)
        if validated != revision:
            self._catalog.update_revision(validated, previous_etag=revision.etag)
        self._require_quality_evidence(validated)
        if self._require_evaluation:
            report = self._catalog.get_latest_evaluation(validated.semantic_spec.spec_hash)
            if (
                report is None
                or report.total < self._minimum_evaluation_cases
                or report.accuracy < self._minimum_accuracy
                or not report.gate_passed
            ):
                raise SemanticValidationError(
                    describe_evaluation_gate_failure(
                        total=None if report is None else report.total,
                        accuracy=None if report is None else report.accuracy,
                        gate_passed=bool(report and report.gate_passed),
                        minimum_cases=self._minimum_evaluation_cases,
                        minimum_accuracy=self._minimum_accuracy,
                    ),
                    code="EVALUATION_GATE_FAILED",
                )
            index_snapshot = self._catalog.get_index_snapshot(
                report.index_snapshot_id,
                project_id=validated.project_id,
            )
            if index_snapshot.release_spec_hash != validated.semantic_spec.spec_hash:
                raise SemanticValidationError(
                    "evaluated semantic index does not match this revision",
                    code="EVALUATION_GATE_FAILED",
                )
        else:
            index_snapshot = self._index_builder.build(validated.semantic_spec)
        return self._catalog.publish(revision=validated, index_snapshot=index_snapshot)

    def _require_quality_evidence(self, revision: ModelingRevision) -> None:
        """Fail closed when read-only quality evidence is missing or blocking.

        The profiler already detects duplicate/NULL primary identifiers,
        declared-vs-observed cardinality conflicts, failing metric samples and
        unreachable metric-dimension pairs. Those findings describe a model that
        answers questions incorrectly, so they must gate publication rather than
        remain an optional diagnostic. The report is bound to the exact semantic
        spec hash, so editing a revision after review invalidates the evidence.
        """

        if not self._require_quality_report:
            return
        # 按证据哈希取报告：改中文名不会作废需要真实扫库的证据，
        # 但任何影响关系、聚合或主题范围的改动仍会要求重跑。
        report = self._catalog.get_latest_modeling_quality_report(
            semantic_evidence_hash(revision.semantic_spec)
        )
        if report is None:
            raise SemanticValidationError(
                "modeling quality report is required before publication",
                code="QUALITY_GATE_FAILED",
            )
        if report.blocking_count:
            raise SemanticValidationError(
                f"数据质量报告有 {report.blocking_count} 个阻断问题,请先处理",
                code="QUALITY_GATE_FAILED",
            )
        if not report.ready:
            # ready = 无阻断 且 无待核对。此前两种情况合并报「blocking findings」,
            # 阻断数为 0 的用户看到这句话完全无从下手——实测就是指标样本待核对。
            pending = sum(
                1 for item in report.metric_previews if item.status is QualityStatus.PENDING_REVIEW
            )
            raise SemanticValidationError(
                f"数据质量报告还有 {pending} 项指标样本待人工确认",
                code="QUALITY_GATE_FAILED",
            )
