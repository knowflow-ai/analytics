from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from knowflow_analytics.contracts import FrozenModel


class DomainLifecycle(StrEnum):
    """Status values meaningful for an analytics domain."""

    INITIALIZED = "initialized"
    ONLINE = "online"
    OFFLINE = "offline"


class DomainGovernance(FrozenModel):
    project_id: str = Field(min_length=1, max_length=128)
    parent_project_id: str | None = Field(default=None, min_length=1, max_length=128)
    classifications: tuple[str, ...] = Field(default=(), max_length=100)
    lifecycle: DomainLifecycle = DomainLifecycle.INITIALIZED
    etag: int = Field(ge=1)
    updated_by: str = Field(min_length=1, max_length=128)
    updated_at: datetime

    @field_validator("classifications")
    @classmethod
    def normalize_classifications(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 128 for item in normalized):
            raise ValueError("domain classifications are invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("domain classifications must be unique")
        return normalized
