"""问题里的数字是字面量，不是字典碎片。

现场（2026-09-16）：「账户余额大于 2000 的占比多少」被拒答「不能按你指定的维度安全拆分」。
碎片通道把「2000」切出「20」，和另一张表取值字典里的「20」编辑距离 1.0，被标成精确命中，
一个数字碎片于是拥有了否决事实根的权力。
"""

from __future__ import annotations

from knowflow_analytics.contracts import DimensionValueSpec
from knowflow_analytics.query.contracts import MatchMethod
from knowflow_analytics.query.mapper import MapMode, SemanticMapper, _dictionary_segment_spans
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    SemanticElementType,
    SemanticIndexBuilder,
    SemanticIndexEntry,
)


class _ConstantEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="numeric-test", dimension=1, vectors=tuple((1.0,) for _ in texts)
        )


def _entry(phrase: str, element_type: SemanticElementType, element_id: str) -> SemanticIndexEntry:
    return SemanticIndexEntry(
        id=f"entry-{element_id}",
        phrase=phrase,
        normalized_phrase=phrase.casefold(),
        element_type=element_type,
        element_id=element_id,
        dataset_ids=("sales",),
        source="name",
        priority=300,
        dimension_id="acct" if element_type is SemanticElementType.DIMENSION_VALUE else None,
    )


def _with_numeric_segments(sales_release):
    """给客户分层加几个纯数字取值，模拟整数档位被采进字典。"""

    values = tuple(
        DimensionValueSpec(
            id=f"segment_{value}",
            dimension_id="customer_segment",
            value=value,
            display_name=value,
            aliases=(),
        )
        for value in ("20", "1000", "20000")
    )
    release = sales_release.model_copy(
        update={"dimension_values": (*sales_release.dimension_values, *values)}
    )
    return release, SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)


def _value_matches(mapping):
    return [
        (item.detected_text, item.phrase, item.method)
        for item in mapping.matches
        if item.element_type is SemanticElementType.DIMENSION_VALUE
    ]


def test_scan_never_cuts_into_a_number() -> None:
    entries = (
        _entry("20", SemanticElementType.DIMENSION_VALUE, "v20"),
        _entry("1000", SemanticElementType.DIMENSION_VALUE, "v1000"),
        _entry("账户余额", SemanticElementType.METRIC, "zhye"),
    )

    assert set(_dictionary_segment_spans("账户余额大于 2000 的占比多少", entries)) == {"账户余额"}
    assert set(_dictionary_segment_spans("账户余额大于2000的占比多少", entries)) == {"账户余额"}
    # 数字整体出现时仍是候选
    assert "20" in _dictionary_segment_spans("账户类型是 20 的账户余额", entries)


def test_a_threshold_in_the_question_is_not_a_dictionary_value(sales_release) -> None:
    _release, index = _with_numeric_segments(sales_release)
    question = "净收入大于 2000 的占比多少"

    for mode in (MapMode.STRICT, MapMode.MODERATE, MapMode.ALL):
        mapping = SemanticMapper().map(
            question=question, dataset_id="sales_dataset", index=index, mode=mode
        )
        assert _value_matches(mapping) == [], mode
        exact = [item for item in mapping.matches if item.method is MatchMethod.EXACT]
        assert {item.element_id for item in exact} == {"net_revenue"}, mode


def test_a_number_still_matches_when_it_is_the_whole_token(sales_release) -> None:
    _release, index = _with_numeric_segments(sales_release)

    mapping = SemanticMapper().map(
        question="客户分层是 20 的净收入",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    assert ("20", "20", MatchMethod.EXACT) in _value_matches(mapping)
    assert all(phrase == "20" for _text, phrase, _method in _value_matches(mapping))
