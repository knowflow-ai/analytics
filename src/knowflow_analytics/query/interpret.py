"""结果解读：把返回的行变成一句业务人话。

移植自上游 ``DataInterpretProcessor``（"结果数据解读"，`enable(false)`），保留它的
角色设定、"用问题的语言回答"和"必须引用关键数据"两条规则。三处按本仓库的底线加严：

- **只能引用 ``#Data`` 里出现过的数字。** 解读是一段和数字一样有权威感的话，模型
  顺手算一个没给过的同比或合计，就是一条新的静默错答通道，而且没有任何治理关能
  拦住它——这正是上游默认关闭这个功能的原因。
- **喂给模型的是投影后的业务名结果**（和用户看到的表一字不差），不是语义 ID 或 SQL。
- **有上限**：行数和字符数都截断，长表只解读前几行并在提示里说明，避免把一次问数的
  时延和 token 成本拖到不可预期。

解读**不参与**任何判定：它读不出东西、失败、超时，问数结果都照常返回。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from knowflow_analytics.gateways.model import StructuredModelGateway

LOGGER = logging.getLogger(__name__)

# 只解读前这么多行：更多行对一句话的结论没有帮助，却线性推高时延和 token。
MAX_INTERPRETED_ROWS = 30
# 整个 #Data 的字符上限，防单元格超长（长文本维度）把 Prompt 撑爆。
MAX_DATA_CHARS = 4_000

# 指令进 system、问题与数据进 user：宿主把 JSON Schema 指令拼在 system 之后
# （`analytics_inference_service.generate_json`），而正文若以 "#Answer:" 收尾会强烈
# 暗示模型直接写散文——实测就是这样丢的：网关报 "model output is not valid JSON"，
# 解读静默为空。与 multi_turn 的消息形状保持一致。
_INTERPRET_INSTRUCTION = (
    "#Role: You are a data expert who communicates with business users everyday."
    "\n#Task: Your will be provided with a question asked by a user and the relevant "
    "result data queried from the databases, please interpret the data and organize a brief answer."
    "\n#Rules: "
    "\n1.ALWAYS respond in the same language as the `#Question`."
    "\n2.ALWAYS reference some key data in the answer."
    "\n3.ONLY state numbers that literally appear in `#Data`. NEVER compute or guess totals, "
    "shares, growth rates or comparisons that are not given, and never describe a trend the "
    "data does not show."
    "\n4.Keep the answer within 3 short sentences. Do not restate the question."
    # 第 5 条只约束「不许说是谁加的」，不要求它必须说出默认窗——实测两次它都只
    # 陈述了日期范围。「补了必须显示且可撤」这条底线由回答卡上的紫色 chip 保证，
    # 那是确定性的；把一条保证寄托在提示词上，等于没有保证。
    "\n5.`#Context` lists the filters actually applied to `#Data`. Describe that scope factually "
    "and never invent one. Do NOT say who added a filter: only a line explicitly marked as a "
    "system default may be described as a default range."
)
_INTERPRET_USER = "#Question:{question}\n{context}#Data:\n{data}"


class _InterpretOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: str = Field(min_length=1, max_length=2_000)


def format_result_data(
    columns: list[str], rows: list[list[Any]], units: dict[str, str] | None = None
) -> str:
    """把投影后的结果拼成给模型看的表格文本；超出上限就截断并写明。

    表头带上受治理的展示单位（`销售金额（元）`）。不带的话模型只看得到裸数字，
    写出来的那句话有时带「元」有时不带——而带的那次是它猜的。单位是发布配置里的
    事实，直接给它，不让它猜。
    """

    header = " | ".join(
        f"{item}（{units[item]}）" if units and units.get(item) else str(item) for item in columns
    )
    lines = [header]
    for row in rows[:MAX_INTERPRETED_ROWS]:
        lines.append(" | ".join("" if value is None else str(value) for value in row))
    if len(rows) > MAX_INTERPRETED_ROWS:
        lines.append(f"(共 {len(rows)} 行，以上只列出前 {MAX_INTERPRETED_ROWS} 行)")
    text = "\n".join(lines)
    if len(text) > MAX_DATA_CHARS:
        text = text[:MAX_DATA_CHARS] + "\n(数据过长已截断)"
    return text


def format_context(
    filters: list[str] | None = None,
    default_time_window: dict[str, Any] | None = None,
) -> str:
    """本次真正生效的口径。

    界面上有、而模型看不到的东西，它一律会猜——单位是这样，过滤条件同理：不给的话
    「上海的门店」会被读成全部门店。这里给的都是回答卡上已经显示的 chip，不引入
    任何新信息。

    **归属也是它会猜的东西。** 实测把用户自己说的「上个月」列成「已生效的过滤条件」，
    模型转述成「根据系统自动添加的过滤条件」——凭空给用户扣了一顶他没戴过的帽子。
    所以措辞是中性的「本次查询的过滤条件」，只有系统补的默认时间窗单独标注，
    也只有那一行允许被说成「系统补的」（见指令第 5 条）。
    """

    lines: list[str] = []
    if filters:
        lines.append("本次查询的过滤条件: " + "; ".join(filters))
    if default_time_window:
        label = default_time_window.get("label") or ""
        start = default_time_window.get("start") or ""
        end = default_time_window.get("end") or ""
        lines.append(f"系统补充的默认时间范围（用户没有指定）: {label}（{start} 起，{end} 前）")
    if not lines:
        return ""
    return "#Context:\n" + "\n".join(lines) + "\n"


class ResultInterpreter:
    """一次模型调用，把结果读成一段话。默认关闭，与上游一致。"""

    def __init__(self, gateway: StructuredModelGateway, *, enabled: bool = False) -> None:
        self._gateway = gateway
        self.enabled = enabled

    def interpret(
        self,
        *,
        question: str,
        columns: list[str],
        rows: list[list[Any]],
        tenant_id: str,
        units: dict[str, str] | None = None,
        filters: list[str] | None = None,
        default_time_window: dict[str, Any] | None = None,
        llm_id: str | None = None,
    ) -> str | None:
        """返回解读文本；不适用或失败时返回 None——解读永远不该让问数失败。"""

        if not columns or not rows:
            return None
        try:
            payload = self._gateway.generate_json(
                purpose="analytics.result_interpretation",
                messages=[
                    {"role": "system", "content": _INTERPRET_INSTRUCTION},
                    {
                        "role": "user",
                        "content": _INTERPRET_USER.format(
                            question=question,
                            context=format_context(filters, default_time_window),
                            data=format_result_data(columns, rows, units),
                        ),
                    },
                ],
                response_schema=_InterpretOutput.model_json_schema(),
                trace={
                    "tenant_id": tenant_id,
                    "contract_version": "knowflow-interpret-v1",
                    **({"llm_id": llm_id} if llm_id else {}),
                },
            )
            return _InterpretOutput.model_validate(payload).interpretation.strip() or None
        except Exception:  # noqa: BLE001 - 解读失败不影响问数结果
            LOGGER.warning("result interpretation failed", exc_info=True)
            return None
