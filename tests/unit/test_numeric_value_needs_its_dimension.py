"""问句里的一个裸数字是阈值，不是某个维度的取值。

现场（2026-09-17）：「账户余额大于 1000 的人占比情况」被拒，理由是「该指标不能按所选
维度安全分析」。追下去是「1000」精确命中了另一张表的 `账户代码 = 1000`——那列其实是
评分调整值（15 个取值：-30、-20、-10、0、20…、1000）。命中之后这个维度成了必须满足的
精确条件，而指标锚定的是另一个事实根，两表之间一条关系都没有，于是整条路由 fail-closed。

判据只看问句本身：纯数字的维度取值，只有在**该维度自己也在问句里被点名**时才召回。
「账户代码是 1000」召回，「大于 1000」不召回。文本取值不受影响——「上海」是强信号，
数字不是：任何问句里的任何数字都可能撞上某张表的某个编码。

这与 2026-09-16 那条「数字片段只认整体相等」是同一类问题的两半：那次修的是
「2000」被切出「20」去撞取值，这次修的是整体相等时撞上了取值。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.semantic.index import (
    IndexState,
    SemanticElementType,
    SemanticIndexEntry,
    SemanticIndexSnapshot,
)


def _entry(**over) -> SemanticIndexEntry:
    base = {
        "dataset_ids": ("scores",),
        "source": "value",
        "priority": 300,
    }
    return SemanticIndexEntry(**{**base, **over})


@pytest.fixture
def index() -> SemanticIndexSnapshot:
    entries = (
        _entry(
            id="entry-acct-code",
            phrase="账户代码",
            normalized_phrase="账户代码",
            element_type=SemanticElementType.DIMENSION,
            element_id="acct_code",
            source="name",
        ),
        _entry(
            id="entry-acct-1000",
            phrase="1000",
            normalized_phrase="1000",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="acct_code_1000",
            dimension_id="acct_code",
            raw_value=1000,
        ),
        _entry(
            id="entry-city",
            phrase="所在城市",
            normalized_phrase="所在城市",
            element_type=SemanticElementType.DIMENSION,
            element_id="city",
            source="name",
        ),
        _entry(
            id="entry-city-shanghai",
            phrase="上海",
            normalized_phrase="上海",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="city_shanghai",
            dimension_id="city",
            raw_value="上海",
        ),
    )
    return SemanticIndexSnapshot(
        id="idx-numeric-value",
        release_spec_hash="spec-numeric-value",
        content_hash="hash-numeric-value",
        state=IndexState.READY,
        embedding_model_id="test",
        vector_dimension=1,
        entries=entries,
        vectors=tuple((1.0,) for _ in entries),
    )


def _value_matches(index: SemanticIndexSnapshot, question: str) -> set[str]:
    result = SemanticMapper().map(
        question=question,
        dataset_id="scores",
        index=index,
        mode=MapMode.ALL,
    )
    return {
        item.element_id
        for item in result.matches
        if item.element_type is SemanticElementType.DIMENSION_VALUE
    }


def test_a_threshold_is_not_a_dimension_value(index: SemanticIndexSnapshot) -> None:
    """「大于 1000」里的 1000 是阈值。撞上某张表的编码不是证据，是巧合。"""

    assert _value_matches(index, "账户余额大于 1000 的人占比情况") == set()


def test_naming_the_dimension_brings_the_value_back(index: SemanticIndexSnapshot) -> None:
    """说了「账户代码」，1000 就有了出处。"""

    assert "acct_code_1000" in _value_matches(index, "账户代码是 1000 的记录有多少条")


def test_a_text_value_still_needs_no_introduction(index: SemanticIndexSnapshot) -> None:
    """「上海」本身就是强信号，不必先说「所在城市」。"""

    assert "city_shanghai" in _value_matches(index, "上海有哪些账户")
