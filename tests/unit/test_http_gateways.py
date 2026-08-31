from __future__ import annotations

import json

import httpx
import pytest

from knowflow_analytics.gateways.embedding import EmbeddingGatewayError, HttpEmbeddingGateway
from knowflow_analytics.gateways.model import HttpModelGateway, ModelGatewayError


def test_model_gateway_uses_the_existing_ragflow_agent_service_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["token"] = request.headers.get("X-KnowFlow-Agent-Token")
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={"code": 0, "data": {"structured": {"models": []}}},
        )

    client = httpx.Client(
        base_url="http://ragflow.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        llm_id="model@provider",
        client=client,
    )

    result = gateway.generate_json(
        purpose="analytics.modeling",
        messages=[{"role": "user", "content": "建模"}],
        response_schema={"type": "object"},
        trace={"revision_id": "rev-1", "tenant_id": "tenant-1"},
    )

    assert result == {"models": []}
    assert captured["path"] == "/v1/analytics/internal/model/generate"
    assert captured["token"] == "service-token"
    assert "model@provider" in captured["body"]


def test_model_gateway_raises_temperature_after_the_first_parser_attempt():
    temperatures = []

    def handler(request: httpx.Request) -> httpx.Response:
        temperatures.append(json.loads(request.read())["model_params"]["temperature"])
        return httpx.Response(
            200,
            json={"code": 0, "data": {"structured": {"query": {}}}},
        )

    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        llm_id="model@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid",
            transport=httpx.MockTransport(handler),
        ),
    )
    for attempt in ("1", "2", "3"):
        gateway.generate_json(
            purpose="analytics.s2sql",
            messages=[{"role": "user", "content": "query"}],
            response_schema={"type": "object"},
            trace={"attempt": attempt, "tenant_id": "tenant-1"},
        )

    # 逐级升温；此前第 2、3 次都是 0.5，没有递进。
    assert temperatures == [0.0, 0.3, 0.6]


def test_model_gateway_logs_upstream_rejection_without_exposing_it(caplog):
    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        llm_id="model@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "code": 101,
                        "message": "model output does not match the response schema",
                    },
                )
            ),
        ),
    )

    with pytest.raises(ModelGatewayError) as raised:
        gateway.generate_json(
            purpose="analytics.alias_suggestion",
            messages=[{"role": "user", "content": "alias"}],
            response_schema={"type": "object"},
            trace={"tenant_id": "tenant-1"},
        )

    assert str(raised.value) == "model gateway rejected the request"
    assert "analytics.alias_suggestion" in caplog.text
    assert "response schema" in caplog.text


def test_embedding_gateway_unwraps_the_ragflow_response_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/analytics/internal/embeddings/encode"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "model_id": "embedding@provider",
                    "dimension": 2,
                    "vectors": [[1, 0], [0, 1]],
                },
            },
        )

    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        tenant_id="tenant-1",
        embedding_id="embedding@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid",
            transport=httpx.MockTransport(handler),
        ),
    )

    batch = gateway.encode(("收入", "区域"))

    assert batch.model_id == "embedding@provider"
    assert batch.dimension == 2
    assert batch.vectors == ((1.0, 0.0), (0.0, 1.0))


def test_embedding_gateway_batches_large_requests_using_the_ragflow_limit():
    batch_sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.read())["texts"]
        batch_sizes.append(len(texts))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "model_id": "embedding@provider",
                    "dimension": 1,
                    "vectors": [[float(value)] for value in texts],
                },
            },
        )

    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        tenant_id="tenant-1",
        embedding_id="embedding@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid",
            transport=httpx.MockTransport(handler),
        ),
    )

    batch = gateway.encode(tuple(str(index) for index in range(257)))

    assert batch_sizes == [256, 1]
    assert len(batch.vectors) == 257
    assert batch.vectors[-1] == (256.0,)


def test_embedding_gateway_rejects_contract_changes_between_batches():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        texts = json.loads(request.read())["texts"]
        dimension = calls
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "model_id": "embedding@provider",
                    "dimension": dimension,
                    "vectors": [[0.0] * dimension for _ in texts],
                },
            },
        )

    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        tenant_id="tenant-1",
        embedding_id="embedding@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid",
            transport=httpx.MockTransport(handler),
        ),
    )

    with pytest.raises(EmbeddingGatewayError, match="changed model contract"):
        gateway.encode(tuple(str(index) for index in range(257)))


def test_model_gateway_uses_the_tenant_that_owns_the_request():
    """RAGFlow already forwards the signed-in identity as X-KnowFlow-Actor-Id, and
    a RAGFlow tenant id is that user's id. The gateway is an application-wide
    singleton, so the tenant has to arrive with the call; otherwise one
    deployment serves every user with a single tenant's model configuration."""

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"code": 0, "data": {"structured": {"ok": True}}})

    client = httpx.Client(
        base_url="http://ragflow.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        llm_id="",
        client=client,
    )

    gateway.generate_json(
        purpose="analytics.modeling",
        messages=[{"role": "user", "content": "建模"}],
        response_schema={"type": "object"},
        trace={"revision_id": "rev-1", "tenant_id": "tenant-from-request"},
    )

    assert captured["body"]["model"]["tenant_id"] == "tenant-from-request"
    # Routing metadata, not part of the generation audit trail.
    assert "tenant_id" not in captured["body"]["trace"]


def test_embedding_gateway_uses_the_tenant_that_owns_the_request():
    """Embeddings follow the same identity rule as chat: the signed-in tenant is
    known per request, and the gateway is an application-wide singleton, so a
    deployment that pins no tenant must still resolve one from the caller."""

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"model_id": "m", "dimension": 2, "vectors": [[1.0, 0.0]]},
            },
        )

    client = httpx.Client(
        base_url="http://ragflow.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        tenant_id="",
        embedding_id="",
        client=client,
    )

    gateway.for_tenant("tenant-from-request").encode(("净收入",))

    assert captured["body"]["model"]["tenant_id"] == "tenant-from-request"


def test_embedding_gateway_falls_back_to_the_configured_tenant():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"model_id": "m", "dimension": 2, "vectors": [[1.0, 0.0]]},
            },
        )

    client = httpx.Client(
        base_url="http://ragflow.invalid",
        transport=httpx.MockTransport(handler),
    )
    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        tenant_id="configured-tenant",
        embedding_id="",
        client=client,
    )

    gateway.encode(("净收入",))

    assert captured["body"]["model"]["tenant_id"] == "configured-tenant"


def _gateway(handler):
    return HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="service-token",
        llm_id="model@provider",
        client=httpx.Client(
            base_url="http://ragflow.invalid", transport=httpx.MockTransport(handler)
        ),
    )


def test_model_gateway_retries_transport_errors_and_5xx_with_backoff(monkeypatch):
    """此前 ModelGatewayError 一次都不重试，一次网络抖动整轮建模作废。"""

    import knowflow_analytics.gateways.model as gateway_module

    sleeps = []
    monkeypatch.setattr(gateway_module.time, "sleep", lambda s: sleeps.append(s))
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        if len(calls) == 2:
            return httpx.Response(503, json={"code": 500, "message": "busy"})
        return httpx.Response(200, json={"code": 0, "data": {"structured": {"ok": 1}}})

    result = _gateway(handler).generate_json(
        purpose="analytics.modeling.naming",
        messages=[{"role": "user", "content": "x"}],
        response_schema={"type": "object"},
        trace={"tenant_id": "tenant-1"},
    )

    assert result == {"ok": 1}
    assert len(calls) == 3
    assert sleeps == [0.5, 2.0]


def test_model_gateway_does_not_retry_a_4xx(monkeypatch):
    import knowflow_analytics.gateways.model as gateway_module

    monkeypatch.setattr(
        gateway_module.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError)
    )
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"code": 400})

    try:
        _gateway(handler).generate_json(
            purpose="p",
            messages=[{"role": "user", "content": "x"}],
            response_schema={"type": "object"},
            trace={"tenant_id": "tenant-1"},
        )
    except ModelGatewayError:
        pass
    else:
        raise AssertionError("expected ModelGatewayError")
    assert len(calls) == 1


def test_model_gateway_sizes_max_tokens_from_the_callers_hint():
    """此前硬编码 4096：宽表响应截断 → JSON 解析失败 → 三次相同重试全烧掉。"""

    seen = []

    def handler(request):
        seen.append(json.loads(request.read())["model_params"]["max_tokens"])
        return httpx.Response(200, json={"code": 0, "data": {"structured": {}}})

    gateway = _gateway(handler)
    for hint in ("2800", "99", "999999", "junk", None):
        trace = {"tenant_id": "t"}
        if hint is not None:
            trace["max_tokens_hint"] = hint
        gateway.generate_json(
            purpose="p",
            messages=[{"role": "user", "content": "x"}],
            response_schema={"type": "object"},
            trace=trace,
        )

    assert seen == [2800, 512, 16_384, 4096, 4096]


# ---- 租户唯一来源:当前请求 actor,无 env 兜底 -----------------------------------


def _tenant_harness(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read().decode())
        captured["count"] = captured.get("count", 0) + 1
        return httpx.Response(200, json={"code": 0, "data": {"structured": {"ok": True}}})

    return httpx.Client(base_url="http://ragflow.invalid", transport=httpx.MockTransport(handler))


def test_model_gateway_fails_closed_without_an_actor_tenant():
    """env 静态租户是跨租户泄漏通道:哪个调用丢了租户,就静默用了配置租户的
    模型与额度。移除兜底后,缺租户必须拒绝,而不是替谁扣费。"""

    captured: dict = {}
    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="secret",
        llm_id="",
        client=_tenant_harness(captured),
    )

    with pytest.raises(ModelGatewayError, match="tenant"):
        gateway.generate_json(
            purpose="analytics.modeling.naming",
            messages=[{"role": "user", "content": "x"}],
            response_schema={"type": "object"},
            trace={},
        )
    assert captured.get("count", 0) == 0  # 一个请求都不该发出


def test_model_gateway_uses_the_trace_tenant():
    captured: dict = {}
    gateway = HttpModelGateway(
        base_url="http://ragflow.invalid",
        service_token="secret",
        llm_id="",
        client=_tenant_harness(captured),
    )

    gateway.generate_json(
        purpose="analytics.modeling.naming",
        messages=[{"role": "user", "content": "x"}],
        response_schema={"type": "object"},
        trace={"tenant_id": "actor-1"},
    )

    assert captured["body"]["model"]["tenant_id"] == "actor-1"


def test_embedding_gateway_requires_a_tenant_binding():
    captured: dict = {}
    gateway = HttpEmbeddingGateway(
        base_url="http://ragflow.invalid",
        service_token="secret",
        embedding_id="",
        client=_tenant_harness(captured),
    )

    with pytest.raises(EmbeddingGatewayError, match="tenant"):
        gateway.encode(("文本",))
    with pytest.raises(EmbeddingGatewayError, match="tenant"):
        gateway.for_tenant("")
    assert captured.get("count", 0) == 0
