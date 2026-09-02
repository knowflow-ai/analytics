"""AVG(NUMERIC) 回来的 Decimal 带满刻度(0.30000000000000000000),原样透传到前端。

归一化只删无意义的尾零,不改数值精度,也绝不落到科学计数法(1E+2)。
"""

from decimal import Decimal

from knowflow_analytics.execution.executor import _normalize_cell


def test_trailing_zeros_are_stripped_without_scientific_notation():
    assert _normalize_cell(Decimal("0.30000000000000000000")) == Decimal("0.3")
    assert str(_normalize_cell(Decimal("0.30000000000000000000"))) == "0.3"
    assert str(_normalize_cell(Decimal("5.900000000000000"))) == "5.9"
    assert str(_normalize_cell(Decimal("1.000"))) == "1"
    assert str(_normalize_cell(Decimal("100.00"))) == "100"  # normalize() 会给 1E+2,不允许
    assert str(_normalize_cell(Decimal("1200"))) == "1200"


def test_precision_carrying_digits_are_kept():
    assert str(_normalize_cell(Decimal("0.325"))) == "0.325"
    assert str(_normalize_cell(Decimal("-0.050"))) == "-0.05"


def test_non_decimal_and_non_finite_values_pass_through():
    assert _normalize_cell(7) == 7
    assert _normalize_cell("南京") == "南京"
    assert _normalize_cell(None) is None
    nan = Decimal("NaN")
    assert _normalize_cell(nan).is_nan()
