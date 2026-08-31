from __future__ import annotations

from knowflow_analytics.contracts import SemanticQueryType
from knowflow_analytics.query.contracts import (
    MapMode,
    MappingResult,
    MatchMethod,
    ParsedSemanticCandidate,
    SchemaMatch,
)
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic.index import SemanticElementType


def _match(
    *,
    dataset_id: str,
    element_type: SemanticElementType,
    element_id: str,
    method: MatchMethod,
    detected_text: str,
    score: float,
) -> SchemaMatch:
    return SchemaMatch(
        entry_id=f"entry:{dataset_id}:{element_id}",
        dataset_id=dataset_id,
        element_type=element_type,
        element_id=element_id,
        phrase=detected_text,
        detected_text=detected_text,
        method=method,
        score=score,
        priority=250,
    )


def _candidate(
    dataset_id: str,
    *matches: SchemaMatch,
) -> ParsedSemanticCandidate:
    mapping = MappingResult(
        dataset_id=dataset_id,
        mode=MapMode.STRICT,
        normalized_question="测试问题",
        matches=matches,
        config_version="test-mapping-v1",
    )
    return ParsedSemanticCandidate(
        id=f"candidate:{dataset_id}",
        dataset_id=dataset_id,
        parsed_s2sql=f'SELECT "测试字段" FROM "{dataset_id}"',
        corrected_s2sql=f'SELECT "测试字段" FROM "{dataset_id}"',
        query_type=SemanticQueryType.AGGREGATE,
        score=sum(item.score for item in matches),
        map_mode=MapMode.STRICT,
        mapping=mapping,
        parser="rule",
        rationale="测试候选",
    )


def test_unique_exact_metric_scope_excludes_value_only_and_weak_candidates() -> None:
    exact_metric = _candidate(
        "merchant_scope",
        _match(
            dataset_id="merchant_scope",
            element_type=SemanticElementType.METRIC,
            element_id="onboarded_platform_count",
            method=MatchMethod.EXACT,
            detected_text="入驻平台数",
            score=1.0,
        ),
        _match(
            dataset_id="merchant_scope",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="country_us",
            method=MatchMethod.EXACT,
            detected_text="美国",
            score=1.0,
        ),
    )
    value_only = _candidate(
        "coverage_scope",
        _match(
            dataset_id="coverage_scope",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="country_us",
            method=MatchMethod.EXACT,
            detected_text="美国",
            score=1.0,
        ),
    )
    keyword_dimension = _candidate(
        "platform_scope",
        _match(
            dataset_id="platform_scope",
            element_type=SemanticElementType.DIMENSION,
            element_id="platform_name",
            method=MatchMethod.KEYWORD,
            detected_text="平台",
            score=0.5,
        ),
    )
    embedding_metric = _candidate(
        "turnover_scope",
        _match(
            dataset_id="turnover_scope",
            element_type=SemanticElementType.METRIC,
            element_id="annual_sales_ratio",
            method=MatchMethod.EMBEDDING,
            detected_text="占比多少",
            score=0.81,
        ),
    )

    admitted = AnalyticsQueryService._admit_query_scope_candidates(
        (exact_metric, value_only, keyword_dimension, embedding_metric)
    )

    assert admitted == (exact_metric,)


def test_distinct_exact_metric_scopes_still_require_clarification() -> None:
    first = _candidate(
        "merchant_scope",
        _match(
            dataset_id="merchant_scope",
            element_type=SemanticElementType.METRIC,
            element_id="merchant_turnover",
            method=MatchMethod.EXACT,
            detected_text="交易额",
            score=1.0,
        ),
    )
    second = _candidate(
        "platform_scope",
        _match(
            dataset_id="platform_scope",
            element_type=SemanticElementType.METRIC,
            element_id="platform_turnover",
            method=MatchMethod.EXACT,
            detected_text="交易额",
            score=1.0,
        ),
    )

    admitted = AnalyticsQueryService._admit_query_scope_candidates((first, second))

    assert admitted == (first, second)


def test_without_an_exact_metric_all_discovered_scopes_remain() -> None:
    value_only = _candidate(
        "coverage_scope",
        _match(
            dataset_id="coverage_scope",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="country_us",
            method=MatchMethod.EXACT,
            detected_text="美国",
            score=1.0,
        ),
    )
    weak_metric = _candidate(
        "turnover_scope",
        _match(
            dataset_id="turnover_scope",
            element_type=SemanticElementType.METRIC,
            element_id="annual_sales_ratio",
            method=MatchMethod.EMBEDDING,
            detected_text="占比多少",
            score=0.81,
        ),
    )

    admitted = AnalyticsQueryService._admit_query_scope_candidates((value_only, weak_metric))

    assert admitted == (value_only, weak_metric)


def test_multi_exact_metric_clarification_offers_only_metric_bearing_scopes() -> None:
    first_exact = _candidate(
        "merchant_scope",
        _match(
            dataset_id="merchant_scope",
            element_type=SemanticElementType.METRIC,
            element_id="merchant_turnover",
            method=MatchMethod.EXACT,
            detected_text="交易额",
            score=1.0,
        ),
    )
    second_exact = _candidate(
        "platform_scope",
        _match(
            dataset_id="platform_scope",
            element_type=SemanticElementType.METRIC,
            element_id="platform_turnover",
            method=MatchMethod.EXACT,
            detected_text="交易额",
            score=1.0,
        ),
    )
    keyword_bystander = _candidate(
        "coverage_scope",
        _match(
            dataset_id="coverage_scope",
            element_type=SemanticElementType.DIMENSION,
            element_id="turnover_grade",
            method=MatchMethod.KEYWORD,
            detected_text="交易额等级",
            score=0.6,
        ),
    )
    embedding_bystander = _candidate(
        "generic_scope",
        _match(
            dataset_id="generic_scope",
            element_type=SemanticElementType.METRIC,
            element_id="record_count",
            method=MatchMethod.EMBEDDING,
            detected_text="交易额",
            score=0.8,
        ),
    )

    offered = AnalyticsQueryService._clarification_scope_candidates(
        (first_exact, keyword_bystander, second_exact, embedding_bystander)
    )

    assert offered == (first_exact, second_exact)


def test_clarification_without_exact_metrics_keeps_every_discovered_scope() -> None:
    value_only = _candidate(
        "coverage_scope",
        _match(
            dataset_id="coverage_scope",
            element_type=SemanticElementType.DIMENSION_VALUE,
            element_id="country_us",
            method=MatchMethod.EXACT,
            detected_text="美国",
            score=1.0,
        ),
    )
    weak_metric = _candidate(
        "turnover_scope",
        _match(
            dataset_id="turnover_scope",
            element_type=SemanticElementType.METRIC,
            element_id="annual_sales_ratio",
            method=MatchMethod.EMBEDDING,
            detected_text="占比多少",
            score=0.81,
        ),
    )

    offered = AnalyticsQueryService._clarification_scope_candidates((value_only, weak_metric))

    assert offered == (value_only, weak_metric)
