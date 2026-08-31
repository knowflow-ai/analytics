from __future__ import annotations

from knowflow_analytics.errors import AnalyticsError


class MappingError(AnalyticsError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MAPPING_FAILED",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            stage="CANDIDATE_DISCOVERY",
            details=details,
        )


class SemanticParsingError(AnalyticsError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "SEMANTIC_PARSING_FAILED",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, stage="FINAL_PARSING", details=details)


class SemanticCorrectionError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "S2SQL_CORRECTION_FAILED") -> None:
        super().__init__(message, code=code, stage="S2SQL_CORRECTING")


class ClarificationSignal(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        element_ids: tuple[str, ...] = (),
        degraded_reasons: tuple[str, ...] = (),
        stage: str = "CANDIDATE_DISCOVERY",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.element_ids = element_ids
        self.degraded_reasons = degraded_reasons
        self.stage = stage
