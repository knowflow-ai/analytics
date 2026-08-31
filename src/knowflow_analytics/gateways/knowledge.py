from __future__ import annotations

from typing import Protocol

import httpx

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.modeling.contracts import EvidenceRef


class KnowledgeGatewayError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "KNOWLEDGE_GATEWAY_FAILED") -> None:
        super().__init__(message, code=code, stage="MODELING_EVIDENCE")


class KnowledgeGateway(Protocol):
    def search(
        self,
        *,
        modeling_job_id: str,
        manifest_hash: str,
        question: str,
        target_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[EvidenceRef, ...]: ...


class HttpKnowledgeGateway:
    """Search only the immutable document manifest authorized by ragflow-server."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout_seconds, trust_env=False
        )
        self._service_token = service_token

    def search(
        self,
        *,
        modeling_job_id: str,
        manifest_hash: str,
        question: str,
        target_ids: tuple[str, ...],
        limit: int = 8,
    ) -> tuple[EvidenceRef, ...]:
        try:
            response = self._client.post(
                "/v1/analytics/internal/knowledge/search",
                headers={"X-KnowFlow-Agent-Token": self._service_token},
                json={
                    "modeling_job_id": modeling_job_id,
                    "manifest_hash": manifest_hash,
                    "question": question,
                    "target_ids": target_ids,
                    "limit": min(max(limit, 1), 20),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KnowledgeGatewayError("knowledge evidence search failed") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise KnowledgeGatewayError("knowledge gateway rejected the request")
        data = payload.get("data")
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            raise KnowledgeGatewayError(
                "knowledge gateway returned an invalid contract",
                code="KNOWLEDGE_RESPONSE_INVALID",
            )
        return tuple(EvidenceRef.model_validate(item) for item in hits)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
