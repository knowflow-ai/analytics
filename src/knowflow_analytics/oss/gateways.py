"""OpenAI-compatible replacements for the RAGFlow-hosted gateways.

They satisfy the same Protocols the core consumes (``StructuredModelGateway``,
``EmbeddingGateway``) so the core never learns which
edition it is running in.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from typing import Any

import httpx
import jsonschema

from knowflow_analytics.gateways.embedding import EmbeddingGatewayError
from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.oss.config import ModelEndpoint
from knowflow_analytics.semantic.index import EmbeddingBatch

LOGGER = logging.getLogger(__name__)

_TEMPERATURE_BY_ATTEMPT = {1: 0.0, 2: 0.3, 3: 0.6}
_DEFAULT_OUTPUT_BUDGET = 4096
_DEFAULT_OUTPUT_CEILING = 16_384
_TRANSPORT_RETRIES = 3
_BACKOFF_SECONDS = (0.5, 2.0, 5.0)
_MAX_EMBEDDING_BATCH_SIZE = 128
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_THINK = re.compile(r"^.*</think>", re.DOTALL)
_SCHEMA_INSTRUCTION = (
    "Return exactly one JSON object matching this JSON Schema. "
    "Do not return Markdown, explanations, tool calls, or physical SQL unless the schema "
    "explicitly contains such a field.\n"
)


def _headers(endpoint: ModelEndpoint) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = endpoint.api_key.get_secret_value()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


_FORMAT_MARKERS = ("response_format", "json_schema", "json_object", "structured output")


def _rejects_format(body: str) -> bool:
    """Only a complaint about the format itself justifies downgrading the mode.

    Other 400s (context length, unknown model, content filter) must surface
    as-is instead of silently switching every later call to a weaker mode.
    """

    lowered = body.lower()
    return any(marker in lowered for marker in _FORMAT_MARKERS)


def _bounded_int(value: object, *, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _with_schema_prompt(
    messages: list[dict[str, str]], schema: dict[str, Any]
) -> list[dict[str, str]]:
    """Fold every system part plus the schema into one leading system message.

    Mirrors the commercial RAGFlow gateway: the model sees the schema as text,
    so providers that ignore ``response_format`` still know the shape.
    """

    system_parts = [
        m["content"] for m in messages if m.get("role") == "system" and m.get("content")
    ]
    history = [m for m in messages if m.get("role") != "system"]
    system_parts.append(
        _SCHEMA_INSTRUCTION + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    )
    return [{"role": "system", "content": "\n\n".join(system_parts)}, *history]


def _parse_json_object(text: str) -> dict[str, Any]:
    # Reasoning models prepend their thinking; only the final answer is JSON.
    cleaned = _FENCE.sub("", _THINK.sub("", text.strip()).strip())
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("model output is not a JSON object")
    return parsed


class OpenAiCompatibleModelGateway:
    """Chat-completions client that always asks for, and validates, a JSON object.

    Providers differ in structured-output support, so the request degrades from
    ``json_schema`` to ``json_object`` to plain text, remembering the first mode
    the provider accepted.
    """

    _MODES = ("json_schema", "json_object", "text")

    def __init__(
        self,
        endpoint: ModelEndpoint,
        *,
        timeout_seconds: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=endpoint.base_url, timeout=timeout_seconds, trust_env=False
        )
        self._mode_index = 0
        self._mode_lock = threading.Lock()

    def _response_format(self, mode: str, purpose: str, schema: dict[str, Any]) -> dict | None:
        if mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": purpose.replace(".", "_"), "schema": schema},
            }
        if mode == "json_object":
            return {"type": "json_object"}
        return None

    def _post_with_backoff(self, body: dict[str, Any], *, purpose: str) -> httpx.Response:
        last: Exception | None = None
        for index in range(_TRANSPORT_RETRIES):
            try:
                response = self._client.post(
                    "/chat/completions", headers=_headers(self._endpoint), json=body
                )
                if response.status_code < 500:
                    return response
                last = httpx.HTTPStatusError(
                    f"upstream {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except httpx.HTTPError as exc:
                last = exc
            if index < _TRANSPORT_RETRIES - 1:
                LOGGER.warning(
                    "oss model gateway transport error purpose=%s attempt=%d: %s",
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
        attempt = _bounded_int(trace.get("attempt"), default=1, lo=1, hi=3)
        # The caller estimates how long its *answer* is; the endpoint states how
        # much this deployment may emit at all.  Neither is discoverable from the
        # provider, so both stay explicit.
        ceiling = self._endpoint.max_output_tokens or _DEFAULT_OUTPUT_CEILING
        max_tokens = _bounded_int(
            trace.get("max_tokens_hint"),
            default=min(_DEFAULT_OUTPUT_BUDGET, ceiling),
            lo=min(512, ceiling),
            hi=ceiling,
        )
        prompt = _with_schema_prompt(messages, response_schema)
        while True:
            with self._mode_lock:
                mode_index = min(self._mode_index, len(self._MODES) - 1)
            mode = self._MODES[mode_index]
            body: dict[str, Any] = {
                "model": self._endpoint.model,
                "messages": prompt,
                "temperature": _TEMPERATURE_BY_ATTEMPT.get(attempt, 0.6),
                "max_tokens": max_tokens,
            }
            response_format = self._response_format(mode, purpose, response_schema)
            if response_format is not None:
                body["response_format"] = response_format
            if self._endpoint.thinking == "off":
                # No provider agrees on the spelling: SiliconFlow and DashScope
                # read the top-level flag, vLLM reads chat_template_kwargs.  Both
                # are sent because OpenAI-compatible servers ignore fields they
                # do not know (verified against DeepSeek-V3 and Qwen2.5-32B).
                body["enable_thinking"] = False
                body["chat_template_kwargs"] = {"enable_thinking": False}
            response = self._post_with_backoff(body, purpose=purpose)
            if response.status_code == 400 and mode != "text" and _rejects_format(response.text):
                LOGGER.info(
                    "oss model gateway: provider rejected response_format=%s, downgrading", mode
                )
                with self._mode_lock:
                    self._mode_index = max(self._mode_index, mode_index + 1)
                continue
            if response.status_code >= 400:
                LOGGER.warning(
                    "oss model gateway rejected purpose=%s status=%s body=%s",
                    purpose,
                    response.status_code,
                    response.text[:500],
                )
                raise ModelGatewayError("model gateway rejected the request")
            return self._extract(
                response,
                purpose=purpose,
                schema=response_schema,
                requested_max_tokens=max_tokens,
            )

    def _extract(
        self,
        response: httpx.Response,
        *,
        purpose: str,
        schema: dict[str, Any],
        requested_max_tokens: int,
    ) -> dict[str, Any]:
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
            content = message["content"]
            if isinstance(content, list):
                content = "".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
            # A reasoning model spends the same output budget on thinking first.
            # When the budget runs out mid-thought the provider returns an empty
            # ``content`` beside a populated ``reasoning_content``: the model
            # never wrote an answer.  Reporting that as invalid output hides the
            # only two things that fix it, so name it and name them.
            if not str(content).strip() and str(message.get("reasoning_content") or "").strip():
                LOGGER.warning(
                    "oss model gateway truncated inside reasoning purpose=%s: "
                    "budget=%d exhausted before the answer; raise the endpoint's "
                    "max output tokens or set thinking=off",
                    purpose,
                    requested_max_tokens,
                )
                raise ModelGatewayError(
                    "模型在思考阶段用尽输出预算，未写出答案；"
                    "请调高该模型的最大输出长度，或在模型设置里关闭思考模式。",
                    code="MODEL_OUTPUT_TRUNCATED_IN_REASONING",
                )
            parsed = _parse_json_object(str(content))
            # Reject shape mismatches here so the caller's attempt/temperature
            # retry kicks in instead of a malformed payload failing downstream.
            jsonschema.validate(parsed, schema)
            return parsed
        except ModelGatewayError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, jsonschema.ValidationError) as exc:
            LOGGER.warning(
                "oss model gateway invalid output purpose=%s: %s; raw=%r",
                purpose,
                exc,
                response.text[:600],
            )
            raise ModelGatewayError(
                "model gateway returned no structured payload", code="MODEL_OUTPUT_INVALID"
            ) from exc

    def probe(self) -> str:
        """One tiny round trip used by the settings page's connection test."""

        result = self.generate_json(
            purpose="oss.probe",
            messages=[{"role": "user", "content": 'Reply with {"ok": true}.'}],
            response_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            trace={},
        )
        if result.get("ok") is not True:
            raise ModelGatewayError("model did not return the expected probe payload")
        return self._endpoint.model

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class OpenAiCompatibleEmbeddingGateway:
    def __init__(
        self,
        endpoint: ModelEndpoint,
        *,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=endpoint.base_url, timeout=timeout_seconds, trust_env=False
        )

    def for_tenant(self, tenant_id: str) -> OpenAiCompatibleEmbeddingGateway:
        # Single-user edition: every tenant shares the one configured model.
        return self

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        if not texts:
            raise EmbeddingGatewayError(
                "embedding gateway requires at least one text",
                code="EMBEDDING_REQUEST_INVALID",
            )
        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        for offset in range(0, len(texts), _MAX_EMBEDDING_BATCH_SIZE):
            batch = self._encode_batch(texts[offset : offset + _MAX_EMBEDDING_BATCH_SIZE])
            if dimension is None:
                dimension = len(batch[0])
            if any(len(vector) != dimension for vector in batch):
                raise EmbeddingGatewayError(
                    "embedding gateway changed vector dimension between batches",
                    code="EMBEDDING_RESPONSE_INVALID",
                )
            vectors.extend(batch)
        return EmbeddingBatch(
            model_id=self._endpoint.model, dimension=dimension or 0, vectors=tuple(vectors)
        )

    def _encode_batch(self, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        try:
            response = self._client.post(
                "/embeddings",
                headers=_headers(self._endpoint),
                json={"model": self._endpoint.model, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingGatewayError("embedding gateway request failed") from exc
        try:
            rows = sorted(payload["data"], key=lambda item: int(item.get("index", 0)))
            vectors = [tuple(float(value) for value in row["embedding"]) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingGatewayError(
                "embedding gateway returned an invalid contract",
                code="EMBEDDING_RESPONSE_INVALID",
            ) from exc
        if len(vectors) != len(texts) or any(
            not vector or any(not math.isfinite(value) for value in vector) for vector in vectors
        ):
            raise EmbeddingGatewayError(
                "embedding gateway returned an incomplete batch",
                code="EMBEDDING_RESPONSE_INVALID",
            )
        return vectors

    def probe(self) -> int:
        return self.encode(("knowflow",)).dimension

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
