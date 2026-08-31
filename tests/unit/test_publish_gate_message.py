from __future__ import annotations

from knowflow_analytics.catalog.release import describe_evaluation_gate_failure


def test_message_says_how_many_more_cases_are_needed() -> None:
    """此前只说"门禁未通过"，用户不知道差几条、差多少准确率。"""

    message = describe_evaluation_gate_failure(
        total=22, accuracy=1.0, gate_passed=True, minimum_cases=30, minimum_accuracy=1.0
    )
    assert "还需要 8 条" in message


def test_message_states_the_accuracy_gap() -> None:
    message = describe_evaluation_gate_failure(
        total=30, accuracy=0.9667, gate_passed=True, minimum_cases=30, minimum_accuracy=1.0
    )
    assert "96.7%" in message
    assert "100%" in message


def test_message_covers_both_gaps_at_once() -> None:
    message = describe_evaluation_gate_failure(
        total=10, accuracy=0.5, gate_passed=True, minimum_cases=30, minimum_accuracy=1.0
    )
    assert "还需要 20 条" in message
    assert "50.0%" in message


def test_message_when_no_evaluation_has_been_run() -> None:
    message = describe_evaluation_gate_failure(
        total=None, accuracy=None, gate_passed=False, minimum_cases=30, minimum_accuracy=1.0
    )
    assert "尚未运行" in message
    assert "30 条" in message
