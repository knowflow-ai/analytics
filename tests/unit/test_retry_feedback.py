"""重试要把拒绝原因带给模型（2026-09-05 提速盘点第 4 条）。

实机「咖啡按月环比销售情况」：候选被门拒掉后重试三次、48 秒，模型每次写的都是同一个
形状——它只被升了温度，从来不知道错在哪。两层重试都要带上一次的错误：解析器内层的
校验失败，和编排器第一趟候选被翻译 / 校正拒掉之后的 ALL 那趟。
"""

from __future__ import annotations

from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import LlmS2SqlParser


class _Gateway:
    def __init__(self, sqls: list[str]) -> None:
        self.sqls = list(sqls)
        self.prompts: list[list[dict[str, str]]] = []

    def generate_json(self, **kwargs):
        self.prompts.append(kwargs["messages"])
        return {"thought": "t", "sql": self.sqls.pop(0)}


def _mapping(sales_index):
    return SemanticMapper().map(
        question="各区域净收入是多少",
        dataset_id="sales_dataset",
        index=sales_index,
        mode=MapMode.STRICT,
    )


def _parse(parser, sales_release, sales_index, **overrides):
    kwargs = dict(
        question="各区域净收入是多少",
        release=sales_release,
        mapping=_mapping(sales_index),
        query_id="q-1",
        tenant_id="t-1",
    )
    kwargs.update(overrides)
    return parser.parse(**kwargs)


def test_the_all_pass_prompt_carries_the_previous_rejection(sales_release, sales_index) -> None:
    gateway = _Gateway(['SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'])
    _parse(
        LlmS2SqlParser(gateway),
        sales_release,
        sales_index,
        rejection={
            "code": "S2SQL_RATIO_METRIC_PRE_AGGREGATED",
            "message": "ratio metric argument must be a governed metric",
            "s2sql": 'SELECT RATIO_ROLL(SUM("净收入")) FROM "销售经营"',
        },
    )

    tail = gateway.prompts[0][-1]
    assert tail["role"] == "user"
    assert "S2SQL_RATIO_METRIC_PRE_AGGREGATED" in tail["content"]
    assert 'RATIO_ROLL(SUM("净收入"))' in tail["content"]
    assert "Do not repeat" in tail["content"]


def test_the_inner_retry_tells_the_model_why_the_last_attempt_failed(
    sales_release, sales_index
) -> None:
    # 第一次不是合法 SELECT（校验拒），第二次才对。
    gateway = _Gateway(
        ["DELETE FROM x", 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"']
    )
    candidate = _parse(LlmS2SqlParser(gateway), sales_release, sales_index)

    assert candidate is not None
    assert len(gateway.prompts) == 2
    # 第一次没有反馈段，第二次带着上一次的错误。
    assert "Previous attempt was rejected" not in gateway.prompts[0][-1]["content"]
    assert "Previous attempt was rejected" in gateway.prompts[1][-1]["content"]


def test_no_rejection_means_no_extra_message(sales_release, sales_index) -> None:
    gateway = _Gateway(['SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'])
    _parse(LlmS2SqlParser(gateway), sales_release, sales_index)
    assert "Previous attempt" not in gateway.prompts[0][-1]["content"]
