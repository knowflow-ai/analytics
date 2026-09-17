"""助手选的模型和思考开关，要真的作用到这次问数的每一发模型调用上。

现场（2026-09-17）：助手设置里有「模型」下拉，但 `options.llm_id` 在核心侧只有一处
消费——`application.py` 传给**结果解读**，而结果解读默认是关的。S2SQL 生成那条路
一直用装配期固定的 `self._llm_id`：网关从 trace 里只取 `tenant_id` 和
`max_tokens_hint`，`llm_id` 一次都没被 pop 过。也就是助手选的模型对问答不起作用。

根因是传法：逐请求的配置靠「每个建 trace 的调用点记得带上」，于是只有一处记得了。
现在有六七处建 trace，将来还会更多，忘记的成本比隐式传递高。改走 ContextVar——
问数入口设一次，网关自己读，调用点一个都不用改（整问预算走的就是这条路）。

线程那条一起修：自洽投票用 ThreadPoolExecutor，而工作线程**不继承 ContextVar**。
记录调用漏几条只是诊断不全，模型和思考开关漏掉是那几票用了错的模型。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from knowflow_analytics.gateways.calls import ModelOverrides, model_overrides
from knowflow_analytics.gateways.model import HttpModelGateway

_SCHEMA = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}


def _gateway(captured: list[dict], **kwargs) -> HttpModelGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {"structured": {"sql": "SELECT 1"}}})

    return HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="token-token-token",
        llm_id="deployment-model",
        client=httpx.Client(
            base_url="http://ragflow.invalid", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def _ask(gateway: HttpModelGateway) -> None:
    gateway.generate_json(
        purpose="s2sql",
        messages=[{"role": "user", "content": "账户余额大于 1000 有多少人"}],
        response_schema=_SCHEMA,
        trace={"tenant_id": "tenant-1"},
    )


def test_the_assistants_model_reaches_the_generation_call() -> None:
    """助手选了模型，这次问数的每一发都该打到它身上。"""

    captured: list[dict] = []
    with model_overrides(ModelOverrides(llm_id="assistant-model")):
        _ask(_gateway(captured))

    assert captured[0]["model"]["llm_id"] == "assistant-model"


def test_the_assistants_thinking_choice_reaches_the_generation_call() -> None:
    captured: list[dict] = []
    with model_overrides(ModelOverrides(thinking_enabled=True)):
        _ask(_gateway(captured))

    assert captured[0]["model_params"]["enable_thinking"] is True


def test_an_assistant_that_chose_nothing_follows_the_deployment() -> None:
    """空 = 跟随部署配置，与 QueryOptions 其它各项同一个约定。"""

    captured: list[dict] = []
    with model_overrides(ModelOverrides()):
        _ask(_gateway(captured, thinking_enabled=False))

    assert captured[0]["model"]["llm_id"] == "deployment-model"
    assert captured[0]["model_params"]["enable_thinking"] is False


def test_outside_a_request_nothing_changes() -> None:
    """建模等不经问数入口的调用照旧走部署配置。"""

    captured: list[dict] = []
    _ask(_gateway(captured))

    assert captured[0]["model"]["llm_id"] == "deployment-model"


def test_ballots_running_in_worker_threads_all_use_the_same_model() -> None:
    """自洽投票在线程池里跑,几票同时在飞。

    线程不继承 ContextVar,不带上下文那几票会悄悄用部署默认模型。而上下文必须
    **每票各复制一份**:同一个 Context 不能被两个线程同时进入,共用一份在并发下会抛
    "cannot enter context"。栅栏保证三票真的重叠,把这两种写法都钉住。
    """

    import contextvars

    captured: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)
    gateway = _gateway(captured)

    def ballot() -> None:
        barrier.wait()
        with lock:
            _ask(gateway)

    with (
        model_overrides(ModelOverrides(llm_id="assistant-model")),
        ThreadPoolExecutor(max_workers=3) as pool,
    ):
        futures = [pool.submit(contextvars.copy_context().run, ballot) for _ in range(3)]
        for future in futures:
            future.result()

    assert [item["model"]["llm_id"] for item in captured] == ["assistant-model"] * 3
