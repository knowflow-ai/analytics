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
