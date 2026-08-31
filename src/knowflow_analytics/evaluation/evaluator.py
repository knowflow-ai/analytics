from __future__ import annotations

import math
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from knowflow_analytics.evaluation.contracts import (
    EvaluationCaseResult,
    EvaluationReport,
    EvaluationSlice,
    ExpectedFilter,
    GoldenCase,
    GoldenSuite,
)
from knowflow_analytics.query.contracts import (
    ClarificationQueryResponse,
    CompletedQueryResponse,
    FailedQueryResponse,
    QueryRequest,
    QueryState,
)
from knowflow_analytics.query.service import AnalyticsQueryService


class GoldenEvaluator:
    def __init__(
        self, query_service: AnalyticsQueryService, *, actor_id: str | None = None
    ) -> None:
        self._query_service = query_service
        # 商业版所有模型调用以 actor 为租户,缺租户一律拒绝。评测问数不带 actor
        # 时,试跑里刚答对的问题在这里全部 FAILED——两条链路必须同一上下文。
        self._actor_id = actor_id

    def evaluate(
        self,
        suite: GoldenSuite,
        *,
        required_accuracy: float = 1.0,
        concurrency: int = 1,
    ) -> EvaluationReport:
        if not 1 <= concurrency <= 32:
            raise ValueError("evaluation concurrency must be between 1 and 32")
        if concurrency == 1 or len(suite.cases) == 1:
            evaluated = tuple(self._evaluate_case(suite, item) for item in suite.cases)
        else:
            with ThreadPoolExecutor(
                max_workers=min(concurrency, len(suite.cases)),
                thread_name_prefix="analytics-eval",
            ) as executor:
                # executor.map preserves suite order even though cases finish out
                # of order. Each query still owns an independent Query ID/state.
                evaluated = tuple(
                    executor.map(
                        lambda item: self._evaluate_case(suite, item),
                        suite.cases,
                    )
                )
        results = tuple(item[0] for item in evaluated)
        responses = tuple(item[1] for item in evaluated)
        passed = sum(item.passed for item in results)
        accuracy = passed / len(results)
        first = responses[0]
        summary = _summarize(suite, results)
        return EvaluationReport(
            id=f"eval_{uuid.uuid4().hex}",
            suite_id=suite.id,
            project_id=suite.project_id,
            release_id=first.release_id,
            spec_hash=first.spec_hash,
            index_snapshot_id=first.index_snapshot_id,
            total=len(results),
            passed=passed,
            accuracy=accuracy,
            required_accuracy=required_accuracy,
            gate_passed=accuracy >= required_accuracy,
            results=results,
            **summary,
        )

    def _evaluate_case(
        self, suite: GoldenSuite, case: GoldenCase
    ) -> tuple[EvaluationCaseResult, Any]:
        response = self._query_service.query(
            QueryRequest(
                project_id=suite.project_id,
                question=case.question,
                dataset_ids=case.dataset_ids,
            ),
            now=suite.fixed_now,
            actor_id=self._actor_id,
        )
        context = _evaluation_context(case, response)
        if response.state is not case.expected_state:
            # 只说 got FAILED 等于没说:把底层错误码与信息带给建模者,
            # 否则界面上只能看到挂了、看不到为什么。
            detail = ""
            if isinstance(response, FailedQueryResponse):
                detail = f" ({response.error.code}: {response.error.message})"
            return EvaluationCaseResult(
                case_id=case.id,
                passed=False,
                tags=case.tags,
                failure_stage="state",
                message=f"expected {case.expected_state}, got {response.state}{detail}",
                **context,
            ), response
        if isinstance(response, FailedQueryResponse):
            passed = response.error.code == case.expected_error_code
            return EvaluationCaseResult(
                case_id=case.id,
                passed=passed,
                tags=case.tags,
                failure_stage=None if passed else "error",
                message="" if passed else f"unexpected error code {response.error.code}",
                **context,
            ), response
        if not isinstance(response, CompletedQueryResponse):
            return EvaluationCaseResult(
                case_id=case.id,
                passed=True,
                tags=case.tags,
                **context,
            ), response
        query = response.semantic_query
        projection_matches = not (
            (case.expected_dataset_id is not None and query.dataset_id != case.expected_dataset_id)
            or query.query_type is not case.expected_query_type
            or not _projection_equal(query.metric_ids, case.expected_metric_ids)
            or not _projection_equal(query.dimension_ids, case.expected_dimension_ids)
            or _aggregation_signature(query.aggregation_overrides)
            != _aggregation_signature(case.expected_aggregation_overrides)
        )
        # 只有 top-N(expected_limit 存在)时排序才决定结果内容,必须严格;
        # 无 limit 的 ORDER BY 是呈现装饰,LLM 时有时无(2026-08-26 真实重放实测),
        # 比较它只会把非语义波动固化成假失败。expected_order_by is None 仍表示
        # 用例作者根本没表达排序期望,一并跳过。
        modifiers_match = (
            case.expected_order_by is None
            or case.expected_limit is None
            or (
                _order_signature(query.order_by) == _order_signature(case.expected_order_by)
                and query.limit == case.expected_limit
            )
        )
        actual_filters = tuple(
            (item.dimension_id, item.operator, item.value) for item in query.filters
        )
        expected_filters = tuple(
            (item.dimension_id, item.operator, item.value) for item in case.expected_filters
        )
        filters_match = _filters_equal(actual_filters, expected_filters)
        measure_filters_match = _filters_equal(
            tuple((item.metric_id, item.operator, item.value) for item in query.measure_filters),
            tuple(
                (item.metric_id, item.operator, item.value)
                for item in case.expected_measure_filters
            ),
        )
        metric_filters_match = _filters_equal(
            tuple((item.metric_id, item.operator, item.value) for item in query.metric_filters),
            tuple(
                (item.metric_id, item.operator, item.value) for item in case.expected_metric_filters
            ),
        )
        result_matches = _completed_result_matches(
            response=response,
            case=case,
            projection_matches=projection_matches,
        )
        if not projection_matches:
            return EvaluationCaseResult(
                case_id=case.id,
                passed=False,
                tags=case.tags,
                failure_stage="semantic",
                message=_with_detail("semantic projection differs", _projection_diff(case, query)),
                result_matches_expected=result_matches,
                **context,
            ), response
        if not (filters_match and measure_filters_match and metric_filters_match):
            return EvaluationCaseResult(
                case_id=case.id,
                passed=False,
                tags=case.tags,
                failure_stage="semantic",
                message=_with_detail("semantic filters differ", _filters_diff(case, query)),
                result_matches_expected=result_matches,
                **context,
            ), response
        if not modifiers_match:
            return EvaluationCaseResult(
                case_id=case.id,
                passed=False,
                tags=case.tags,
                failure_stage="semantic",
                message=_with_detail(
                    "semantic order or limit differs", _modifiers_diff(case, query)
                ),
                result_matches_expected=result_matches,
                **context,
            ), response
        if result_matches is not True:
            return EvaluationCaseResult(
                case_id=case.id,
                passed=False,
                tags=case.tags,
                failure_stage="result",
                message=_with_detail(
                    "query result differs",
                    _result_diff(
                        response=response,
                        case=case,
                        projection_matches=projection_matches,
                    ),
                ),
                result_matches_expected=result_matches,
                **context,
            ), response
        return EvaluationCaseResult(
            case_id=case.id,
            passed=True,
            tags=case.tags,
            result_matches_expected=True,
            **context,
        ), response


def _evaluation_context(case: GoldenCase, response: Any) -> dict[str, Any]:
    query = response.semantic_query if isinstance(response, CompletedQueryResponse) else None
    return {
        "expected_state": case.expected_state,
        "expected_dataset_id": case.expected_dataset_id,
        "expected_query_type": case.expected_query_type,
        "expected_metric_ids": case.expected_metric_ids,
        "expected_aggregation_overrides": case.expected_aggregation_overrides,
        "expected_dimension_ids": case.expected_dimension_ids,
        "expected_filters": case.expected_filters,
        "expected_measure_filters": case.expected_measure_filters,
        "expected_metric_filters": case.expected_metric_filters,
        "expected_order_by": case.expected_order_by,
        "expected_limit": case.expected_limit,
        "actual_state": response.state,
        "actual_dataset_id": query.dataset_id if query is not None else None,
        "actual_query_type": query.query_type if query is not None else None,
        "actual_metric_ids": query.metric_ids if query is not None else (),
        "actual_aggregation_overrides": (query.aggregation_overrides if query is not None else ()),
        "actual_dimension_ids": query.dimension_ids if query is not None else (),
        "actual_filters": (
            tuple(
                ExpectedFilter(
                    dimension_id=item.dimension_id,
                    operator=item.operator,
                    value=item.value,
                )
                for item in query.filters
            )
            if query is not None
            else ()
        ),
        "actual_measure_filters": query.measure_filters if query is not None else (),
        "actual_metric_filters": query.metric_filters if query is not None else (),
        "actual_order_by": query.order_by if query is not None else (),
        "actual_limit": query.limit if query is not None else None,
        "actual_columns": response.data.columns if query is not None else (),
        "actual_error_code": (
            response.error.code if isinstance(response, FailedQueryResponse) else None
        ),
        "actual_clarification_options": (
            tuple(f"{item.label}|{item.candidate_id}" for item in response.options)
            if isinstance(response, ClarificationQueryResponse)
            else ()
        ),
        "actual_trace": response.trace,
    }


def _with_detail(summary: str, lines: list[str]) -> str:
    """失败原因必须指名道姓:期望什么、实际什么。一句机器码级别的摘要
    (semantic projection differs)对建模者等于没说(2026-08-26 用户实测)。"""

    if not lines:
        return summary
    return summary + "\n" + "\n".join(f"· {line}" for line in lines)


def _fmt_ids(ids: tuple[str, ...]) -> str:
    return "、".join(ids) if ids else "无"


def _multiset_diff(label: str, expected: tuple[str, ...], actual: tuple[str, ...]) -> str | None:
    if sorted(actual) == sorted(expected):
        return None
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    parts = []
    if missing:
        parts.append(f"缺少 {_fmt_ids(tuple(missing))}")
    if extra:
        parts.append(f"多出 {_fmt_ids(tuple(extra))}")
    if not parts:
        parts.append(f"期望 {_fmt_ids(expected)} → 实际 {_fmt_ids(actual)}")
    return f"{label}: " + ";".join(parts)


def _projection_diff(case: GoldenCase, query: Any) -> list[str]:
    lines: list[str] = []
    if case.expected_dataset_id is not None and query.dataset_id != case.expected_dataset_id:
        lines.append(f"主题: 期望 {case.expected_dataset_id} → 实际 {query.dataset_id}")
    if query.query_type is not case.expected_query_type:
        lines.append(
            f"查询类型: 期望 {case.expected_query_type.value} → 实际 {query.query_type.value}"
        )
    for label, expected, actual in (
        ("指标", case.expected_metric_ids, query.metric_ids),
        ("维度", case.expected_dimension_ids, query.dimension_ids),
    ):
        diff = _multiset_diff(label, expected, actual)
        if diff:
            lines.append(diff)
    expected_agg = _aggregation_signature(case.expected_aggregation_overrides)
    actual_agg = _aggregation_signature(query.aggregation_overrides)
    if expected_agg != actual_agg:
        fmt = lambda sig: "、".join(f"{m}={a}" for m, a in sig) or "无"  # noqa: E731
        lines.append(f"聚合覆盖: 期望 {fmt(expected_agg)} → 实际 {fmt(actual_agg)}")
    return lines


def _fmt_filters(filters: tuple[tuple[Any, Any, Any], ...]) -> str:
    if not filters:
        return "无"
    return "、".join(
        f"{fid} {op.value if hasattr(op, 'value') else op} {value!r}"
        for fid, op, value in filters
    )


def _filters_diff(case: GoldenCase, query: Any) -> list[str]:
    lines: list[str] = []
    groups = (
        ("过滤", case.expected_filters, query.filters, "dimension_id"),
        ("度量过滤", case.expected_measure_filters, query.measure_filters, "metric_id"),
        ("指标过滤", case.expected_metric_filters, query.metric_filters, "metric_id"),
    )
    for label, expected_items, actual_items, key in groups:
        expected = tuple((getattr(i, key), i.operator, i.value) for i in expected_items)
        actual = tuple((getattr(i, key), i.operator, i.value) for i in actual_items)
        if not _filters_equal(actual, expected):
            lines.append(f"{label}: 期望 {_fmt_filters(expected)} → 实际 {_fmt_filters(actual)}")
    return lines


def _fmt_order(orders: tuple[tuple[str, str], ...]) -> str:
    return "、".join(f"{element} {direction}" for element, direction in orders) or "无"


def _modifiers_diff(case: GoldenCase, query: Any) -> list[str]:
    lines: list[str] = []
    expected_order = _order_signature(case.expected_order_by or ())
    actual_order = _order_signature(query.order_by)
    if expected_order != actual_order:
        lines.append(f"排序: 期望 {_fmt_order(expected_order)} → 实际 {_fmt_order(actual_order)}")
    if query.limit != case.expected_limit:
        lines.append(f"limit: 期望 {case.expected_limit} → 实际 {query.limit}")
    return lines


def _fmt_row(row: tuple[Any, ...]) -> str:
    return "(" + ", ".join(repr(value) for value in row) + ")"


def _result_diff(
    *, response: CompletedQueryResponse, case: GoldenCase, projection_matches: bool
) -> list[str]:
    if case.expected_rows is None:
        return []
    aligned = _align_rows(
        actual_columns=response.data.columns,
        actual_rows=response.data.rows,
        expected_columns=(*case.expected_dimension_ids, *case.expected_metric_ids),
    )
    if aligned is None:
        return [f"结果列无法与期望对齐: 实际列 {_fmt_ids(response.data.columns)}"]
    expected_rows = case.expected_rows
    if len(aligned) != len(expected_rows):
        return [f"行数: 期望 {len(expected_rows)} 行 → 实际 {len(aligned)} 行"]
    actual_view = aligned
    expected_view = expected_rows
    if not (case.row_order_matters and case.expected_limit is not None):
        actual_view = tuple(sorted(aligned, key=repr))
        expected_view = tuple(sorted(expected_rows, key=repr))
    for index, (actual_row, expected_row) in enumerate(
        zip(actual_view, expected_view, strict=True)
    ):
        if len(actual_row) != len(expected_row) or not all(
            _values_equal(a, e, case.numeric_tolerance)
            for a, e in zip(actual_row, expected_row, strict=True)
        ):
            expected = _fmt_row(expected_row)
            actual = _fmt_row(actual_row)
            return [f"第 {index + 1} 行不一致: 期望 {expected} → 实际 {actual}"]
    return ["行内容不一致(顺序敏感比较)"]


def _projection_equal(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """Compare a semantic projection as a multiset, not as SQL column order."""

    return sorted(actual) == sorted(expected)


def _aggregation_signature(overrides: tuple[Any, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((item.metric_id, item.aggregation.value) for item in overrides))


def _order_signature(orders: tuple[Any, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((item.element_id, item.direction.value) for item in orders)


def _align_rows(
    *,
    actual_columns: tuple[str, ...],
    actual_rows: tuple[tuple[Any, ...], ...],
    expected_columns: tuple[str, ...],
) -> tuple[tuple[Any, ...], ...] | None:
    """Align result tuples by stable semantic element id before comparison."""

    if len(actual_columns) != len(expected_columns):
        return None
    # 占比类查询的输出列是合成别名(如 _华南净收入占比_),不是语义 id——按 id
    # 必然对不上。列名与期望 id 完全不相交时按位置对齐;只要有一列能按 id
    # 对上(说明 id 命名在用),仍走严格对齐,防列序漂移的保护不放松。
    if not set(actual_columns) & set(expected_columns):
        return actual_rows
    available: dict[str, list[int]] = {}
    for index, column in enumerate(actual_columns):
        available.setdefault(column, []).append(index)
    permutation: list[int] = []
    for column in expected_columns:
        indexes = available.get(column)
        if not indexes:
            return None
        permutation.append(indexes.pop(0))
    if any(indexes for indexes in available.values()):
        return None
    if any(len(row) != len(actual_columns) for row in actual_rows):
        return None
    return tuple(tuple(row[index] for index in permutation) for row in actual_rows)


def _completed_result_matches(
    *, response: CompletedQueryResponse, case: GoldenCase, projection_matches: bool
) -> bool | None:
    if case.expected_rows is None:
        return None
    if projection_matches:
        actual_rows = _align_rows(
            actual_columns=response.data.columns,
            actual_rows=response.data.rows,
            expected_columns=(*case.expected_dimension_ids, *case.expected_metric_ids),
        )
    else:
        permutation = _semantic_slot_permutation(
            actual_dimensions=response.semantic_query.dimension_ids,
            actual_metrics=response.semantic_query.metric_ids,
            expected_dimensions=case.expected_dimension_ids,
            expected_metrics=case.expected_metric_ids,
        )
        actual_rows = (
            tuple(tuple(row[index] for index in permutation) for row in response.data.rows)
            if permutation is not None
            and all(len(row) == len(permutation) for row in response.data.rows)
            else None
        )
    return actual_rows is not None and _rows_equal(
        actual_rows,
        case.expected_rows,
        # 行序只在 top-N 下有语义:无 limit 时行序完全由是否出现 ORDER BY 决定,
        # 而装饰性排序已不参与比较,行序敏感只会连带假失败。
        order_matters=case.row_order_matters and case.expected_limit is not None,
        tolerance=case.numeric_tolerance,
    )


def _semantic_slot_permutation(
    *,
    actual_dimensions: tuple[str, ...],
    actual_metrics: tuple[str, ...],
    expected_dimensions: tuple[str, ...],
    expected_metrics: tuple[str, ...],
) -> tuple[int, ...] | None:
    """Align semantic drift by kind, retaining exact-id matches where possible."""

    if len(actual_dimensions) != len(expected_dimensions) or len(actual_metrics) != len(
        expected_metrics
    ):
        return None
    dimension_slots = _slot_permutation(actual_dimensions, expected_dimensions, offset=0)
    metric_slots = _slot_permutation(
        actual_metrics,
        expected_metrics,
        offset=len(actual_dimensions),
    )
    if dimension_slots is None or metric_slots is None:
        return None
    return (*dimension_slots, *metric_slots)


def _slot_permutation(
    actual: tuple[str, ...], expected: tuple[str, ...], *, offset: int
) -> tuple[int, ...] | None:
    available = list(range(len(actual)))
    resolved: list[int | None] = []
    for element_id in expected:
        exact = next((index for index in available if actual[index] == element_id), None)
        resolved.append(exact)
        if exact is not None:
            available.remove(exact)
    fallback = iter(available)
    return tuple(offset + (index if index is not None else next(fallback)) for index in resolved)


def _summarize(
    suite: GoldenSuite,
    results: tuple[EvaluationCaseResult, ...],
) -> dict[str, Any]:
    """Build diagnostic metrics without weakening the all-case publication gate.

    Semantic and result accuracy are end-to-end rates over cases that should have
    completed. A wrong state therefore also counts against the downstream rate.
    Rejection accuracy covers both expected failures and expected clarifications.
    "Silent wrong" means the service returned a completed answer for a case that
    did not pass, which is the highest-risk outcome for trusted analytics.
    """

    pairs = tuple(zip(suite.cases, results, strict=True))
    expected_completed = sum(case.expected_state is QueryState.COMPLETED for case, _ in pairs)
    expected_non_completed = len(pairs) - expected_completed
    state_passed = sum(result.actual_state is case.expected_state for case, result in pairs)
    semantic_passed = sum(
        case.expected_state is QueryState.COMPLETED
        and result.actual_state is QueryState.COMPLETED
        and result.failure_stage not in {"state", "mapping", "semantic"}
        for case, result in pairs
    )
    result_passed = sum(
        case.expected_state is QueryState.COMPLETED and result.passed for case, result in pairs
    )
    answer_correct = sum(
        case.expected_state is QueryState.COMPLETED
        and result.actual_state is QueryState.COMPLETED
        and result.result_matches_expected is True
        for case, result in pairs
    )
    rejection_passed = sum(
        case.expected_state is not QueryState.COMPLETED and result.passed for case, result in pairs
    )
    silent_wrong_count = sum(
        result.actual_state is QueryState.COMPLETED
        and not result.passed
        and result.result_matches_expected is not True
        for _, result in pairs
    )
    semantic_drift_count = sum(
        case.expected_state is QueryState.COMPLETED
        and result.actual_state is QueryState.COMPLETED
        and result.failure_stage == "semantic"
        and result.result_matches_expected is True
        for case, result in pairs
    )
    false_accept_count = sum(
        case.expected_state is not QueryState.COMPLETED
        and result.actual_state is QueryState.COMPLETED
        for case, result in pairs
    )
    false_refusal_count = sum(
        case.expected_state is QueryState.COMPLETED and result.actual_state is QueryState.FAILED
        for case, result in pairs
    )
    unexpected_clarification_count = sum(
        case.expected_state is QueryState.COMPLETED
        and result.actual_state is QueryState.CLARIFICATION_REQUIRED
        for case, result in pairs
    )
    failure_stage_counts: dict[str, int] = {}
    for result in results:
        if result.failure_stage is not None:
            failure_stage_counts[result.failure_stage] = (
                failure_stage_counts.get(result.failure_stage, 0) + 1
            )

    tagged: dict[str, list[EvaluationCaseResult]] = {}
    for case, result in pairs:
        for tag in case.tags:
            tagged.setdefault(tag, []).append(result)
    slices = tuple(
        EvaluationSlice(
            tag=tag,
            total=len(items),
            passed=sum(item.passed for item in items),
            accuracy=sum(item.passed for item in items) / len(items),
        )
        for tag, items in sorted(tagged.items())
    )
    return {
        "state_accuracy": state_passed / len(pairs),
        "semantic_accuracy": (semantic_passed / expected_completed if expected_completed else None),
        "result_accuracy": result_passed / expected_completed if expected_completed else None,
        "answer_correct": answer_correct,
        "answer_accuracy": answer_correct / expected_completed if expected_completed else None,
        "rejection_accuracy": (
            rejection_passed / expected_non_completed if expected_non_completed else None
        ),
        "expected_completed": expected_completed,
        "expected_non_completed": expected_non_completed,
        "silent_wrong_count": silent_wrong_count,
        "semantic_drift_count": semantic_drift_count,
        "false_accept_count": false_accept_count,
        "false_refusal_count": false_refusal_count,
        "unexpected_clarification_count": unexpected_clarification_count,
        "failure_stage_counts": failure_stage_counts,
        "slices": slices,
    }


def _rows_equal(
    actual: tuple[tuple[Any, ...], ...],
    expected: tuple[tuple[Any, ...], ...],
    *,
    order_matters: bool,
    tolerance: Decimal,
) -> bool:
    if len(actual) != len(expected):
        return False
    if not order_matters:
        actual = tuple(sorted(actual, key=repr))
        expected = tuple(sorted(expected, key=repr))
    return all(
        len(actual_row) == len(expected_row)
        and all(
            _values_equal(actual_value, expected_value, tolerance)
            for actual_value, expected_value in zip(actual_row, expected_row, strict=True)
        )
        for actual_row, expected_row in zip(actual, expected, strict=True)
    )


def _values_equal(actual: Any, expected: Any, tolerance: Decimal) -> bool:
    if isinstance(actual, (date, datetime)) or isinstance(expected, (date, datetime)):
        return _semantic_value(actual) == _semantic_value(expected)
    if isinstance(actual, float) and math.isnan(actual):
        return isinstance(expected, float) and math.isnan(expected)
    # 期望行天生经 JSON 往返:Decimal('80') 存进用例就是字符串 '80'。只要有
    # 一侧是数值,就尝试把另一侧当数值解析并按容差比较——否则「答对了就存」
    # 的用例自产自销都过不了。两侧都是字符串时不做数值化('华东' 原样比较)。
    left_numeric = isinstance(actual, (int, float, Decimal)) and not isinstance(actual, bool)
    right_numeric = isinstance(expected, (int, float, Decimal)) and not isinstance(expected, bool)
    if left_numeric or right_numeric:
        try:
            return abs(Decimal(str(actual)) - Decimal(str(expected))) <= tolerance
        except InvalidOperation:
            return False
    return actual == expected


def _filters_equal(
    actual: tuple[tuple[Any, ...], ...], expected: tuple[tuple[Any, ...], ...]
) -> bool:
    if len(actual) != len(expected):
        return False
    remaining = list(expected)
    for left in actual:
        matched_index = next(
            (
                index
                for index, right in enumerate(remaining)
                if left[0] == right[0]
                and left[1] == right[1]
                and _filter_value_equal(left[1], left[2], right[2])
            ),
            None,
        )
        if matched_index is None:
            return False
        remaining.pop(matched_index)
    return not remaining


def _filter_value_equal(operator: Any, actual: Any, expected: Any) -> bool:
    if operator in {"in", "not_in"} or getattr(operator, "value", None) in {
        "in",
        "not_in",
    }:
        if not isinstance(actual, (list, tuple)) or not isinstance(expected, (list, tuple)):
            return False
        actual_values = sorted((_semantic_value(item) for item in actual), key=repr)
        expected_values = sorted((_semantic_value(item) for item in expected), key=repr)
        return actual_values == expected_values
    return _semantic_value(actual) == _semantic_value(expected)


def _semantic_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return tuple(_semantic_value(item) for item in value)
    return value
