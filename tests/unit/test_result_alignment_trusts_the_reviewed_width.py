"""人确认过的那几列，才是这次输出真正有几列。

现场（2026-09-17）：「账户余额大于 1000 的有多少人」试问答对 28，存为评测用例后
跑评测报「结果列无法与期望对齐: 实际列 dimension:…:zhhao」。

模型写的是 CTE ——先按账号求平均余额，再数出平均余额大于 1000 的账号，输出**一列**。
语义投影里却有两个成员（指标 账户余额 + 分组维度 账号），因为 CTE 内部的分组维度
也算投影成员。`_align_rows` 本来有一条位置对齐的后路专为这种形状准备，判据却是
「列名与期望 id 完全不相交」——而这一列的标签恰好被映射回了账号维度的语义 id，
交集非空，后路不启用，直接判死。

那个重合是标签巧合，不是证据：这一列的值是 COUNT，不是账号。判据应该是宽度——
人确认过的结果有几列是事实，投影成员比它多属于已知的有损，不是列序漂移。
列数相等时的严格对齐一个字不放松，那道防漂移的保护仍然管着真正的风险。
"""

from __future__ import annotations

from knowflow_analytics.evaluation.evaluator import _align_rows

_DIM = "dimension:field:knowflow_analytics:acct_model_warn_curr:zhhao"
_METRIC = "metric:field:knowflow_analytics:acct_model_warn_curr:zhye"


def test_a_collapsed_answer_aligns_even_when_its_label_reuses_a_member_id() -> None:
    """输出一列、投影两个成员：以人确认过的宽度为准。"""

    aligned = _align_rows(
        actual_columns=(_DIM,),
        actual_rows=((28,),),
        expected_columns=(_DIM, _METRIC),
        expected_width=1,
    )

    assert aligned == ((28,),)


def test_column_order_drift_is_still_caught() -> None:
    """列数相等时严格按 id 对齐，列序漂移仍然是失败。"""

    aligned = _align_rows(
        actual_columns=(_METRIC, _DIM),
        actual_rows=((100, "A001"),),
        expected_columns=(_DIM, _METRIC),
        expected_width=2,
    )

    assert aligned == (("A001", 100),)


def test_a_width_that_matches_neither_is_refused() -> None:
    """既不是投影的宽度、也不是人确认过的宽度——那就是真的对不上。"""

    aligned = _align_rows(
        actual_columns=(_DIM, _METRIC, "expression:2"),
        actual_rows=((1, 2, 3),),
        expected_columns=(_DIM, _METRIC),
        expected_width=1,
    )

    assert aligned is None


def test_synthetic_aliases_still_align_by_position() -> None:
    """占比类查询的合成别名与语义 id 完全不相交，原有的位置对齐不受影响。"""

    aligned = _align_rows(
        actual_columns=("_华南净收入占比_",),
        actual_rows=((0.42,),),
        expected_columns=(_DIM, _METRIC),
        expected_width=1,
    )

    assert aligned == ((0.42,),)
