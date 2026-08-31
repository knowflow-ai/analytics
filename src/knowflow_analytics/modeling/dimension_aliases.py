from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from pydantic import Field, ValidationError, model_validator

from knowflow_analytics.contracts import FrozenModel
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.gateways.model import ModelGatewayError, StructuredModelGateway
from knowflow_analytics.modeling.contracts import (
    DimensionValueCandidate,
    ModelingRevision,
)


class _AliasItem(FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def aliases_are_clean(self) -> _AliasItem:
        if not self.display_name.strip():
            raise ValueError("display name cannot be blank")
        normalized = [item.strip().casefold() for item in self.aliases]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("aliases must be non-blank and unique")
        return self


class _AliasOutput(FrozenModel):
    items: tuple[_AliasItem, ...] = Field(max_length=500)


class DimensionValueAliasSuggester:
    """Generate review-only aliases for observed values.

    Dimension alias editing is a governed form.  AI generation only fills those
    existing fields; it cannot change raw values, list policy, visibility, or the
    semantic revision by itself.
    """

    _BATCH_SIZE = 200
    _MAX_ATTEMPTS = 3
    _MAX_PARALLEL_DIMENSIONS = 3

    def __init__(
        self, gateway: StructuredModelGateway, *, max_concurrency: int | None = None
    ) -> None:
        self._gateway = gateway
        self._max_concurrency = max(1, max_concurrency or self._MAX_PARALLEL_DIMENSIONS)

    def suggest(
        self,
        *,
        revision: ModelingRevision,
        candidates: tuple[DimensionValueCandidate, ...],
        tenant_id: str = "",
    ) -> dict[str, dict[str, object]]:
        if not candidates:
            return {}
        dimensions = {item.id: item for item in revision.semantic_spec.dimensions}
        output: dict[str, dict[str, object]] = {}
        candidates_by_dimension: dict[str, list[DimensionValueCandidate]] = {}
        for candidate in candidates:
            candidates_by_dimension.setdefault(candidate.dimension_id, []).append(candidate)

        # Dimension-value aliases are generated one dimension at a
        # time.  Keep the same semantic boundary so values from unrelated
        # dimensions cannot influence one another or inflate one model response.
        # Parity source: DimensionServiceImpl.mockDimensionValueAlias.
        dimension_groups = tuple(
            tuple(dimension_candidates) for dimension_candidates in candidates_by_dimension.values()
        )
        with ThreadPoolExecutor(
            max_workers=min(self._max_concurrency, len(dimension_groups)),
            thread_name_prefix="analytics-dimension-alias",
        ) as executor:
            futures = [
                executor.submit(
                    self._suggest_dimension,
                    revision=revision,
                    candidates=dimension_candidates,
                    dimensions=dimensions,
                    tenant_id=tenant_id,
                )
                for dimension_candidates in dimension_groups
            ]
            for future in futures:
                output.update(future.result())
        return output

    def _suggest_dimension(
        self,
        *,
        revision: ModelingRevision,
        candidates: tuple[DimensionValueCandidate, ...],
        dimensions: dict,
        tenant_id: str = "",
    ) -> dict[str, dict[str, object]]:
        output: dict[str, dict[str, object]] = {}
        for offset in range(0, len(candidates), self._BATCH_SIZE):
            batch = candidates[offset : offset + self._BATCH_SIZE]
            # One batch asks for up to 200 values at once, so a single schema slip
            # is likely and used to abort the entire one-click modeling run. The
            # ModelSchema stage retries for the same reason; the attempt number
            # also lets the gateway raise temperature to escape a repeated bad
            # generation.
            parsed = None
            last_error: Exception | None = None
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                try:
                    payload = self._gateway.generate_json(
                        purpose="analytics.dimension_value_aliases",
                        messages=self._messages(batch=batch, dimensions=dimensions),
                        response_schema=_AliasOutput.model_json_schema(),
                        trace={
                            "revision_id": revision.id,
                            "revision_etag": revision.etag,
                            "schema_snapshot_hash": revision.schema_snapshot_hash,
                            "dimension_id": batch[0].dimension_id,
                            "candidate_ids": [item.id for item in batch],
                            "contract_version": "dimension-value-alias-v1",
                            "tenant_id": tenant_id,
                            "attempt": str(attempt),
                            "upstream_commit": "af08d869c4609bf8d48d64e78c61427fe93f7489",
                        },
                    )
                    candidate_output = _AliasOutput.model_validate(payload)
                    expected_ids = {item.id for item in batch}
                    returned_ids = [item.candidate_id for item in candidate_output.items]
                    if len(returned_ids) != len(set(returned_ids)) or (
                        set(returned_ids) != expected_ids
                    ):
                        raise SemanticValidationError(
                            "AI alias output must contain every requested candidate exactly once",
                            code="AI_ALIAS_OUTPUT_INVALID",
                        )
                except (ModelGatewayError, ValidationError, SemanticValidationError) as exc:
                    last_error = exc
                    continue
                parsed = candidate_output
                break
            if parsed is None:
                assert last_error is not None
                raise last_error
            output.update(
                {
                    item.candidate_id: {
                        "display_name": item.display_name.strip(),
                        "aliases": tuple(alias.strip() for alias in item.aliases),
                    }
                    for item in parsed.items
                }
            )
        return output

    @staticmethod
    def _messages(*, batch, dimensions) -> list[dict[str, str]]:
        values = [
            {
                "candidate_id": item.id,
                "dimension_id": item.dimension_id,
                "dimension_name": (
                    dimensions[item.dimension_id].name
                    if item.dimension_id in dimensions
                    else item.dimension_id
                ),
                "dimension_description": (
                    dimensions[item.dimension_id].description
                    if item.dimension_id in dimensions
                    else ""
                ),
                "raw_value": item.value,
                "current_display_name": item.display_name,
                "current_aliases": item.aliases,
            }
            for item in batch
        ]
        return [
            {
                "role": "system",
                "content": (
                    "你是企业数据字典编辑助手。只能为输入中的每个 candidate_id 建议"
                    "展示名和常用业务别名；不得改写原始值、编造编码含义、合并不同值、"
                    "设置黑白名单或删除候选。缺少业务证据时保留当前展示名并返回空别名。"
                    "必须逐项返回且只返回符合 JSON Schema 的对象。"
                ),
            },
            {
                "role": "user",
                "content": "dimension_values=" + json.dumps(values, ensure_ascii=False),
            },
        ]
