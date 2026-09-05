"""问数可观测性与超时 / 重试合同（2026-09-05 提速盘点第 1、2 条）。

一轮问数的时间几乎全在模型往返（实测非模型阶段合计 <1.5s，模型 7–60s），此前没有
任何一处记它。这里钉住：阶段带服务端时间戳、每次模型 / 向量调用有记录、问数链路上
的模型调用按用途封顶超时、超时不在传输层重试同一个 prompt。
"""

from __future__ import annotations

import httpx
import pytest

from knowflow_analytics.gateways.calls import capture_calls, record_call
from knowflow_analytics.gateways.model import HttpModelGateway, ModelGatewayError
from knowflow_analytics.query.contracts import ObservedTrace, QueryStage, QueryTraceStep


class TestStageTimestamps:
    def test_every_recorded_stage_carries_elapsed_ms(self) -> None:
        seen: list[QueryTraceStep] = []
        trace = ObservedTrace(
            [QueryTraceStep(stage=QueryStage.PRECHECK, status="started")], observer=seen.append
        )
        trace.append(QueryTraceStep(stage=QueryStage.PRECHECK, status="completed"))
        trace[-1] = QueryTraceStep(stage=QueryStage.FINAL_PARSING, status="started")

        assert all(step.elapsed_ms is not None for step in trace)
        assert [step.elapsed_ms for step in trace] == sorted(step.elapsed_ms for step in trace)
        # 观察者看到的和列表里存的是同一份盖过章的对象。
        assert all(step.elapsed_ms is not None for step in seen)

    def test_an_explicit_timestamp_is_kept(self) -> None:
        trace = ObservedTrace()
        trace.append(QueryTraceStep(stage=QueryStage.PRECHECK, status="started", elapsed_ms=42))
        assert trace[0].elapsed_ms == 42


class TestCallLog:
    def test_calls_are_recorded_only_inside_a_capture(self) -> None:
        record_call(kind="model", purpose="outside")  # 无人收集：不报错、不记
        with capture_calls() as calls:
            record_call(kind="model", purpose="analytics.s2sql", elapsed_ms=7)
            with capture_calls() as inner:
                record_call(kind="embedding", purpose="embedding", elapsed_ms=1)
            record_call(kind="model", purpose="analytics.result_interpretation", elapsed_ms=3)
        assert [item["purpose"] for item in calls] == [
            "analytics.s2sql",
            "analytics.result_interpretation",
        ]
        assert [item["purpose"] for item in inner] == ["embedding"]


class _Client:
    """假 httpx 客户端：按脚本返回或抛出，记录每次 post 的 timeout。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def post(self, path, *, headers, json, timeout=None):
        self.calls.append({"path": path, "timeout": timeout, "body": json})
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class _Response:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "http://core/v1/analytics/internal/model/generate")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=self.request, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


def _gateway(script, timeout_seconds=60.0) -> tuple[HttpModelGateway, _Client]:
    client = _Client(script)
    gateway = HttpModelGateway(
        base_url="http://core",
        service_token="secret",
        llm_id="model-x",
        timeout_seconds=timeout_seconds,
        client=client,  # type: ignore[arg-type]
    )
    return gateway, client


def _call(gateway: HttpModelGateway, purpose: str = "analytics.s2sql"):
    return gateway.generate_json(
        purpose=purpose,
        messages=[{"role": "user", "content": "各门店销售额"}],
        response_schema={"type": "object"},
        trace={"tenant_id": "t1"},
    )


_OK = _Response(200, {"code": 0, "data": {"structured": {"sql": "SELECT 1"}}})


class TestTimeoutPolicy:
    def test_ask_purposes_are_capped_below_the_global_timeout(self) -> None:
        gateway, client = _gateway([_OK, _OK], timeout_seconds=240.0)
        _call(gateway, "analytics.s2sql")
        _call(gateway, "analytics.modeling")
        # 问数链路 30s 封顶；建模沿用全局（AI 补全要跑很久）。
        assert [item["timeout"] for item in client.calls] == [30.0, 240.0]

    def test_the_cap_never_raises_a_smaller_global_timeout(self) -> None:
        gateway, client = _gateway([_OK], timeout_seconds=10.0)
        _call(gateway)
        assert client.calls[0]["timeout"] == 10.0

    def test_a_timeout_is_not_retried_on_the_transport_layer(self) -> None:
        # 同一个 prompt 再等一遍只是把尾巴拉长；换一种生成是解析器重试链的事。
        gateway, client = _gateway([httpx.ReadTimeout("slow"), _OK])
        with pytest.raises(ModelGatewayError):
            _call(gateway)
        assert len(client.calls) == 1

    def test_a_5xx_is_still_retried(self, monkeypatch) -> None:
        monkeypatch.setattr("knowflow_analytics.gateways.model.time.sleep", lambda _s: None)
        gateway, client = _gateway([_Response(502), _OK])
        assert _call(gateway) == {"sql": "SELECT 1"}
        assert len(client.calls) == 2


class TestGatewayRecordsItsCalls:
    def test_success_and_failure_are_both_recorded(self, monkeypatch) -> None:
        monkeypatch.setattr("knowflow_analytics.gateways.model.time.sleep", lambda _s: None)
        gateway, _client = _gateway([_OK, httpx.ReadTimeout("slow")])
        with capture_calls() as calls:
            _call(gateway)
            with pytest.raises(ModelGatewayError):
                _call(gateway)

        assert [(item["purpose"], item["ok"]) for item in calls] == [
            ("analytics.s2sql", True),
            ("analytics.s2sql", False),
        ]
        assert calls[0]["prompt_chars"] == len("各门店销售额")
        assert calls[1]["error"] == "ModelGatewayError"
        assert all("elapsed_ms" in item for item in calls)
