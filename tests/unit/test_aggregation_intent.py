from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    MetricSpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.query.aggregation import (
    RuleAggregateType,
    aggregation_grammar_version,
    parse_aggregation_intent,
)
from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import RuleS2SqlParser
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder


class _ConstantEmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="aggregation-test",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


def test_aggregation_operator_applies_to_the_rule_matched_metric() -> None:
    release, index = _capital_release()
    question = "注册资本最大为多少"
    mapping = SemanticMapper().map(
        question=question,
        dataset_id="company_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question=question,
        release=release,
        mapping=mapping,
    )

    assert candidate is not None
    assert 'MAX("总注册资本")' in candidate.parsed_s2sql


def test_explicit_governed_metric_alias_selects_scalar_max() -> None:
    release, index = _capital_release()
    question = "最大注册资本是多少"
    mapping = SemanticMapper().map(
        question=question,
        dataset_id="company_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question=question,
        release=release,
        mapping=mapping,
    )

    assert candidate is not None
    assert 'MAX("最高注册资本")' in candidate.parsed_s2sql
    assert "GROUP BY" not in candidate.parsed_s2sql
    assert "ORDER BY" not in candidate.parsed_s2sql
    assert "LIMIT" not in candidate.parsed_s2sql


def test_explicit_entity_selector_remains_top_one_ranking() -> None:
    release, index = _capital_release()
    question = "哪个区域总注册资本最高"
    mapping = SemanticMapper().map(
        question=question,
        dataset_id="company_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question=question,
        release=release,
        mapping=mapping,
    )

    assert candidate is not None
    assert 'SELECT "区域", MAX("总注册资本")' in candidate.parsed_s2sql
    assert 'GROUP BY "区域"' in candidate.parsed_s2sql
    assert 'ORDER BY "总注册资本" DESC' in candidate.parsed_s2sql
    assert "LIMIT 100" in candidate.parsed_s2sql


def test_grouped_extremum_is_not_silently_truncated_to_one_row() -> None:
    release, index = _capital_release()
    question = "各区域的最大注册资本是多少"
    mapping = SemanticMapper().map(
        question=question,
        dataset_id="company_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question=question,
        release=release,
        mapping=mapping,
    )

    assert candidate is not None
    assert 'SELECT "区域", MAX("最高注册资本")' in candidate.parsed_s2sql
    assert 'GROUP BY "区域"' in candidate.parsed_s2sql
    assert 'ORDER BY "最高注册资本" DESC' in candidate.parsed_s2sql
    assert "LIMIT 100" in candidate.parsed_s2sql


def test_scalar_extrema_is_an_aggregation_override() -> None:
    intent = parse_aggregation_intent("覆盖省份最多为多少")

    assert intent.aggregation is Aggregation.MAX


def test_conflicting_ranking_and_average_cues_choose_one_max_count_type() -> None:
    intent = parse_aggregation_intent("平均收入最高的区域 Top 1")

    assert intent.aggregate_type in {
        RuleAggregateType.MAX,
        RuleAggregateType.AVG,
        RuleAggregateType.TOPN,
    }
    assert intent.matched_phrase


def test_conflicting_global_aggregation_cues_do_not_create_a_clarification() -> None:
    intent = parse_aggregation_intent("收入最大值和平均值")

    assert intent.aggregate_type in {RuleAggregateType.MAX, RuleAggregateType.AVG}
    assert intent.aggregation in {Aggregation.MAX, Aggregation.AVG}


@pytest.mark.parametrize(
    ("question", "aggregation"),
    (("订单总数", Aggregation.COUNT), ("访客 UV", Aggregation.COUNT_DISTINCT)),
)
def test_count_vocabulary_is_exact(
    question: str,
    aggregation: Aggregation,
) -> None:
    assert parse_aggregation_intent(question).aggregation is aggregation


def test_bare_quantity_does_not_expand_count_vocabulary() -> None:
    assert parse_aggregation_intent("美容医生数量大于1万").aggregation is None


def test_aggregation_grammar_is_versioned() -> None:
    assert aggregation_grammar_version() == "knowflow-aggregate-type-v1"


def test_rule_candidate_survives_cross_family_aggregation_ties() -> None:
    release, index = _capital_release()
    question = "平均注册资本最高"
    mapping = SemanticMapper().map(
        question=question,
        dataset_id="company_dataset",
        index=index,
        mode=MapMode.STRICT,
    )

    candidate = RuleS2SqlParser().parse(
        question=question,
        release=release,
        mapping=mapping,
    )

    assert candidate is not None
    assert candidate.query_type.value == "aggregate"
    assert candidate.parsed_s2sql.count("(") == 1


@pytest.mark.parametrize(
    ("question", "aggregate_type", "phrase"),
    (
        ("访问次数 topN", RuleAggregateType.TOPN, "top"),
        ("查看访问明细", RuleAggregateType.NONE, "明细"),
    ),
)
def test_non_executable_upstream_aggregate_types_are_preserved(
    question: str,
    aggregate_type: RuleAggregateType,
    phrase: str,
) -> None:
    """Parity: AggregateTypeParser keeps TOPN/NONE even outside AggOperator."""

    intent = parse_aggregation_intent(question)

    assert intent.aggregate_type is aggregate_type
    assert intent.matched_phrase == phrase
    assert intent.aggregation is None


def _capital_release() -> tuple[SemanticRelease, object]:
    release = SemanticRelease(
        id="capital_release",
        project_id="capital_project",
        spec_hash="capital-fixture",
        models=(
            ModelSpec(
                id="company",
                name="企业",
                schema_name="analytics",
                table="company",
            ),
        ),
        fields=(
            FieldSpec(
                id="company.region",
                model_id="company",
                name="区域",
                column="区域",
                kind=FieldKind.DIMENSION,
            ),
            FieldSpec(
                id="company.capital",
                model_id="company",
                name="注册资本",
                column="注册资本",
                data_type="numeric",
                kind=FieldKind.MEASURE,
                aliases=("注册资金",),
            ),
        ),
        dimensions=(
            DimensionSpec(
                id="region",
                name="区域",
                model_id="company",
                field_id="company.region",
            ),
            DimensionSpec(
                id="capital_value",
                name="注册资本取值",
                model_id="company",
                field_id="company.capital",
            ),
        ),
        metrics=(
            MetricSpec(
                id="capital_sum",
                name="总注册资本",
                model_id="company",
                field_id="company.capital",
                aggregation=Aggregation.SUM,
                aliases=("注册资本", "注册资本总和"),
            ),
            MetricSpec(
                id="capital_max",
                name="最高注册资本",
                model_id="company",
                field_id="company.capital",
                aggregation=Aggregation.MAX,
                aliases=("最大注册资本", "注册资本最大值"),
            ),
            MetricSpec(
                id="capital_min",
                name="最低注册资本",
                model_id="company",
                field_id="company.capital",
                aggregation=Aggregation.MIN,
                aliases=("最小注册资本", "注册资本最低值"),
            ),
            MetricSpec(
                id="capital_avg",
                name="平均注册资本",
                model_id="company",
                field_id="company.capital",
                aggregation=Aggregation.AVG,
                aliases=("注册资本平均值",),
            ),
        ),
        datasets=(
            DatasetSpec(
                id="company_dataset",
                name="企业分析",
                model_ids=("company",),
                metric_ids=("capital_sum", "capital_max", "capital_min", "capital_avg"),
                dimension_ids=("region", "capital_value"),
            ),
        ),
    )
    return release, SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
