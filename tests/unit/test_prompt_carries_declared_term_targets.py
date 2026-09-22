"""业务词典里声明的「这个说法指哪些成员」要进 prompt。

上游 `TermDescMapper` 只重映射术语的**描述文本**，直接的 term→成员 链接被当作治理
元数据、刻意不用（`mapper.py` 的注释写明是这条对齐）。我们照抄了它，但我们自己的产品
**强制**用户填这些链接——「新写入的 Term 必须关联至少一个受治理 Metric 或 Dimension」。
于是成了"用户填了、系统不读"。

实测代价（D005）：问「2024年2月招待费是多少」，词典写着「业务招待费」→ 科目名称 +
借方金额，模型却一个科目条件都没写，把当月全部借方加了起来（9500 = 1500 办公费 +
8000 招待费，真值 8000）。行比对靠数字对不上侥幸抓到；若当月只有招待费一笔，它会
静默通过。

这里给的是**声明**，不是合成的匹配证据——上游避免的是后者。
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime

from knowflow_analytics.contracts import TermSpec
from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.semantic.index import SemanticIndexBuilder
from tests.conftest import DeterministicEmbeddingGateway


def _terms(release, question: str, **kwargs) -> list[dict[str, object]]:
    # 索引必须按**带术语的** release 重建：fixture 的索引里没有这个 Term，
    # mapper 就命中不了它，`mapped_term_ids` 为空，测到的就不是这条机制。
    index = SemanticIndexBuilder(DeterministicEmbeddingGateway()).build(release)
    dataset = release.datasets[0]
    mapping = SemanticMapper().map(
        question=question,
        dataset_id=dataset.id,
        index=index,
        mode=MapMode.ALL,
    )
    content = LlmS2SqlParser._messages(
        question,
        release,
        dataset,
        mapping,
        now=datetime(2026, 8, 20, tzinfo=UTC),
        **kwargs,
    )[1]["content"]
    line = next((item for item in content.splitlines() if item.startswith("domain_terms=")), None)
    if line is None:
        return []
    return ast.literal_eval(line.removeprefix("domain_terms="))


def _with_term(release, *, metric_ids=(), dimension_ids=()):
    term = TermSpec(
        id="term_gross",
        name="业绩",
        description="门店当期的销售表现",
        aliases=("营收",),
        metric_ids=tuple(metric_ids),
        dimension_ids=tuple(dimension_ids),
    )
    return release.model_copy(update={"terms": (term,)})


def test_a_matched_term_carries_what_the_modeler_declared(sales_release):
    metric_id = sales_release.metrics[0].id
    release = _with_term(sales_release, metric_ids=(metric_id,))

    entries = _terms(release, "业绩")

    declared = next(item for item in entries if item["name"] == "业绩")
    assert declared["means"] == [sales_release.metrics[0].name]


def test_the_key_disappears_when_nothing_survives_the_filters(sales_release):
    """声明的成员全被过滤掉时，`means` 这个键整个不出现，而不是给一个空数组。

    prompt 的每个字都要有它的理由：一个空数组既不带信息，又要占位置，还会让模型
    以为"这个词有声明但是空的"。
    （不测"术语一个链接都不声明"——产品强制至少关联一个受治理成员，那种术语造不出来。）
    """

    metric_id = sales_release.metrics[0].id
    release = _with_term(sales_release, metric_ids=(metric_id,))
    visible = frozenset(
        item.id for item in (*release.metrics, *release.dimensions) if item.id != metric_id
    )

    entries = _terms(release, "业绩", visible_element_ids=visible)

    declared = next(item for item in entries if item["name"] == "业绩")
    assert "means" not in declared


def test_a_member_the_actor_cannot_see_never_reaches_the_prompt(sales_release):
    """权限与成员归属是**两道**过滤，符号表只管后者。

    只用 `_nameable`（符号表叫得出名字）会把当前用户无权访问的成员名摆给模型。
    这个会话里已经在 `_render_scope_catalog` 上踩过一次同样的漏。
    """

    metric_id = sales_release.metrics[0].id
    release = _with_term(sales_release, metric_ids=(metric_id,))
    visible = frozenset(
        item.id
        for item in (*release.metrics, *release.dimensions)
        if item.id != metric_id
    )

    entries = _terms(release, "业绩", visible_element_ids=visible)

    for item in entries:
        assert sales_release.metrics[0].name not in item.get("means", [])
