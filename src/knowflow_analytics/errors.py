from __future__ import annotations


class AnalyticsError(RuntimeError):
    """Base error with a stable machine-readable code and execution stage."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        stage: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = dict(details or {})


class SemanticValidationError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "INVALID_SEMANTIC_MODEL") -> None:
        super().__init__(message, code=code, stage="VALIDATING")


class TranslationError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "TRANSLATION_FAILED") -> None:
        super().__init__(message, code=code, stage="TRANSLATING")


class QueryGuardError(AnalyticsError):
    def __init__(self, message: str, *, code: str = "UNSAFE_PHYSICAL_SQL") -> None:
        super().__init__(message, code=code, stage="PHYSICAL_SQL_VALIDATING")


class QueryExecutionError(AnalyticsError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "QUERY_EXECUTION_FAILED",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, stage="EXECUTING", details=details)
