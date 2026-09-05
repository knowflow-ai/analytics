"""「理解问题」完成时带给前端的 chip：只要精确命中，按业务名去重，零 ID。"""

from __future__ import annotations

from knowflow_analytics.query.contracts import (
    MapMode,
    MappingResult,
    MatchMethod,
    SchemaMatch,
    UnderstoodElement,
)
from knowflow_analytics.query.service import (
    UNDERSTOOD_ELEMENTS_LIMIT,
    understood_elements,
)
from knowflow_analytics.semantic.index import SemanticElementType


def _match(
    element_type: SemanticElementType,
    element_id: str,
    *,
    phrase: str = "x",
    method: MatchMethod = MatchMethod.EXACT,
    raw_value: object = None,
) -> SchemaMatch:
    return SchemaMatch(
        entry_id=f"entry:{element_id}:{phrase}",
        dataset_id="sales",
        element_type=element_type,
        element_id=element_id,
        phrase=phrase,
        detected_text=phrase,
        method=method,
        score=1.0,
        priority=1,
        raw_value=raw_value,
    )


def _attempt(*matches: SchemaMatch, dataset_id: str = "sales") -> MappingResult:
    return MappingResult(
        dataset_id=dataset_id,
        mode=MapMode.STRICT,
        normalized_question="q",
        matches=tuple(matches),
        config_version="v1",
    )


NAMES = {"m_rev": "销售金额", "m_net": "净收入"}
DIMS = {"d_store": "门店名称", "d_city": "所在城市"}


def test_exact_hits_become_chips_with_catalog_names_not_dictionary_phrases():
    # 命中的是别名「销售额」，chip 也要叫「销售金额」——和跑完后理解行上是同一个词。
    elements = understood_elements(
        [
            _attempt(
                _match(SemanticElementType.METRIC, "m_rev", phrase="销售额"),
                _match(SemanticElementType.DIMENSION, "d_store", phrase="门店"),
                _match(
                    SemanticElementType.DIMENSION_VALUE,
                    "v_sh",
                    phrase="上海",
                    raw_value="上海",
                ),
            )
        ],
        metric_names=NAMES,
        dimension_names=DIMS,
    )

    assert elements == (
        UnderstoodElement(kind="metric", label="销售金额"),
        UnderstoodElement(kind="dimension", label="门店名称"),
        UnderstoodElement(kind="dimension_value", label="上海"),
    )


def test_weak_recalls_are_hints_for_the_llm_not_things_the_user_understood():
    # keyword / embedding 只是给最终 LLM 的提示；摆给用户再消失比不显示更糟。
    elements = understood_elements(
        [
            _attempt(
                _match(SemanticElementType.METRIC, "m_rev", method=MatchMethod.KEYWORD),
                _match(SemanticElementType.METRIC, "m_net", method=MatchMethod.EMBEDDING),
                _match(SemanticElementType.METRIC, "m_net", method=MatchMethod.CONFIRMED),
            )
        ],
        metric_names=NAMES,
        dimension_names=DIMS,
    )

    assert [item.label for item in elements] == ["净收入"]


def test_same_member_hit_in_several_scopes_is_one_chip_and_kinds_are_ordered():
    # 全局检索会让同一个成员在每个候选作用域各命中一次；chip 只出一颗。
    elements = understood_elements(
        [
            _attempt(
                _match(SemanticElementType.DIMENSION, "d_city"),
                _match(SemanticElementType.METRIC, "m_rev"),
                dataset_id="orders",
            ),
            _attempt(
                _match(SemanticElementType.METRIC, "m_rev"),
                _match(SemanticElementType.DIMENSION, "d_city"),
                dataset_id="stores",
            ),
        ],
        metric_names=NAMES,
        dimension_names=DIMS,
    )

    assert [(item.kind, item.label) for item in elements] == [
        ("metric", "销售金额"),
        ("dimension", "所在城市"),
    ]


def test_unknown_ids_terms_and_datasets_never_leak():
    # 目录里查不到名字就不出 chip（绝不把 ID 当名字显示）；术语与作用域不是成员。
    elements = understood_elements(
        [
            _attempt(
                _match(SemanticElementType.METRIC, "m_missing"),
                _match(SemanticElementType.TERM, "t_1", phrase="大区"),
                _match(SemanticElementType.DATASET, "sales", phrase="销售分析"),
            )
        ],
        metric_names=NAMES,
        dimension_names=DIMS,
    )

    assert elements == ()


def test_chip_count_is_capped():
    matches = [
        _match(SemanticElementType.METRIC, f"m{i}") for i in range(UNDERSTOOD_ELEMENTS_LIMIT + 5)
    ]
    names = {f"m{i}": f"指标{i}" for i in range(UNDERSTOOD_ELEMENTS_LIMIT + 5)}

    elements = understood_elements([_attempt(*matches)], metric_names=names, dimension_names={})

    assert len(elements) == UNDERSTOOD_ELEMENTS_LIMIT
