from __future__ import annotations

import re

from knowflow_analytics.query.errors import SemanticParsingError

# 真正没有实现的能力：拒绝，且绝不降级成普通聚合（那会变成静默错答）。
_UNSUPPORTED_OPERATIONS = ((re.compile(r"预测|预估|预计|forecast", re.IGNORECASE), "预测分析"),)

# 绝对增减量（「增长了多少」问的是差值本身，不是比率）。受治理函数只有
# RATIO_OVER / RATIO_ROLL / RATIO_TO_TOTAL 三个比率函数，没有期间差值函数。
#
# 2026-09-01 实机实验（rel_72ffa832，放开闸门后逐条实跑）：
#   「净金额同比增长了多少」→ 模型写出 RATIO_OVER(SUM(...))，被 8-30 加的
#   预聚合护栏拦下，FAILED / S2SQL_RATIO_METRIC_PRE_AGGREGATED。
# 也就是说放开它不会给出错误数字（护栏在），但用户拿到的是一个技术错误码。
# 与其如此，不如在入口就告诉他换个说法——可操作的拒绝优于难懂的失败。
# 「了」可省（增长多少 / 增长了多少）。注意它不会误伤「增长率是多少」——
# 那里「增长」后面跟的是「率」，不是「多少」。
_ABSOLUTE_DELTA = re.compile(r"(?:增长|增加|减少|下降)了?多少")


def reject_unsupported_intent(question: str) -> None:
    """把确实做不到的问法挡在链路之外。

    只挡两类：没有实现的分析能力（预测），以及没有受治理表达方式的问法
    （绝对增减量）。**比率类问法不在此列**——同比/环比/增长率/增幅由
    RATIO_OVER / RATIO_ROLL 正常回答，实测见 `_ABSOLUTE_DELTA` 上方注释与
    `tests/unit/test_query_intent.py`。
    """

    requested = [label for pattern, label in _UNSUPPORTED_OPERATIONS if pattern.search(question)]
    if requested:
        raise SemanticParsingError(
            f"当前版本尚未开放{'、'.join(dict.fromkeys(requested))}，不会降级成普通聚合查询。",
            code="UNSUPPORTED_ANALYTIC_OPERATION",
        )
    if _ABSOLUTE_DELTA.search(question):
        raise SemanticParsingError(
            "当前版本只能给出增长率，给不出绝对增减量。"
            "请改问增长率，例如「各月净金额的同比增长率」。",
            code="UNSUPPORTED_ANALYTIC_OPERATION",
        )
