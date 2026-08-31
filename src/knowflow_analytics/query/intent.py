from __future__ import annotations

import re

from knowflow_analytics.query.errors import SemanticParsingError

_UNSUPPORTED_OPERATIONS = (
    (re.compile(r"增长率|增幅|涨幅|降幅|(?:增长|增加|减少|下降)了?多少"), "增长率"),
    (re.compile(r"预测|预估|预计|forecast", re.IGNORECASE), "预测分析"),
)


def reject_unsupported_intent(question: str) -> None:
    requested = [label for pattern, label in _UNSUPPORTED_OPERATIONS if pattern.search(question)]
    if requested:
        raise SemanticParsingError(
            f"当前版本尚未开放{'、'.join(dict.fromkeys(requested))}，不会降级成普通聚合查询。",
            code="UNSUPPORTED_ANALYTIC_OPERATION",
        )
