"""Rule 选不出时间轴时，产出候选而不是拒绝产出。

实机「检查2024年1月所有凭证是否借贷平衡」：会计目录有 5 个时间维、一个
``partition_time`` 都没声明，于是 ``_apply_time_filters`` 在 STRICT/MODERATE/LOOSE
三种模式下全部抛 ``AMBIGUOUS_TIME_DIMENSION``。Rule 发现阶段因此一个候选都没有，
而最终 LLM 需要一个 Rule 候选来固定数据集与 ElementMatches——**发现阶段全灭，
LLM 阶段就没有入口**，一个模型完全答得对的问题变成了一张让用户选日期字段的卡。

Rule 候选在发现阶段的职责是固定「哪个数据集、命中了哪些成员」，时间过滤是附带的。
为一个附带项放弃整个候选，代价远大于收益。

安全边界没有放松：候选带上 ``time_axis_unresolved`` 标记，只要它真的成为被执行的
那一条（LLM 没产出候选、走 Rule 兜底），诊断就必须说清「问题里的时间没有落进查询」。
LLM 赢下这一轮时它自己的 ``applied_defaults`` 里没有这个标记，不会误报。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.contracts import (
    DimensionSpec,
    FieldKind,
    FieldSpec,
    SemanticRelease,
)
from knowflow_analytics.query.contracts import QueryDiagnosticCategory
from knowflow_analytics.query.errors import ClarificationSignal
from knowflow_analytics.query.parser import (
    TIME_AXIS_UNRESOLVED,
    _apply_time_filters,
)
from knowflow_analytics.query.service import _success_diagnosis

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _two_axes_no_default(release: SemanticRelease) -> SemanticRelease:
    """两个时间轴、都没被声明为默认——实机会计目录的形状（那里是 5 个）。"""

    paid_field = FieldSpec(
        id="orders.paid_at",
        model_id="orders",
        name="支付时间",
        column="paid_at",
        data_type="timestamp",
        kind=FieldKind.DIMENSION,
        dimension_type="time",
    )
    paid_dimension = DimensionSpec(
        id="paid_at",
        name="支付时间",
        model_id="orders",
        field_id=paid_field.id,
        semantic_type="time",
    )
    dataset = release.datasets[0]
    return release.model_copy(
        update={
            "fields": (*release.fields, paid_field),
            "dimensions": (*release.dimensions, paid_dimension),
            "datasets": (
                dataset.model_copy(
                    update={
                        "dimension_ids": (*dataset.dimension_ids, paid_dimension.id),
                        "default_time_dimension_id": None,
                    }
                ),
                *release.datasets[1:],
            ),
        }
    )


def _run(release: SemanticRelease, **overrides):
    kwargs = {
        "question": "2024年1月的情况",
        "release": release,
        "dataset": release.datasets[0],
        "mapped_dimension_ids": [],
        "selected_metric_ids": [],
        "existing_filters": [],
        "now": NOW,
    }
    kwargs.update(overrides)
    return _apply_time_filters(**kwargs)


class TestAnUnresolvableTimeAxis:
    def test_it_no_longer_refuses_to_produce_a_candidate(self, sales_release) -> None:
        filters, defaults = _run(_two_axes_no_default(sales_release), defer_axis_ambiguity=True)

        assert filters == []
        assert TIME_AXIS_UNRESOLVED in defaults

    def test_without_an_llm_it_still_asks(self, sales_release) -> None:
        """没有 LLM 时 Rule 就是答案，丢掉用户明说的时间是静默错答。"""

        with pytest.raises(ClarificationSignal) as raised:
            _run(_two_axes_no_default(sales_release))

        assert raised.value.code == "AMBIGUOUS_TIME_DIMENSION"

    def test_the_marker_is_inert_for_the_answer_card(self, sales_release) -> None:
        """它不是一个时间窗标记，回答卡的「默认只看…」chip 不该出现。"""

        from knowflow_analytics.query.default_time_window import parse_time_window_marker

        assert parse_time_window_marker(TIME_AXIS_UNRESOLVED) is None

    def test_a_single_axis_is_still_used(self, sales_release) -> None:
        filters, defaults = _run(sales_release, defer_axis_ambiguity=True)

        assert len(filters) >= 1
        assert TIME_AXIS_UNRESOLVED not in defaults

    def test_an_explicitly_confirmed_axis_is_still_used(self, sales_release) -> None:
        release = _two_axes_no_default(sales_release)
        order_date = next(
            item
            for item in release.dimensions
            if item.id != "paid_at" and item.semantic_type == "time"
        )

        filters, defaults = _run(
            release, selected_time_dimension_id=order_date.id, defer_axis_ambiguity=True
        )

        assert {item.dimension_id for item in filters} == {order_date.id}
        assert TIME_AXIS_UNRESOLVED not in defaults

    def test_conflicting_declared_axes_still_ask(self, sales_release) -> None:
        """指标各自声明了不同的时间轴是另一回事：那是建模者说了话且互相矛盾。"""

        release = _two_axes_no_default(sales_release)
        order_date = next(
            item
            for item in release.dimensions
            if item.id != "paid_at" and item.semantic_type == "time"
        )
        metric_ids = [item.id for item in release.metrics][:2]
        release = release.model_copy(
            update={
                "metrics": tuple(
                    item.model_copy(update={"agg_time_dimension_id": axis})
                    if item.id == metric_id
                    else item
                    for item in release.metrics
                    for metric_id, axis in ((metric_ids[0], order_date.id),)
                )
            }
        )
        release = release.model_copy(
            update={
                "metrics": tuple(
                    item.model_copy(update={"agg_time_dimension_id": "paid_at"})
                    if item.id == metric_ids[1]
                    else item
                    for item in release.metrics
                )
            }
        )

        with pytest.raises(ClarificationSignal) as raised:
            _run(release, selected_metric_ids=metric_ids, defer_axis_ambiguity=True)

        assert raised.value.code == "AMBIGUOUS_TIME_DIMENSION"


class TestTheDroppedTimeIsReportedWhenRuleAnswers:
    def test_rule_fallback_says_the_time_was_dropped(self) -> None:
        diagnosis = _success_diagnosis(
            parser="rule",
            llm_enabled=True,
            audit_complete=True,
            time_axis_unresolved=True,
        )

        assert diagnosis.severity == "warning"
        assert diagnosis.category is QueryDiagnosticCategory.RULE_FALLBACK
        assert "时间" in diagnosis.user_hint

    def test_the_llm_answer_is_not_accused_of_dropping_time(self) -> None:
        """标记只挂在 Rule 候选上；LLM 赢下这一轮时不该出现这句话。"""

        diagnosis = _success_diagnosis(
            parser="llm", llm_enabled=True, audit_complete=True, time_axis_unresolved=False
        )

        assert diagnosis.category is QueryDiagnosticCategory.SUCCESS
