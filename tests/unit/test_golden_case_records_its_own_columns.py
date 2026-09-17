"""用例记住那次答案长什么样，比对就不必从投影反推。

现场（2026-09-17）：「账户余额大于 1000 的有多少人」试问答对 28，存为用例后评测报
「结果列无法与期望对齐」。两边其实都是**一列**——试问一列、重跑一列、存下的
`expected_rows` 也是一列；唯一说两列的是语义投影，因为 CTE 内部那个 `GROUP BY 账号`
也算投影成员。

评测重跑的是同一个问题、同一条链路，两边都是真实执行结果，本该直接比。之所以要算，
是因为 GoldenCase 只存了 `expected_rows` 和一份语义投影，没存**那次的列**，于是比对得
从投影反推列清单——而投影是有损的，反推不出来，就堆出了宽度推断、id 交集判断、
合成别名例外那一套。复杂度全是反推带出来的。

按语义 id 对齐本身是对的：模型这次写 `SELECT 指标, 维度`、下次写 `SELECT 维度, 指标`，
答案一样但行元组顺序反了。但那只需要存下那次的列，不需要猜。
"""

from __future__ import annotations

from typing import Any

import pytest

from knowflow_analytics.contracts import Aggregation, QueryAggregationOverride
from knowflow_analytics.evaluation.contracts import GoldenCase
from knowflow_analytics.evaluation.evaluator import _aligned_actual_rows
from knowflow_analytics.query.contracts import QueryState

_DIM = "dimension:field:knowflow_analytics:acct_model_warn_curr:zhhao"
_METRIC = "metric:field:knowflow_analytics:acct_model_warn_curr:zhye"


def _case(
    *,
    columns: tuple[str, ...] | None,
    rows: tuple[tuple[Any, ...], ...],
) -> GoldenCase:
    return GoldenCase(
        id="case-1",
        question="账户余额大于 1000 的有多少人",
        dataset_ids=("sales_dataset",),
        expected_state=QueryState.COMPLETED,
        expected_dataset_id="sales_dataset",
        expected_metric_ids=(_METRIC,),
        expected_aggregation_overrides=(
            QueryAggregationOverride(metric_id=_METRIC, aggregation=Aggregation.AVG),
        ),
        expected_dimension_ids=(_DIM,),
        expected_columns=columns,
        expected_rows=rows,
    )


def test_a_collapsed_answer_needs_no_width_guessing() -> None:
    """一列就是一列。投影有几个成员与这次输出几列无关。"""

    case = _case(columns=(_DIM,), rows=((28,),))

    assert _aligned_actual_rows(
        case=case, actual_columns=(_DIM,), actual_rows=((28,),)
    ) == ((28,),)


def test_column_order_drift_is_absorbed() -> None:
    """同样的答案换个列序仍然是同样的答案——这才是按 id 对齐的用处。"""

    case = _case(columns=(_DIM, _METRIC), rows=(("A001", 100),))

    assert _aligned_actual_rows(
        case=case, actual_columns=(_METRIC, _DIM), actual_rows=((100, "A001"),)
    ) == (("A001", 100),)


def test_a_different_set_of_columns_is_a_real_difference() -> None:
    """列不一样就是答的不是一回事,不要对齐到「看起来能比」为止。"""

    case = _case(columns=(_DIM,), rows=((28,),))

    assert (
        _aligned_actual_rows(case=case, actual_columns=(_METRIC,), actual_rows=((28,),))
        is None
    )


def test_repeated_columns_keep_their_positions() -> None:
    """同一个成员出现两次(原值与占比)时,按出现次序一一对应。"""

    case = _case(columns=(_METRIC, _METRIC), rows=((100, 0.4),))

    assert _aligned_actual_rows(
        case=case, actual_columns=(_METRIC, _METRIC), actual_rows=((100, 0.4),)
    ) == ((100, 0.4),)


@pytest.mark.parametrize(
    ("actual_columns", "actual_rows", "expected"),
    [
        ((_DIM,), ((28,),), ((28,),)),
        (("_合成别名_",), ((28,),), ((28,),)),
    ],
)
def test_a_case_saved_before_columns_were_recorded_still_runs(
    actual_columns, actual_rows, expected
) -> None:
    """存量用例没有这个字段,走原来的兜底,不因为升级整批失败。"""

    case = _case(columns=None, rows=((28,),))

    assert _aligned_actual_rows(
        case=case, actual_columns=actual_columns, actual_rows=actual_rows
    ) == expected
