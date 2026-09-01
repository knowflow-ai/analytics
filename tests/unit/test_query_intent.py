from __future__ import annotations

import pytest

from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.intent import reject_unsupported_intent


@pytest.mark.parametrize(
    "question",
    [
        "销售额增长率是多少",
        "各月净金额的同比增长率",
        "各月净金额环比增幅",
        "净金额涨幅",
        "本季度净金额降幅",
        "各月净金额同比",
        "净金额同比增长率是多少",
    ],
)
def test_ratio_questions_reach_the_pipeline(question: str) -> None:
    """比率类问法必须放行——RATIO_OVER / RATIO_ROLL 就是干这个的。

    这道闸门来自首次开源提交，当时还没有比率函数；`RATIO_*` 落地后没人回头
    对齐，于是「同比」放行、「增长率」被拒，同一个能力两种待遇。
    2026-09-01 实机实验（rel_72ffa832）：「销售额增长率是多少」返回
    ['月份','增长率']，「各月净金额的同比增长率」返回 ['月','净金额同比增长率']，
    都是正确的比率结果，不是降级后的普通聚合。
    """

    reject_unsupported_intent(question)


@pytest.mark.parametrize(
    "question",
    [
        "净金额同比增长了多少",
        "净金额增长多少",
        "订单数增加了多少",
        "退款金额减少了多少",
        "客户数下降了多少",
    ],
)
def test_absolute_delta_is_refused_with_an_actionable_message(question: str) -> None:
    """绝对增减量没有受治理的表达方式，拒绝时要给出能照做的替代说法。

    受治理函数只有三个比率函数，没有期间差值函数。实机验证：放开后模型会写
    RATIO_OVER(SUM(...))，被预聚合护栏拦成 S2SQL_RATIO_METRIC_PRE_AGGREGATED
    ——不会给出错误数字，但用户只能看到一个技术错误码。入口拒绝更有用。
    """

    with pytest.raises(SemanticParsingError) as raised:
        reject_unsupported_intent(question)

    assert raised.value.code == "UNSUPPORTED_ANALYTIC_OPERATION"
    # 拒绝必须告诉用户改怎么问，否则就是死胡同。
    assert "增长率" in str(raised.value)


@pytest.mark.parametrize(
    "question",
    ["预计下季度净收入", "预测明年销售额", "forecast next quarter revenue"],
)
def test_forecasting_stays_unsupported(question: str) -> None:
    """预测能力确实不存在，继续拒绝，且绝不降级成普通聚合。"""

    with pytest.raises(SemanticParsingError) as raised:
        reject_unsupported_intent(question)

    assert raised.value.code == "UNSUPPORTED_ANALYTIC_OPERATION"
    assert "预测分析" in str(raised.value)


def test_ordinary_questions_are_untouched() -> None:
    reject_unsupported_intent("各地区的净金额")
    reject_unsupported_intent("华东有哪些客户类别")
