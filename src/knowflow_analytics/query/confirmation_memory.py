"""Release-bound memory of an explicit user semantic confirmation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from knowflow_analytics.contracts import FrozenModel


class ConfirmationMemory(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(min_length=1, max_length=128)
    spec_hash: str = Field(min_length=1, max_length=128)
    index_snapshot_id: str = Field(min_length=1, max_length=128)
    detected_text: str = Field(min_length=1, max_length=4_000)
    normalized_phrase: str = Field(min_length=1, max_length=4_000)
    selection_kind: Literal["metric", "dimension", "dimension_value", "analysis_object"]
    semantic_element_id: str | None = Field(default=None, max_length=512)
    dataset_id: str | None = Field(default=None, max_length=128)
    candidate_set_hash: str = Field(min_length=1, max_length=128)
    exact_context_hash: str = Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle_and_selection(self) -> ConfirmationMemory:
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("confirmation memory timestamps must include timezone")
        if self.revoked_at is not None and self.revoked_at.utcoffset() is None:
            raise ValueError("confirmation memory revoked_at must include timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("confirmation memory must expire after creation")
        if self.selection_kind == "analysis_object":
            if self.dataset_id is None or self.semantic_element_id is not None:
                raise ValueError("analysis object memory requires only a dataset selection")
        elif self.semantic_element_id is None:
            raise ValueError("semantic confirmation memory requires an element selection")
        return self


class ConfirmationSuggestion(FrozenModel):
    """Pending, review-only alias/Term evidence derived from explicit choices."""

    id: str
    detected_text: str
    selection_kind: Literal["metric", "dimension", "dimension_value", "analysis_object"]
    semantic_element_id: str | None = None
    dataset_id: str | None = None
    confirmation_count: int = Field(ge=1)
    latest_confirmed_at: datetime
    status: Literal["pending_review"] = "pending_review"


class ConfirmationMemoryStore(Protocol):
    def save_confirmation_memory(self, memory: ConfirmationMemory) -> None: ...

    def find_confirmation_memory(
        self,
        *,
        actor_id: str,
        project_id: str,
        release_id: str,
        spec_hash: str,
        index_snapshot_id: str,
        normalized_phrase: str,
        candidate_set_hash: str,
        exact_context_hash: str,
        now: datetime,
    ) -> ConfirmationMemory | None: ...

    def list_confirmation_memories(
        self,
        *,
        actor_id: str,
        project_id: str,
        include_revoked: bool = False,
    ) -> tuple[ConfirmationMemory, ...]: ...

    def revoke_confirmation_memory(
        self,
        *,
        memory_id: str,
        actor_id: str,
        project_id: str,
        revoked_at: datetime,
    ) -> bool: ...


def confirmation_candidate_set_hash(
    values: Iterable[tuple[str, str, str | None]],
) -> str:
    from knowflow_analytics.hashing import content_hash

    return content_hash(
        sorted(
            {
                (kind.strip().casefold(), element_id, dataset_id or "")
                for kind, element_id, dataset_id in values
            }
        )
    )
