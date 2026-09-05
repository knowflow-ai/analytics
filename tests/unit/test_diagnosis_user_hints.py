from __future__ import annotations

import pytest

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.query.contracts import QueryStage
from knowflow_analytics.query.service import _error_diagnosis, _success_diagnosis

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
