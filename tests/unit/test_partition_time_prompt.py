from __future__ import annotations

from pathlib import Path

_PARSER_SOURCE = (
    Path(__file__).parents[2] / "src" / "knowflow_analytics" / "query" / "parser.py"
).read_text(encoding="utf-8")


def test_prompt_tells_the_model_to_use_the_given_partition_time_name() -> None:
    """模型编造过「数据日期」这个不存在的时间维度。

    partition_time 只是裸传一个 {"name": ...}，提示词从没说明它是什么、该不该用，
    于是模型自己造了个业务名，最终被 AST 校验拒绝：
    LLM_S2SQL_AST_INVALID: unknown semantic business name: 数据日期。
    """

    assert "partition_time 给出的名称" in _PARSER_SOURCE
    assert "不得自行命名时间列" in _PARSER_SOURCE


def test_prompt_still_forbids_inventing_any_column_name() -> None:
    """总约束仍在：列只能来自给定的指标/维度。"""

    assert "列只能" in _PARSER_SOURCE
    assert "禁止内部 ID、物理表和物理列" in _PARSER_SOURCE


def test_unknown_name_error_lists_what_is_available(sales_release) -> None:
    """报错只说「unknown semantic business name: 数据日期」，
    不告诉用户有哪些名字可用，也不给模型重试的线索。"""

    from knowflow_analytics.errors import AnalyticsError
    from knowflow_analytics.query.symbols import SemanticSymbolTable

    dataset = sales_release.datasets[0]
    table = SemanticSymbolTable(release=sales_release, dataset=dataset)
    try:
        table.resolve_first("绝不存在的业务名")
    except AnalyticsError as exc:
        message = str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("expected an error for an unknown name")

    assert "绝不存在的业务名" in message
    # 至少列出一个真实可用的业务名，用户才知道该改成什么
    known = {item.name for item in sales_release.metrics}
    assert any(name in message for name in known)


def test_prompt_allows_any_governed_time_dimension_for_a_time_filter() -> None:
    """上一版提示词说「partition_time 为空时不要添加时间过滤」，
    结果模型把「截止到 8 月 2 日」整个丢掉，返回了全部历史数据。

    partition_time 只在 dimension_type == "partition_time" 时才有值，而普通
    「时间」角色的维度（如订单日期）同样可以做时间过滤，且本来就在 dimensions
    清单里。提示词必须允许使用它们。
    """

    assert "semantic_type 为 time 的维度" in _PARSER_SOURCE
    # 不能再出现"没有 partition_time 就别加时间过滤"这种授权丢弃条件的说法
    assert "partition_time 为空时不要添加时间过滤" not in _PARSER_SOURCE


def test_prompt_forbids_dropping_a_time_condition_the_user_asked_for() -> None:
    """没有任何可用时间维度时，正确做法是说明缺失，而不是静默丢掉条件。"""

    assert "不得直接忽略" in _PARSER_SOURCE
