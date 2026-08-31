"""前后缀召回在截断前的排序方向必须与上游一致。

上游 SearchService.java:63/98 的比较器是
``(a, b) -> -(b.getName().length() - a.getName().length())``,展开即
``a.len - b.len``——按名称长度**升序,最短优先**,再 .limit(detection_max_size)。
短名与探测片段的编辑距离最近、相似度最高。

我们此前是最长优先,等于在相似度过滤**之前**先丢掉最可能正确的候选。

影响范围的实测结论(不要高估):主名指标/维度另有一条不受截断限制的子串召回
路径(mapper.py 的 detected_text not in entry.normalized_phrase),所以它们不会
因此丢失;真正受影响的是别名与维度值条目。
"""

from __future__ import annotations

from knowflow_analytics.query.mapper import recall_order_key


def test_shortest_phrase_sorts_first() -> None:
    phrases = ["区域细分维度扩展", "区域名", "区域细分"]
    assert sorted(phrases, key=recall_order_key) == ["区域名", "区域细分", "区域细分维度扩展"]


def test_truncation_keeps_the_closest_candidates() -> None:
    """截断窗口内应留下最短的那些,而不是最长的那些。"""

    limit = 3
    phrases = ["区域名"] + [f"区域{'扩展' * (i + 1)}" for i in range(5)]
    kept = sorted(phrases, key=recall_order_key)[:limit]
    assert "区域名" in kept
    assert "区域扩展扩展扩展扩展扩展" not in kept


def test_same_length_falls_back_to_lexical_order() -> None:
    """同长度按字典序稳定化,避免存储顺序变化导致快照漂移;不引入语义偏好。"""

    assert sorted(["乙区域", "甲区域"], key=recall_order_key) == ["乙区域", "甲区域"]
