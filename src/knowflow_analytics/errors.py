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
    def __init__(
        self,
        message: str,
        *,
        code: str = "TRANSLATION_FAILED",
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, code=code, stage="TRANSLATING", details=details)


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


def failure_message(exc: AnalyticsError) -> str:
    """失败信息带上数据库的真实报错。

    「PostgreSQL query failed」对排障等于零:sqlstate 与 message_primary 早已
    被执行器安全截取进 exc.details(不含 SQL 原文),却只在诊断 trace 里——评测卡、
    普通失败响应和发布检查的指标样本都看不到,用户只知道挂了、不知道为什么
    (2026-08-26 问数实测;2026-09-28 客户 MySQL 发布检查 8 条阻断全是同一句)。
    """

    message = str(exc)
    database_message = exc.details.get("database_message")
    sqlstate = exc.details.get("sqlstate")
    if database_message:
        suffix = f" [{sqlstate}]" if sqlstate else ""
        return f"{message}: {database_message}{suffix}"
    return message
