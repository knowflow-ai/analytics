"""答案里有系统自己猜的成员时，这次回答本身必须说出来。

`_inferred_member_names` 本来就在算它——答案用到、但精确证据里根本没出现过的成员，
说明用户的说法没被业务词典覆盖，模型自己挑了一个顶上。此前它只进
`QueryFailureRecord`（建模者要去翻问数反馈列表），**这次回答一个字都不说**。

实机 D012「2024年3月货币资金期末余额是多少」：目录里只有 `期末借方余额` 与
`期末贷方余额`，没有派生的"期末余额"（借方 − 贷方）。模型写出
``SUM("期末借方余额") AS "_期末余额_"``——别名叫用户问的那个，用的是另一个成员。
六道治理关全绿、数字看起来正常。拿到这个答案的人有权知道它建立在一次猜测上。
"""

from __future__ import annotations

from knowflow_analytics.query.contracts import QueryDiagnosis, QueryDiagnosticCategory
from knowflow_analytics.query.service import _with_inferred_members


def _clean() -> QueryDiagnosis:
    return QueryDiagnosis(
        category=QueryDiagnosticCategory.SUCCESS,
        stage="finished",
        severity="info",
        summary="问数完整链路执行成功",
        recommendation="核对语义解释和结果后可加入黄金问题。",
    )


def test_nothing_guessed_leaves_the_diagnosis_alone() -> None:
    diagnosis = _clean()

    assert _with_inferred_members(diagnosis, inferred_members=()) == diagnosis


def test_a_guessed_member_is_named_to_the_user() -> None:
    result = _with_inferred_members(_clean(), inferred_members=("期末借方余额",))

    assert "期末借方余额" in result.user_hint
    assert "系统自己选的" in result.user_hint


def test_the_modeler_is_told_it_may_be_a_missing_derived_metric() -> None:
    """补词典解决不了"期末余额＝借方−贷方"——那是缺一个派生指标。

    两种缺口的修法完全不同，recommendation 必须把话说到这一层，否则建模者会
    一直往词典里加别名而问题不动。
    """

    result = _with_inferred_members(_clean(), inferred_members=("期末借方余额",))

    assert "词典" in result.recommendation
    assert "派生指标" in result.recommendation


def test_the_warning_is_not_hidden_behind_the_diagnostics_flag() -> None:
    """info 级要 `include_diagnostics` 才出得来——猜测不该藏在开关后面。"""

    result = _with_inferred_members(_clean(), inferred_members=("期末借方余额",))

    assert result.severity == "warning"


def test_it_appends_to_an_existing_warning_instead_of_replacing_it() -> None:
    """零行告警与"猜了个成员"是两件都为真的事，不能互相盖掉。"""

    empty = QueryDiagnosis(
        category=QueryDiagnosticCategory.DATABASE_EXECUTION,
        stage="executing",
        severity="warning",
        summary="聚合结果是在零行上算出来的空值",
        recommendation="核对 FROM 与过滤条件。",
        user_hint="没有查到数据。",
    )

    result = _with_inferred_members(empty, inferred_members=("期末借方余额",))

    assert result.summary == empty.summary
    assert "没有查到数据。" in result.user_hint
    assert "期末借方余额" in result.user_hint
    assert "核对 FROM 与过滤条件。" in result.recommendation
