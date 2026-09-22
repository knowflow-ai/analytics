"""区间也在用这个值，不是把它丢了。

客户现场截图（2026-09-22）：「2024年1月到3月差旅费总额是多少」拒答
`LLM_S2SQL_GROUNDED_VALUE_REQUIRED`，提示「模型遗漏了 Schema Linking 已确认的精确
维度值约束：「1」（维度「月份」）、「3」（维度「月份」）」。而模型写的是

    WHERE "科目名称" = '销售费用-差旅费' AND "年份" = 2024 AND "月份" BETWEEN 1 AND 3

两个值明明白白写在 BETWEEN 里，完全正确，3/3 稳定被拒——因为这道校验只扫
`=`、`IN`、`RATIO_TO_TOTAL`。

更要命的是它放行了什么：Rule 兜底写的 ``"月份" IN (3, 1)`` 能过校验，语义却是错的
（只有一月和三月，丢了二月）。这道门当时在把系统从正确答案往错答案上推。

判据回到这道校验自己的措辞——它拦的是「模型悄悄**丢掉**了精确命中」，而值的字面量
出现在约束该维度的比较谓词里，它就没被丢。与算子无关。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.contracts import MapMode, MappingResult, MatchMethod, SchemaMatch
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.semantic.index import SemanticElementType


class _Gateway:
    def __init__(self, sql: str) -> None:
        self.payload = {"thought": "按已发布取值过滤", "sql": sql}

    def generate_json(self, **kwargs):
        return self.payload


def _exact_region_value_mapping() -> MappingResult:
    return MappingResult(
        dataset_id="sales_dataset",
        mode=MapMode.STRICT,
        normalized_question="华东净收入",
        matches=(
            SchemaMatch(
                entry_id="entry:region-east",
                dataset_id="sales_dataset",
                element_type=SemanticElementType.DIMENSION_VALUE,
                element_id="region_east",
                phrase="华东",
                detected_text="华东",
                method=MatchMethod.EXACT,
                score=1.0,
                priority=300,
                dimension_id="region",
                raw_value="华东",
            ),
        ),
        config_version="test",
    )


def _parse(sales_release, predicate: str):
    sql = f'SELECT SUM("净收入") FROM "销售经营" WHERE {predicate}'
    return LlmS2SqlParser(_Gateway(sql)).parse(
        question="华东净收入",
        release=sales_release,
        mapping=_exact_region_value_mapping(),
        query_id="range-grounding",
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "\"区域\" BETWEEN '华东' AND '华南'",
        "\"区域\" >= '华东'",
        "\"区域\" <= '华东'",
        "\"区域\" > '华东'",
        "\"区域\" != '华东'",
    ],
)
def test_a_value_written_into_a_comparison_is_not_a_dropped_value(sales_release, predicate) -> None:
    candidate = _parse(sales_release, predicate)

    assert "华东" in candidate.parsed_s2sql


def test_the_upper_bound_of_a_range_counts_too(sales_release) -> None:
    """现场那道题两个值分别落在区间的两端，只认下界照样拒答。"""

    candidate = _parse(sales_release, "\"区域\" BETWEEN '华南' AND '华东'")

    assert "华东" in candidate.parsed_s2sql


def test_a_range_on_another_dimension_does_not_ground_this_value(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _parse(sales_release, "\"渠道\" BETWEEN '华东' AND '华南'")

    assert raised.value.code == "LLM_S2SQL_GROUNDED_VALUE_REQUIRED"


def test_dropping_the_value_entirely_is_still_rejected(sales_release) -> None:
    """放宽的是算子，不是把这道校验拆了。"""

    with pytest.raises(SemanticParsingError) as raised:
        LlmS2SqlParser(_Gateway('SELECT SUM("净收入") FROM "销售经营"')).parse(
            question="华东净收入",
            release=sales_release,
            mapping=_exact_region_value_mapping(),
            query_id="range-grounding-drop",
        )

    assert raised.value.code == "LLM_S2SQL_GROUNDED_VALUE_REQUIRED"
