"""问数与发布前试问默认不让模型思考。

现场（2026-09-17）：发布前试问每次 60 秒超时。端点本身不慢（80–120 tok/s），烧掉的是
思维链——qwen235b 是 Qwen3 推理模型，默认开思考。同一道 S2SQL 题实测：思考开 10.5 秒
（输出 810 token，其中 1451 字是思维链）、关掉 0.8 秒（29 token），13 倍。建模那档超时
180 秒扛得住，问数这档 60 秒扛不住，所以「建模是好的、问数不行」。

问数要的是一段受治理的 S2SQL，形态由符号表、路由和六道治理关确定性校验；模型把推理
过程写出来不改变答案，只改变延迟。所以这一档默认关思考，且由配置而非代码写死——
换模型、换端点的人得能自己调。

开关是请求的一部分：网关侧不猜模型支不支持，只如实表达「这次不需要思考」，
怎么落到厂商协议上由 RAGFlow 侧按端点决定。
"""

from __future__ import annotations

import json

import httpx

from knowflow_analytics.gateways.model import HttpModelGateway

_SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}


def _gateway(captured: list[dict], **kwargs) -> HttpModelGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"structured": {"sql": "SELECT 1"}}},
        )

    client = httpx.Client(
        base_url="http://ragflow.invalid",
        transport=httpx.MockTransport(handler),
    )
    return HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="token-token-token",
        llm_id="",
        client=client,
        **kwargs,
    )


def _ask(gateway: HttpModelGateway, purpose: str = "s2sql") -> None:
    gateway.generate_json(
        purpose=purpose,
        messages=[{"role": "user", "content": "账户余额大于 1000 有多少人"}],
        response_schema=_SCHEMA,
        trace={"tenant_id": "tenant-1"},
    )


def test_a_query_asks_the_model_not_to_think() -> None:
    """默认不思考：问数的答案由治理关校验，思维链只是延迟。"""

    captured: list[dict] = []
    _ask(_gateway(captured))

    assert captured[0]["model_params"]["enable_thinking"] is False


def test_thinking_can_be_turned_back_on() -> None:
    """换一个靠推理才写得对复杂 SQL 的模型时，运营要能把它打开。"""

    captured: list[dict] = []
    _ask(_gateway(captured, thinking_enabled=True))

    assert captured[0]["model_params"]["enable_thinking"] is True


def test_modeling_keeps_the_same_switch() -> None:
    """AI 建模也走同一个开关：一处配置，不是每个用途各有一套。"""

    captured: list[dict] = []
    _ask(_gateway(captured), purpose="modeling.naming")

    assert captured[0]["model_params"]["enable_thinking"] is False
