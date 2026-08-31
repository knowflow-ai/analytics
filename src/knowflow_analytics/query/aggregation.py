from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from knowflow_analytics.contracts import Aggregation


class RuleAggregateType(StrEnum):
    """Governed aggregation names the rule parser may produce."""

    SUM = "sum"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    TOPN = "topn"
    DISTINCT = "distinct"
    COUNT = "count"
    NONE = "none"


_EXECUTABLE_AGGREGATIONS: dict[RuleAggregateType, Aggregation] = {
    RuleAggregateType.SUM: Aggregation.SUM,
    RuleAggregateType.AVG: Aggregation.AVG,
    RuleAggregateType.MAX: Aggregation.MAX,
    RuleAggregateType.MIN: Aggregation.MIN,
    RuleAggregateType.DISTINCT: Aggregation.COUNT_DISTINCT,
    RuleAggregateType.COUNT: Aggregation.COUNT,
}


@dataclass(frozen=True)
class AggregationIntent:
    """Complete contract for the rule aggregation parser output."""

    aggregate_type: RuleAggregateType = RuleAggregateType.NONE
    matched_phrase: str | None = None

    @property
    def aggregation(self) -> Aggregation | None:
        """Return the executable aggregate operator, if this type has one."""

        return _EXECUTABLE_AGGREGATIONS.get(self.aggregate_type)


# All eight enum values are kept. TOPN and NONE are rule-stage intent types,
# not physical aggregate operators in the execution contract.
_AGGREGATION_GRAMMAR_VERSION = "knowflow-aggregate-type-v1"
_AGGREGATION_PATTERNS: tuple[tuple[RuleAggregateType, re.Pattern[str]], ...] = (
    (RuleAggregateType.MAX, re.compile(r"最大值|最大|max|峰值|最高|最多", re.IGNORECASE)),
    (RuleAggregateType.MIN, re.compile(r"最小值|最小|min|最低|最少", re.IGNORECASE)),
    (RuleAggregateType.SUM, re.compile(r"汇总|总和|sum", re.IGNORECASE)),
    (RuleAggregateType.AVG, re.compile(r"平均值|日均|平均|avg", re.IGNORECASE)),
    (RuleAggregateType.TOPN, re.compile(r"top", re.IGNORECASE)),
    (RuleAggregateType.DISTINCT, re.compile(r"uv", re.IGNORECASE)),
    (RuleAggregateType.COUNT, re.compile(r"总数|pv", re.IGNORECASE)),
    (RuleAggregateType.NONE, re.compile(r"明细", re.IGNORECASE)),
)


def aggregation_grammar_version() -> str:
    return _AGGREGATION_GRAMMAR_VERSION


def parse_aggregation_intent(question: str) -> AggregationIntent:
    """Scan the raw question at the rule-parser stage.

    Matches are counted for each supported aggregate family and the family with
    the highest count wins. All eight enum values and the exact regex declaration
    order are preserved. Where a hash-map implementation would leave equal-count
    iteration order unspecified, the tuple here
    order only stabilizes that otherwise undefined tie and does not add a semantic
    rule or broaden the upstream vocabulary.
    """

    matches: list[tuple[RuleAggregateType, int, str]] = []
    for aggregate_type, pattern in _AGGREGATION_PATTERNS:
        phrases = tuple(item.group(0) for item in pattern.finditer(question))
        if phrases:
            matches.append((aggregate_type, len(phrases), phrases[-1]))
    if not matches:
        return AggregationIntent()

    aggregate_type, _count, phrase = max(matches, key=lambda item: item[1])
    return AggregationIntent(aggregate_type=aggregate_type, matched_phrase=phrase)
