"""Built-in S2SQL syntax examples used when no reviewed exemplar exists.

Parity source: the pinned standalone launcher loads ``s2-exemplar.json`` into the
embedding store at startup (``ExemplarServiceImpl.loadSysExemplars``), so upstream
never generates S2SQL without few-shot examples. A freshly modelled KnowFlow
project has no human-reviewed GoldenCase yet, which would otherwise leave the
model to infer the dialect from prose rules alone.

These examples deliberately use a neutral fictional schema. They teach *syntax*
only -- quoting, explicit aggregation, comparison-operator time ranges, governed
value filters -- and never the customer's business names, so they cannot be
mistaken for evidence about the live semantic model or leak one project's
vocabulary into another. Reviewed release-bound exemplars remain authoritative
and are shown separately; these are the floor, not a replacement.

Unlike the pinned upstream file, every aggregate projection states its aggregation
explicitly, because the KnowFlow prompt requires that and an inconsistent example
would teach the model to violate it.
"""

from __future__ import annotations

from typing import Final

SYNTAX_EXEMPLARS: Final[tuple[dict[str, str], ...]] = (
    {
        "question": "各部门访问次数",
        "sql": 'SELECT "部门", SUM("访问次数") FROM "示例数据集" GROUP BY "部门"',
    },
    {
        "question": "今年以来的访问人数",
        "sql": (
            'SELECT SUM("访问人数") FROM "示例数据集" '
            "WHERE \"数据日期\" >= '2024-01-01' AND \"数据日期\" <= '2024-06-30'"
        ),
    },
    {
        "question": "美术部和技术研发部的访问时长",
        "sql": (
            'SELECT "部门", SUM("访问时长") FROM "示例数据集" '
            "WHERE \"部门\" IN ('美术部', '技术研发部') GROUP BY \"部门\""
        ),
    },
    {
        "question": "访问次数最多的前 3 个部门",
        "sql": (
            'SELECT "部门", SUM("访问次数") FROM "示例数据集" '
            'GROUP BY "部门" ORDER BY SUM("访问次数") DESC LIMIT 3'
        ),
    },
)
