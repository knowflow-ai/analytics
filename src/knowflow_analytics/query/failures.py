from __future__ import annotations

from typing import Protocol

from knowflow_analytics.query.contracts import QueryFailureRecord


class QueryFailureStore(Protocol):
    """Sink for refused questions. Separate from QueryHistoryStore on purpose:
    that one serves multi-turn rewrite and is keyed by conversation; this one
    is an append-only log for offline vocabulary mining."""

    def save_failure(
        self,
        record: QueryFailureRecord,
        *,
        actor_id: str,
        project_id: str,
    ) -> None: ...
