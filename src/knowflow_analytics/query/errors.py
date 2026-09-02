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
    """解析失败。

    ``stage`` 默认 FINAL_PARSING——绝大多数解析失败确实发生在那里。但同一个异常
    类型也被入口闸门（版本门、数据范围解析）复用，那些在 PRECHECK 就失败了；
    记成 FINAL_PARSING 会让诊断把"配置有问题"显示成"模型没理解"。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "SEMANTIC_PARSING_FAILED",
        details: dict[str, object] | None = None,
        stage: str = "FINAL_PARSING",
    ) -> None:
        super().__init__(message, code=code, stage=str(stage), details=details)


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
