"""实体名召回合同（2026-08-28 城市/图书馆 r2 后评审）。

r2 把命名修对了（图书馆.名称 → 图书馆名称），准确率仍停在 6/12：5 个失败
共享同一个第二根因——用户说的「图书馆」召不回「图书馆名称」维度，最终 LLM
的最小 schema 里因此没有它，只能丢掉 GROUP BY 返回一个看起来正常的总数。

实测（真实 bge-m3）：cos(图书馆, 图书馆名称)=0.8318 / relevance 0.9159，
超过 0.90 阈值——向量本身够用，但「图书馆」这个 span 从未被查询：
``_embedding_segments`` 是 size=3/step=2 的固定偏移滑窗（移植自上游
BatchMatchStrategy），词能否被切出取决于它在问句里的起始位置奇偶性。

三条确定性修复：并集切分、实体名别名、最小 schema 兜底。
"""

from __future__ import annotations

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.query import mapper as M
from knowflow_analytics.query.parser import _dimension_payload
from knowflow_analytics.semantic.index import SemanticElementType, SemanticIndexEntry


def _entries(phrases: tuple[str, ...]) -> tuple[SemanticIndexEntry, ...]:
    return tuple(
        SemanticIndexEntry(
            id=f"entry:{index}",
            phrase=phrase,
            normalized_phrase=M.normalize_text(phrase),
            element_type=SemanticElementType.DIMENSION,
            element_id=f"dim:{index}",
            dataset_ids=("ds",),
            source="name",
        )
        for index, phrase in enumerate(phrases)
    )


def test_embedding_segments_union_governed_words_with_the_sliding_window() -> None:
    entries = _entries(("图书馆", "图书馆名称", "藏品数量"))

    segments = M._embedding_segments("各图书馆的藏品数量", entries=entries)

    # 受治理真词进入向量查询（滑窗永远切不出「图书馆」——奇数位起始）。
    assert "图书馆" in segments
    assert "藏品数量" in segments
    # 「各图书」与精确命中「图书馆」重叠：它是同一处文本的碎片，不是另一种说法。
    assert "各图书" not in segments


def test_window_fragments_overlapping_an_exact_term_are_dropped() -> None:
    """实机回放（2026-09-02）：「上海有哪些门店，什么时候开业的」。

    词表命中「上海」[0,2) 与「门店」[5,7)；滑窗从偶数位切出「些门店」[4,7)，
    与「门店」重叠。碎片去查向量召回的是「门店数量」这类弱候选，再被升级成
    需要用户确认的指标——一个没问指标的问题因此反复弹澄清卡。
    上游 MapFilter 同样以精确命中为准，剔除落在同一 span 上的模糊命中。
    """
    entries = _entries(("上海", "门店", "门店数量"))

    segments = M._embedding_segments("上海有哪些门店，什么时候开业的", entries=entries)

    assert "门店" in segments
    assert "些门店" not in segments
    assert "上海有" not in segments
    assert "店，什" not in segments
    # 不与任何精确命中重叠的碎片保留：词表外说法仍有兜底。
    assert "有哪些" in segments
    assert "什么时" in segments


def test_embedding_segments_without_entries_keep_the_sliding_window() -> None:
    assert M._embedding_segments("各图书馆的藏品数量") == (
        "各图书",
        "书馆的",
        "的藏品",
        "品数量",
        "量",
    )


def _entity_release() -> SemanticRelease:
    return SemanticRelease(
        id="release_entity",
        project_id="prj",
        spec_hash="entity-v1",
        models=(
            ModelSpec(id="library", name="图书馆", schema_name="s", table="library"),
            ModelSpec(id="city", name="城市", schema_name="s", table="city"),
        ),
        fields=(
            FieldSpec(
                id="library.id",
                model_id="library",
                name="图书馆ID",
                column="词条id",
                kind=FieldKind.IDENTIFIER,
                identifier_type="primary",
            ),
            FieldSpec(
                id="library.name",
                model_id="library",
                name="图书馆名称",
                column="名称",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="library.addr",
                model_id="library",
                name="图书馆地址",
                column="地址",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="city.name",
                model_id="city",
                name="城市名称",
                column="名称",
                kind=FieldKind.DIMENSION,
            ),
        ),
        dimensions=(
            DimensionSpec(
                id="dim:library:name",
                name="图书馆名称",
                model_id="library",
                field_id="library.name",
            ),
            DimensionSpec(
                id="dim:library:addr",
                name="图书馆地址",
                model_id="library",
                field_id="library.addr",
            ),
            DimensionSpec(
                id="dim:city:name", name="城市名称", model_id="city", field_id="city.name"
            ),
        ),
        datasets=(
            DatasetSpec(
                id="library_scope",
                name="图书馆分析",
                model_ids=("library", "city"),
                metric_ids=(),
                dimension_ids=("dim:library:name", "dim:library:addr", "dim:city:name"),
            ),
        ),
        analysis_topic_routes=(
            AnalysisTopicRouteSpec(dataset_id="library_scope", root_model_id="library"),
        ),
    )


def test_schema_carries_the_whole_scope_including_the_entity_name() -> None:
    """Scope 全量送给模型，实体名称维度不再依赖召回是否命中。

    r2 实测：Mapper 拿「图书馆」只命中了地址一类维度，实体名称维度进不了
    最小 schema，模型于是丢掉 GROUP BY 返回一个总数。现在整个 Scope 都在，
    「各图书馆」和「各所属城市」都有维度可用。
    """

    release = _entity_release()
    dataset = release.datasets[0]

    payload = _dimension_payload(release, dataset)

    names = {str(item["name"]) for item in payload}
    assert names == {"图书馆名称", "图书馆地址", "城市名称"}
