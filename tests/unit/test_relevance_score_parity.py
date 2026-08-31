"""向量相似度阈值必须与上游同一口径。

上游 langchain4j 侧 similarity = RelevanceScore.fromCosineSimilarity(cos)
= (cos + 1) / 2（InMemoryEmbeddingStore.java:160、EmbeddingServiceImpl.java:157），
阈值 s2.mapper.embedding.threshold 默认 0.9 ⇒ 实际放行 cosine >= 0.8。

我们计算的是原始余弦，却直接拿 0.90 去比，等于门槛整整高了 0.1 cosine——
业务同义词（"销售额" vs "实付金额"）更容易被卡在召回之外，正是 CLAUDE.md
记录的映射失败故障模式。exemplar 的 0.989 同源，导致「极相似」分支基本走不到。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MappingConfig
from knowflow_analytics.query.exemplars import (
    EXEMPLAR_EXACT_SIMILARITY,
    relevance_score,
)


@pytest.mark.parametrize(
    ("cosine", "expected"),
    [(1.0, 1.0), (0.8, 0.9), (0.0, 0.5), (-1.0, 0.0)],
)
def test_relevance_score_matches_upstream_formula(cosine: float, expected: float) -> None:
    assert relevance_score(cosine) == pytest.approx(expected)


def test_embedding_threshold_admits_upstream_cosine_floor() -> None:
    """上游 0.9 阈值放行 cosine >= 0.8;我们必须放行同一批。"""

    threshold = MappingConfig().embedding_similarity
    assert relevance_score(0.80) >= threshold
    assert relevance_score(0.79) < threshold


def test_exemplar_exact_threshold_admits_upstream_cosine_floor() -> None:
    """上游 0.989 相当于 cosine > 0.978,不是 cosine > 0.989。"""

    # 0.978 恰好映射到阈值本身,上游用严格大于,故取两侧明确值
    assert relevance_score(0.977) < EXEMPLAR_EXACT_SIMILARITY
    assert relevance_score(0.98) > EXEMPLAR_EXACT_SIMILARITY
    # 关键对比:原始余弦口径下 0.98 根本达不到 0.989,旧实现因此几乎永不判「极相似」
    assert EXEMPLAR_EXACT_SIMILARITY > 0.98
