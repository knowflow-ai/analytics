from __future__ import annotations

import logging
import math

import httpx

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.semantic.index import EmbeddingBatch

_MAX_EMBEDDING_BATCH_SIZE = 256
LOGGER = logging.getLogger(__name__)


class EmbeddingGatewayError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "EMBEDDING_GATEWAY_FAILED") -> None:
        super().__init__(message, code=code, stage="INDEXING")


class HttpEmbeddingGateway:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        embedding_id: str,
        tenant_id: str = "",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, trust_env=False
        )
        self._service_token = service_token
        self._tenant_id = tenant_id
        self._embedding_id = embedding_id

    def for_tenant(self, tenant_id: str) -> HttpEmbeddingGateway:
        """Return a view bound to the tenant that owns the current request.

        RAGFlow knows the signed-in tenant per request while this gateway is an
        application-wide singleton, so the tenant cannot live on the instance.
        The view shares the same HTTP connection pool and never owns it, so
        closing the original gateway remains the single cleanup path.
        """

        normalized = str(tenant_id or "").strip()
        if not normalized:
            # 租户唯一来源是当前请求的 actor;没有兜底租户可退,缺失即拒绝。
            raise EmbeddingGatewayError(
                "embedding call is missing the actor tenant",
                code="EMBEDDING_TENANT_REQUIRED",
            )
        if normalized == self._tenant_id:
            return self
        return type(self)(
            base_url=str(self._client.base_url),
            service_token=self._service_token,
            tenant_id=normalized,
            embedding_id=self._embedding_id,
            client=self._client,
        )

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        if not self._tenant_id:
            raise EmbeddingGatewayError(
                "embedding call is missing the actor tenant",
                code="EMBEDDING_TENANT_REQUIRED",
            )
        if not texts:
            raise EmbeddingGatewayError(
                "embedding gateway requires at least one text",
                code="EMBEDDING_REQUEST_INVALID",
            )
        model_id: str | None = None
        dimension: int | None = None
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), _MAX_EMBEDDING_BATCH_SIZE):
            batch_texts = texts[offset : offset + _MAX_EMBEDDING_BATCH_SIZE]
            batch = self._encode_batch(batch_texts)
            if len(batch.vectors) != len(batch_texts):
                raise EmbeddingGatewayError(
                    "embedding gateway returned an incomplete batch",
                    code="EMBEDDING_RESPONSE_INVALID",
                )
            if model_id is None:
                model_id = batch.model_id
                dimension = batch.dimension
            elif batch.model_id != model_id or batch.dimension != dimension:
                raise EmbeddingGatewayError(
                    "embedding gateway changed model contract between batches",
                    code="EMBEDDING_RESPONSE_INVALID",
                )
            vectors.extend(batch.vectors)
        return EmbeddingBatch(
            model_id=model_id or self._embedding_id,
            dimension=dimension or 0,
            vectors=tuple(vectors),
        )

    def _encode_batch(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        try:
            response = self._client.post(
                "/v1/analytics/internal/embeddings/encode",
                headers={"X-KnowFlow-Agent-Token": self._service_token},
                json={
                    "model": {
                        "tenant_id": self._tenant_id,
                        "embedding_id": self._embedding_id,
                    },
                    "texts": texts,
                    "purpose": "analytics.semantic_index",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingGatewayError("embedding gateway request failed") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            LOGGER.warning(
                "analytics embedding gateway rejected tenant=%s model=%s "
                "batch=%s code=%s message=%s",
                self._tenant_id or "<unset>",
                self._embedding_id or "<tenant-default>",
                len(texts),
                payload.get("code") if isinstance(payload, dict) else None,
                str(payload.get("message") or "")[:500]
                if isinstance(payload, dict)
                else "invalid response envelope",
            )
            raise EmbeddingGatewayError("embedding gateway rejected the request")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise EmbeddingGatewayError(
                "embedding gateway returned an invalid contract",
                code="EMBEDDING_RESPONSE_INVALID",
            )
        try:
            raw_model_id = data["model_id"]
            if not isinstance(raw_model_id, str) or not raw_model_id.strip():
                raise ValueError("model_id is invalid")
            batch = EmbeddingBatch(
                model_id=raw_model_id,
                dimension=int(data["dimension"]),
                vectors=tuple(tuple(float(value) for value in row) for row in data["vectors"]),
            )
            if any(
                len(vector) != batch.dimension or any(not math.isfinite(value) for value in vector)
                for vector in batch.vectors
            ):
                raise ValueError("embedding vectors are invalid")
            return batch
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingGatewayError(
                "embedding gateway returned an invalid contract",
                code="EMBEDDING_RESPONSE_INVALID",
            ) from exc

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
