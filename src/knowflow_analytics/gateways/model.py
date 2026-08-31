from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import httpx

from knowflow_analytics.errors import AnalyticsError

LOGGER = logging.getLogger(__name__)


class ModelGatewayError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "MODEL_GATEWAY_FAILED") -> None:
        super().__init__(message, code=code, stage="MODELING_SUGGESTION")


class StructuredModelGateway(Protocol):
    def generate_json(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any],
        trace: dict[str, str],
    ) -> dict[str, Any]: ...


_TEMPERATURE_BY_ATTEMPT = {1: 0.0, 2: 0.3, 3: 0.6}
_TRANSPORT_RETRIES = 3
_BACKOFF_SECONDS = (0.5, 2.0, 5.0)


def _bounded_int(value: object, *, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class HttpModelGateway:
    """Client for RAGFlow's ordinary chat-model gateway; no tool calling is required."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        llm_id: str,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, trust_env=False
        )
        self._service_token = service_token
        self._llm_id = llm_id

    def _post_with_backoff(self, body: dict[str, Any], *, purpose: str) -> Any:
        """网络抖动和 5xx 退避重试；4xx 与信封里的业务拒绝不重试。

        此前 ModelGatewayError 一次都不重试，一次网络抖动整轮建模作废。
        """

        last: Exception | None = None
        for index in range(_TRANSPORT_RETRIES):
            try:
                response = self._client.post(
                    "/v1/analytics/internal/model/generate",
                    headers={"X-KnowFlow-Agent-Token": self._service_token},
                    json=body,
                )
                if 500 <= response.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"upstream {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and 400 <= exc.response.status_code < 500:
                    raise ModelGatewayError("model gateway request failed") from exc
                last = exc
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
            if index < _TRANSPORT_RETRIES - 1:
                LOGGER.warning(
                    "analytics model gateway transport error purpose=%s attempt=%d: %s",
                    purpose,
                    index + 1,
                    last,
                )
                time.sleep(_BACKOFF_SECONDS[index])
        raise ModelGatewayError("model gateway request failed") from last

    def generate_json(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any],
        trace: dict[str, str],
    ) -> dict[str, Any]:
        try:
            attempt = max(1, int(trace.get("attempt", "1")))
        except (TypeError, ValueError):
            attempt = 1
        # 租户唯一来源是当前请求的 actor(RAGFlow 租户 id 就是用户 id),随 trace
        # 传入。没有 env 兜底:静态配置租户意味着任何丢了租户的调用都会静默用
        # 配置租户的模型与额度——跨租户泄漏。缺租户一律拒绝。
        trace = dict(trace)
        tenant_id = str(trace.pop("tenant_id", "") or "").strip()
        if not tenant_id:
            raise ModelGatewayError("model call is missing the actor tenant")
        # 输出预算由调用方按块大小估算（列数 × ~120 token + 表头）；此前硬编码 4096，
        # 宽表响应截断 → JSON 解析失败 → 三次完全相同的重试全烧掉。
        max_tokens = _bounded_int(
            trace.pop("max_tokens_hint", None), default=4096, lo=512, hi=16_384
        )
        body = {
            "model": {"tenant_id": tenant_id, "llm_id": self._llm_id},
            "purpose": purpose,
            "messages": messages,
            "response_schema": {
                "name": purpose.replace(".", "_"),
                "json_schema": response_schema,
            },
            # 第一次确定性生成；校验失败后逐级升温，让模型跳出重复的无效输出。
            # 此前第 2、3 次都是 0.5，没有递进。
            "model_params": {
                "temperature": _TEMPERATURE_BY_ATTEMPT.get(attempt, 0.6),
                "max_tokens": max_tokens,
            },
            "trace": trace,
        }
        payload = self._post_with_backoff(body, purpose=purpose)
        if not isinstance(payload, dict) or payload.get("code") != 0:
            upstream_message = (
                str(payload.get("message") or "").strip()[:500]
                if isinstance(payload, dict)
                else "invalid response envelope"
            )
            LOGGER.warning(
                "analytics model gateway rejected purpose=%s code=%s message=%s",
                purpose,
                payload.get("code") if isinstance(payload, dict) else None,
                upstream_message or "unspecified upstream error",
            )
            raise ModelGatewayError("model gateway rejected the request")
        data = payload.get("data")
        structured = data.get("structured") if isinstance(data, dict) else None
        if not isinstance(structured, dict):
            raise ModelGatewayError(
                "model gateway returned no structured payload", code="MODEL_OUTPUT_INVALID"
            )
        return structured

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
