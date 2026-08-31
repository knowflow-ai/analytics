from __future__ import annotations

from knowflow_analytics.query.hanlp import HanlpCustomDictionary
from knowflow_analytics.semantic.index import SemanticElementType, SemanticIndexEntry


def _entry(
    *,
    phrase: str,
    element_id: str,
    element_type: SemanticElementType,
) -> SemanticIndexEntry:
    return SemanticIndexEntry(
        id=f"entry:{element_id}",
        phrase=phrase,
        normalized_phrase=phrase.casefold().replace(" ", ""),
        element_type=element_type,
        element_id=element_id,
        dataset_ids=("sales_dataset",),
        source="name",
        priority=300,
    )


def test_forced_custom_dictionary_keeps_longest_registered_word_at_one_offset() -> None:
    dictionary = HanlpCustomDictionary(
        (
            _entry(
                phrase="净收入",
                element_id="net_revenue",
                element_type=SemanticElementType.METRIC,
            ),
            _entry(
                phrase="收入",
                element_id="revenue",
                element_type=SemanticElementType.METRIC,
            ),
        )
    )

    terms = dictionary.segment("净收入趋势")

    assert [(item.word, item.offset, item.length) for item in terms] == [("净收入", 0, 3)]
    assert terms[0].frequency == 100_000
    assert terms[0].natures == ("sales_dataset:metric:net_revenue",)


def test_custom_dictionary_preserves_original_offsets_and_ascii_boundaries() -> None:
    dictionary = HanlpCustomDictionary(
        (
            _entry(
                phrase="GMV",
                element_id="gmv",
                element_type=SemanticElementType.METRIC,
            ),
        )
    )

    terms = dictionary.segment("本月 GMV 和 notgmv")

    assert [(item.word, item.offset) for item in terms] == [("GMV", 3)]


def test_same_registered_word_retains_all_governed_natures() -> None:
    dictionary = HanlpCustomDictionary(
        (
            _entry(
                phrase="金额",
                element_id="amount_metric",
                element_type=SemanticElementType.METRIC,
            ),
            _entry(
                phrase="金额",
                element_id="amount_dimension",
                element_type=SemanticElementType.DIMENSION,
            ),
        )
    )

    terms = dictionary.segment("金额")

    assert terms[0].natures == (
        "sales_dataset:dimension:amount_dimension",
        "sales_dataset:metric:amount_metric",
    )
