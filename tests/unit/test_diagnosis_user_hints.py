from __future__ import annotations

import pytest

from knowflow_analytics.contracts import QueryResult
from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.query.contracts import QueryDiagnosticCategory, QueryStage
from knowflow_analytics.query.service import (
    _aggregate_matched_no_rows,
    _error_diagnosis,
    _success_diagnosis,
)

# 这些词只对建模者有意义；出现在给提问者看的提示里，就说明两类文案又混在一起了。
_MODELER_JARGON = ("SQL", "Embedding", "Revision", "Release", "SQLSTATE", "Corrector", "fallback")

_ERROR_CASES = [
    (QueryStage.EXECUTING, "EXEC"),
    (QueryStage.PHYSICAL_SQL_VALIDATING, "GUARD"),
    (QueryStage.TRANSLATING, "MISSING_JOIN_PATH"),  # routing 分支
    (QueryStage.TRANSLATING, "UNSUPPORTED_EXPR"),  # translation 分支
    (QueryStage.S2SQL_CORRECTING, "CORR"),
    (QueryStage.FINAL_PARSING, "PARSE"),
    (QueryStage.CANDIDATE_DISCOVERY, "NO_SEMANTIC_MAPPING"),
    (QueryStage.PRECHECK, "STALE"),
    (QueryStage.POST_PROCESSING, "WHATEVER"),  # 落到 internal
]


@pytest.mark.parametrize(("stage", "code"), _ERROR_CASES)
def test_every_error_diagnosis_tells_the_asker_what_to_do(stage, code):
    """8 条 recommendation 全是让人看物理 SQL / SQLSTATE / Embedding 候选 / Revision 版本
    的 —— 没有一条告诉提问的业务用户"换个问法"。user_hint 是给他的那条，必须存在，
    而且不能再把建模者的术语带进去。
    """

    diagnosis = _error_diagnosis(AnalyticsError("boom", code=code, stage=stage.value))

    assert diagnosis.user_hint.strip(), f"{stage.value} 没有 user_hint"
    for word in _MODELER_JARGON:
        assert word not in diagnosis.user_hint, f"{stage.value} 的 user_hint 含 {word}"
    # 建模者那条保持原样，不能为了加 user_hint 把它弄丢。
    assert diagnosis.recommendation.strip()


def test_routing_and_translation_failures_get_different_hints():
    routing = _error_diagnosis(
        AnalyticsError("x", code="MISSING_JOIN_PATH", stage=QueryStage.TRANSLATING.value)
    )
    translation = _error_diagnosis(
        AnalyticsError("x", code="UNSUPPORTED_EXPR", stage=QueryStage.TRANSLATING.value)
    )
    assert routing.user_hint != translation.user_hint


def test_degraded_success_warnings_also_carry_a_user_hint():
    fallback = _success_diagnosis(parser="rule", llm_enabled=True, audit_complete=True)
    lossy = _success_diagnosis(parser="llm", llm_enabled=True, audit_complete=False)

    assert fallback.user_hint.strip()
    assert lossy.user_hint.strip()
    for word in _MODELER_JARGON:
        assert word not in fallback.user_hint


class TestEmptyResultCausedByAnUnpublishedFilterValue:
    """空结果要能分清"数据里确实没有"和"这个说法系统不认识"。

    实机（2026-09-03，demo_cafe）：问「哪些门店售卖卡布奇洛」——商品叫「卡布奇诺」，
    用户打错一个字。系统照样翻成 `商品名称 = '卡布奇洛'` 执行成功、0 行，界面只说
    "查询成功，但没有返回数据"。用户读到的是"没有门店卖这个"，而真相是这个词根本
    不在已发布取值里。

    已发布取值对高基数维度可能只是抽样，所以措辞是"不在已发布取值里"而不是"不存在"，
    并且只在 0 行时提示——有结果就说明过滤生效了，不需要解释。
    """

    def test_unknown_value_is_named_with_a_near_miss_suggestion(self, sales_release) -> None:
        from knowflow_analytics.query.service import _unpublished_filter_values

        found = _unpublished_filter_values(
            sales_release,
            filters=(("region", "华东省"),),
        )

        assert found == (("区域", "华东省", "华东"),)

    def test_a_value_that_really_is_not_there_gets_no_invented_suggestion(
        self, sales_release
    ) -> None:
        from knowflow_analytics.query.service import _unpublished_filter_values

        found = _unpublished_filter_values(sales_release, filters=(("region", "南极洲"),))

        assert found == (("区域", "南极洲", None),)

    def test_published_values_are_not_flagged(self, sales_release) -> None:
        from knowflow_analytics.query.service import _unpublished_filter_values

        assert _unpublished_filter_values(sales_release, filters=(("region", "华东"),)) == ()

    def test_dimensions_without_published_values_are_left_alone(self, sales_release) -> None:
        """没发布取值的维度无从判断，不能因为"没查到"就说人家说法不对。"""
        from knowflow_analytics.query.service import _unpublished_filter_values

        assert _unpublished_filter_values(sales_release, filters=(("unknown_dim", "任意"),)) == ()

    def test_hint_names_the_value_and_stays_out_of_modeler_jargon(self) -> None:
        diagnosis = _success_diagnosis(
            parser="llm",
            llm_enabled=True,
            audit_complete=True,
            unpublished_values=(("商品名称", "卡布奇洛", "卡布奇诺"),),
        )

        assert diagnosis.severity == "warning"
        assert "卡布奇洛" in diagnosis.user_hint
        assert "卡布奇诺" in diagnosis.user_hint
        for word in _MODELER_JARGON:
            assert word not in diagnosis.user_hint

    def test_without_unpublished_values_the_success_diagnosis_is_unchanged(self) -> None:
        plain = _success_diagnosis(parser="llm", llm_enabled=True, audit_complete=True)
        same = _success_diagnosis(
            parser="llm", llm_enabled=True, audit_complete=True, unpublished_values=()
        )

        assert plain == same


class TestRowLimitExceeded:
    def test_the_translators_sentence_survives_instead_of_the_generic_translation_hint(
        self,
    ) -> None:
        # 用户点名要 3000 行、上限 2000：套翻译阶段通用文案会说「计算方式不支持」。
        exc = AnalyticsError(
            "这个问题要返回 3000 行，超过了一次最多返回的 2000 行。"
            "请加条件缩小范围，或调高「最多返回行数」的设置。",
            code="QUERY_LIMIT_EXCEEDED",
            stage=QueryStage.TRANSLATING.value,
        )

        diagnosis = _error_diagnosis(exc)

        assert diagnosis.user_hint == str(exc)
        assert "计算方式" not in diagnosis.user_hint
        assert diagnosis.stage == QueryStage.TRANSLATING.value


class TestAnAggregateThatMatchedNoRows:
    """没有分组的聚合在零行上返回的是一行 NULL，不是零行。

    界面只在 ``rows.length === 0`` 时说「查询成功，但没有返回数据」
    （``query-answer.tsx``）；一行 NULL 会被渲染成一个**看起来像答案的空格**，
    而诊断照旧报 success、零提示。实机「2024 年应交增值税是多少」正是这样：
    模型落到科目余额分析，那张表没有这个科目的行，于是 ``SUM`` 返回 NULL，
    用户读到的是「这一年没有应交增值税」——真值是进项 13000 / 销项 26000。

    判据确定性、不读问句：**有指标列、没有分组、结果不超过一行、且全部指标列
    为 NULL**。SQL 语义上 ``SUM`` 只在没有任何行参与时才返回 NULL，所以这等价于
    「这个条件下一条记录都没匹配上」。

    措辞沿用 2026-09-03 的口径：说「没有查到数据」，不说「不存在」——高基数维度
    的取值可能只是抽样发布，我们并不知道数据里真的没有。
    """

    def test_all_null_metric_columns_stop_being_a_success(self) -> None:
        diagnosis = _success_diagnosis(
            parser="llm",
            llm_enabled=True,
            audit_complete=True,
            empty_aggregate=True,
        )

        assert diagnosis.severity == "warning"
        assert diagnosis.category is not QueryDiagnosticCategory.SUCCESS
        assert "没有查到数据" in diagnosis.user_hint
        assert "不存在" not in diagnosis.user_hint
        for word in _MODELER_JARGON:
            assert word not in diagnosis.user_hint

    def test_a_named_unpublished_value_still_wins_because_it_says_more(self) -> None:
        """两条都成立时先说能指名道姓的那条：它直接给出了可操作的原因。"""

        diagnosis = _success_diagnosis(
            parser="llm",
            llm_enabled=True,
            audit_complete=True,
            unpublished_values=(("商品名称", "卡布奇洛", "卡布奇诺"),),
            empty_aggregate=True,
        )

        assert "卡布奇洛" in diagnosis.user_hint

    def test_an_ordinary_success_is_unchanged(self) -> None:
        plain = _success_diagnosis(parser="llm", llm_enabled=True, audit_complete=True)
        same = _success_diagnosis(
            parser="llm", llm_enabled=True, audit_complete=True, empty_aggregate=False
        )

        assert plain == same
        assert plain.category is QueryDiagnosticCategory.SUCCESS


class TestWhatCountsAsAnAggregateThatMatchedNoRows:
    """判据的边界，逐条实测过（真实目录 9/9）。"""

    def _result(self, columns, rows):
        return QueryResult(columns=tuple(columns), rows=tuple(rows), row_count=len(rows))

    def test_a_single_all_null_metric_row_counts(self) -> None:
        assert _aggregate_matched_no_rows(
            self._result(["net_revenue"], [(None,)]),
            metric_ids=("net_revenue",),
            dimension_ids=(),
        )

    def test_a_dimension_only_query_never_counts(self) -> None:
        """纯维度查询没有指标列，「全部指标列为 NULL」在空集上恒真——必须先要求有指标列。

        实机「有哪些部门」「往来单位有哪些」就是这种形状。
        """

        assert not _aggregate_matched_no_rows(
            self._result(["region"], [("销售部",)]),
            metric_ids=(),
            dimension_ids=("region",),
        )

    def test_a_grouped_query_never_counts(self) -> None:
        """带分组时零行走既有的 ``row_count == 0`` 分支，多行更不适用。"""

        assert not _aggregate_matched_no_rows(
            self._result(["region", "net_revenue"], [("华南", None), ("华北", 100)]),
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
        )

    def test_one_metric_with_a_value_is_enough_to_disqualify(self) -> None:
        assert not _aggregate_matched_no_rows(
            self._result(["net_revenue", "order_count"], [(None, 3)]),
            metric_ids=("net_revenue", "order_count"),
            dimension_ids=(),
        )

    def test_zero_rows_is_left_to_the_existing_branch(self) -> None:
        assert not _aggregate_matched_no_rows(
            self._result(["net_revenue"], []),
            metric_ids=("net_revenue",),
            dimension_ids=(),
        )
