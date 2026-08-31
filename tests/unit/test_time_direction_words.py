"""无年份日期与方向词的确定性解析。

用户实测:「8月2日以后净收入有多少」连问两次,一次 > 一次 >=,答案 80 与 280。
成因:所有日期正则都要求年份,「8月2日」落给 LLM,而 LLM 的边界不稳定。
同一系统对同一问题必须给同一答案——支持的表达必须确定性解析。

边界语义一次拍死(依中文惯例与《民法典》「开始的当日不计入」):
  以后/之后   → 不含当天    (下界 = D+1, 无上界)
  以来/起/开始 → 含当天      (下界 = D,   无上界)
  之前/以前   → 不含当天    (无下界, 上界 = D)
  截至/截止   → 含当天      (无下界, 上界 = D+1)
"""

from __future__ import annotations

from datetime import date, datetime

from knowflow_analytics.query.parser import _parse_time_range

NOW = datetime(2026, 8, 25, 10, 0)


def test_a_bare_month_day_gets_the_current_year():
    """「8月2日」没有年份:补当年,区间为当天。"""

    assert _parse_time_range("8月2日的净收入", NOW) == (date(2026, 8, 2), date(2026, 8, 3))


def test_after_excludes_the_day_itself():
    """「以后/之后」不含当天:下界是次日,没有上界。"""

    assert _parse_time_range("8月2日以后净收入有多少", NOW) == (date(2026, 8, 3), None)
    assert _parse_time_range("8月2日之后的订单", NOW) == (date(2026, 8, 3), None)
    assert _parse_time_range("2026年8月2日以后净收入", NOW) == (date(2026, 8, 3), None)


def test_since_includes_the_day_itself():
    assert _parse_time_range("8月2日以来的净收入", NOW) == (date(2026, 8, 2), None)
    assert _parse_time_range("8月2日起的订单", NOW) == (date(2026, 8, 2), None)


def test_before_excludes_the_day_itself():
    assert _parse_time_range("8月2日之前的净收入", NOW) == (None, date(2026, 8, 2))
    assert _parse_time_range("8月2日以前的订单", NOW) == (None, date(2026, 8, 2))


def test_until_includes_the_day_itself():
    assert _parse_time_range("截至8月2日的净收入", NOW) == (None, date(2026, 8, 3))
    assert _parse_time_range("截止到2026年8月2日", NOW) == (None, date(2026, 8, 3))


def test_closed_expressions_are_unchanged():
    """既有表达的行为一条都不能变。"""

    assert _parse_time_range("2026年8月2日的净收入", NOW) == (date(2026, 8, 2), date(2026, 8, 3))
    assert _parse_time_range("2026-08-01到2026-08-05", NOW) == (date(2026, 8, 1), date(2026, 8, 6))
    assert _parse_time_range("近7天", NOW) == (date(2026, 8, 18), date(2026, 8, 26))
    assert _parse_time_range("没有时间的问题", NOW) is None
