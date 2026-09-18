"""Built-in S2SQL syntax examples used when no reviewed exemplar exists.

Parity source: the pinned standalone launcher loads ``s2-exemplar.json`` into the
embedding store at startup (``ExemplarServiceImpl.loadSysExemplars``), so upstream
never generates S2SQL without few-shot examples. A freshly modelled KnowFlow
project has no human-reviewed GoldenCase yet, which would otherwise leave the
model to infer the dialect from prose rules alone.

These examples deliberately use a neutral fictional schema. They teach *syntax*
only -- quoting, explicit aggregation, comparison-operator time ranges, governed
value filters, and the compositional shapes (CTE, partitioned windows, scalar
subqueries, set operations, HAVING) -- and never the customer's business names,
so they cannot be mistaken for evidence about the live semantic model or leak one
project's vocabulary into another. Reviewed release-bound exemplars remain
authoritative and are shown separately; these are the floor, not a replacement.

Unlike the pinned upstream file, every aggregate projection states its aggregation
explicitly, because the KnowFlow prompt requires that and an inconsistent example
would teach the model to violate it.

The compositional shapes used to be prose rules in the system prompt ("组内排名写
RANK() OVER (PARTITION BY 维度 ORDER BY SUM(指标) DESC)" and six more). Prose is
unverifiable: nobody notices when it teaches a shape the translator rejects. Every
entry here is run through the translator against a release built from the same
fictional schema (``tests/unit/test_syntax_exemplars_translate.py``), so an example
that stops translating turns red before it ever reaches a model.

Order matters: reviewed exemplars displace entries from the end of this tuple one
for one, so the basics come first and the rarer compositional shapes last.
"""

from __future__ import annotations

from typing import Final

_DS = '"示例数据集"'

SYNTAX_EXEMPLARS: Final[tuple[dict[str, str], ...]] = (
    {
        "question": "各部门访问次数",
        "sql": f'SELECT "部门", SUM("访问次数") FROM {_DS} GROUP BY "部门"',
    },
    {
        "question": "今年以来的访问人数",
        "sql": (
            f'SELECT SUM("访问人数") FROM {_DS} '
            "WHERE \"数据日期\" >= '2024-01-01' AND \"数据日期\" <= '2024-06-30'"
        ),
    },
    {
        "question": "美术部和技术研发部的访问时长",
        "sql": (
            f'SELECT "部门", SUM("访问时长") FROM {_DS} '
            "WHERE \"部门\" IN ('美术部', '技术研发部') GROUP BY \"部门\""
        ),
    },
    {
        "question": "访问次数最多的前 3 个部门",
        "sql": (
            f'SELECT "部门", SUM("访问次数") FROM {_DS} '
            'GROUP BY "部门" ORDER BY SUM("访问次数") DESC LIMIT 3'
        ),
    },
    # 聚合后的条件写 HAVING；明细行的条件才写 WHERE。
    {
        "question": "访问次数总计超过 1000 的部门",
        "sql": (
            f'SELECT "部门", SUM("访问次数") FROM {_DS} '
            'GROUP BY "部门" HAVING SUM("访问次数") > 1000'
        ),
    },
    # 占整个结果集用 RATIO_TO_TOTAL；计算列必须起一个下划线包裹的业务别名。
    {
        "question": "各部门访问次数占比",
        "sql": f'SELECT "部门", RATIO_TO_TOTAL("访问次数") AS "_占比_" FROM {_DS} GROUP BY "部门"',
    },
    # 占本组（而不是占全体）用分区窗口，RATIO_TO_TOTAL 表达不了它。
    {
        "question": "各团队访问次数占本部门的比例",
        "sql": (
            f'SELECT "部门", "团队", SUM("访问次数") '
            '/ SUM(SUM("访问次数")) OVER (PARTITION BY "部门") AS "_占比_" '
            f'FROM {_DS} GROUP BY "部门", "团队"'
        ),
    },
    # 需要对聚合结果再聚合时用 WITH，不要把聚合套在聚合里。
    {
        "question": "各部门平均每个团队的访问次数",
        "sql": (
            'WITH "_汇总_" AS ('
            f'SELECT "部门", "团队", SUM("访问次数") AS "_次数_" FROM {_DS} GROUP BY "部门", "团队"'
            ') SELECT "部门", AVG("_次数_") AS "_均值_" FROM "_汇总_" GROUP BY "部门"'
        ),
    },
    # 与整体比较用标量子查询。
    {
        "question": "访问次数高于所有部门平均水平的部门",
        "sql": (
            'WITH "_汇总_" AS ('
            f'SELECT "部门", SUM("访问次数") AS "_次数_" FROM {_DS} GROUP BY "部门"'
            ') SELECT "部门", "_次数_" FROM "_汇总_" '
            'WHERE "_次数_" > (SELECT AVG("_次数_") FROM "_汇总_")'
        ),
    },
    # 分区取前 N 名：聚合与分区排名写在同一层，外面按名次取。
    {
        "question": "每个部门访问次数最多的团队",
        "sql": (
            'SELECT "部门", "团队", "_次数_" FROM (SELECT "部门", "团队", '
            'SUM("访问次数") AS "_次数_", RANK() OVER '
            '(PARTITION BY "部门" ORDER BY SUM("访问次数") DESC) AS "_名次_" '
            f'FROM {_DS} GROUP BY "部门", "团队") WHERE "_名次_" = 1'
        ),
    },
    # 集合运算：每个分支各自完整、直接 FROM 数据集（分支不能 FROM CTE），投影列一一对应。
    {
        "question": "访问次数超过 1000 且访问时长超过 500 的部门",
        "sql": (
            f'SELECT "部门" FROM {_DS} GROUP BY "部门" HAVING SUM("访问次数") > 1000 '
            f'INTERSECT SELECT "部门" FROM {_DS} GROUP BY "部门" HAVING SUM("访问时长") > 500'
        ),
    },
)
