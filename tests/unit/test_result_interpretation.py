"""结果解读合同（移植上游 DataInterpretProcessor，2026-09-05）。

关键边界：解读**只是一段话**，不参与任何判定；它在结果之后生成，失败、超时、
读不出东西都不影响已经返回的数字。喂给模型的必须是投影后的业务名结果。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import QueryOptions
from knowflow_analytics.query.interpret import (
    MAX_INTERPRETED_ROWS,
    ResultInterpreter,
    format_context,
    format_result_data,
)


class _Gateway:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {"interpretation": "8 月共 5 家门店。"}
        self.error = error
        self.calls: list[dict] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.payload


def _interpret(interpreter: ResultInterpreter, **overrides):
    kwargs = dict(
        question="各门店的销售金额",
        columns=["门店名称", "销售金额"],
        rows=[["南京西路店", 3546], ["西湖店", 2470]],
        tenant_id="user-1",
    )
    kwargs.update(overrides)
    return interpreter.interpret(**kwargs)


class TestPrompt:
    def test_the_model_only_sees_projected_business_names(self) -> None:
        gateway = _Gateway()
        _interpret(ResultInterpreter(gateway, enabled=True))

        prompt = gateway.calls[0]["messages"][-1]["content"]
        assert "门店名称" in prompt and "3546" in prompt
        # 指令必须在 system：宿主把 JSON Schema 指令拼在 system 之后，正文以
        # "#Answer:" 收尾会让模型直接写散文，网关就以「不是合法 JSON」丢掉整次解读。
        assert gateway.calls[0]["messages"][0]["role"] == "system"
        assert not prompt.rstrip().endswith("#Answer:")
        # 语义 ID / 物理列绝不进解读 Prompt。
        assert "dimension:" not in prompt and "__kf_field" not in prompt
        assert gateway.calls[0]["purpose"] == "analytics.result_interpretation"

    def test_units_are_given_to_the_model_instead_of_being_guessed(self) -> None:
        # 实机：不给单位时模型有时写「3138元」有时写「3138」——带单位那次是它猜的。
        gateway = _Gateway()
        _interpret(ResultInterpreter(gateway, enabled=True), units={"销售金额": "元"})

        prompt = gateway.calls[0]["messages"][-1]["content"]
        assert "销售金额（元）" in prompt
        # 没有单位的列不加括号。
        assert "门店名称（" not in prompt

    def test_the_applied_filters_are_given_so_the_scope_is_not_guessed(self) -> None:
        # 不给过滤条件时，「上海的门店销售额」会被读成全部门店。
        gateway = _Gateway()
        _interpret(
            ResultInterpreter(gateway, enabled=True),
            filters=["所在城市 = 上海"],
            default_time_window={
                "dimension": "销售日期",
                "start": "2026-08-29",
                "end": "2026-09-05",
                "label": "最近 7 天",
            },
        )

        prompt = gateway.calls[0]["messages"][-1]["content"]
        assert "本次查询的过滤条件: 所在城市 = 上海" in prompt
        # 系统补的窗要标明是默认的：用户没这么要求，那句话不能说得像他要求过。
        assert "系统补充的默认时间范围（用户没有指定）: 最近 7 天" in prompt

    def test_the_model_may_not_attribute_a_filter_to_the_system(self) -> None:
        """实测：把用户自己说的「上个月」列进上下文，模型转述成「系统自动添加的
        过滤条件」——给用户扣了一顶他没戴过的帽子。只有标了默认的那一行才是系统补的。"""

        gateway = _Gateway()
        _interpret(ResultInterpreter(gateway, enabled=True), filters=["销售日期 ≥ 2026-08-01"])

        instruction = gateway.calls[0]["messages"][0]["content"]
        assert "Do NOT say who added a filter" in instruction
        prompt = gateway.calls[0]["messages"][-1]["content"]
        assert "系统" not in prompt.split("#Data")[0]

    def test_no_context_section_when_nothing_was_applied(self) -> None:
        assert format_context([], None) == ""
        gateway = _Gateway()
        _interpret(ResultInterpreter(gateway, enabled=True))
        assert "#Context" not in gateway.calls[0]["messages"][-1]["content"]

    def test_the_prompt_forbids_numbers_that_are_not_in_the_data(self) -> None:
        # 解读和数字一样有权威感；模型顺手算一个没给过的同比就是新的静默错答通道。
        gateway = _Gateway()
        _interpret(ResultInterpreter(gateway, enabled=True))

        instruction = gateway.calls[0]["messages"][0]["content"]
        assert "ONLY state numbers that literally appear in `#Data`" in instruction
        assert "NEVER compute or guess" in instruction

    def test_long_results_are_truncated_with_the_row_count_kept(self) -> None:
        rows = [[f"店{i}", i] for i in range(MAX_INTERPRETED_ROWS + 20)]
        text = format_result_data(["门店名称", "销售金额"], rows)

        assert text.count("\n") <= MAX_INTERPRETED_ROWS + 2
        assert f"共 {len(rows)} 行" in text

    def test_a_very_wide_cell_cannot_blow_up_the_prompt(self) -> None:
        text = format_result_data(["备注"], [["x" * 50_000]])
        assert len(text) < 5_000
        assert "已截断" in text


class TestNeverBreaksTheAnswer:
    def test_a_model_failure_yields_no_summary_instead_of_an_error(self) -> None:
        interpreter = ResultInterpreter(_Gateway(error=RuntimeError("gateway down")), enabled=True)
        assert _interpret(interpreter) is None

    def test_a_malformed_model_response_yields_no_summary(self) -> None:
        interpreter = ResultInterpreter(_Gateway(payload={"nope": 1}), enabled=True)
        assert _interpret(interpreter) is None

    @pytest.mark.parametrize("rows", ([], None))
    def test_no_rows_means_no_model_call_at_all(self, rows) -> None:
        gateway = _Gateway()
        interpreter = ResultInterpreter(gateway, enabled=True)
        assert _interpret(interpreter, rows=rows or []) is None
        assert gateway.calls == []


class TestAssistantSwitch:
    def test_the_option_defaults_to_following_the_deployment(self) -> None:
        assert QueryOptions().result_interpretation_enabled is None
        # 部署默认关闭（与上游 enable(false) 一致），助手可以显式打开。
        assert QueryOptions().merged("result_interpretation_enabled", False) is False
        assert (
            QueryOptions(result_interpretation_enabled=True).merged(
                "result_interpretation_enabled", False
            )
            is True
        )
