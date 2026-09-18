"""两条时间治理规则此前只写在提示词里，翻译期零强制。

第 22 条「时间维度若带 time_granularity，它是该列数据的真实粒度，不得生成比它更细的
粒度」：建模者声明了，编译器没验。模型写 DATE_TRUNC('day') 打在一列月粒度数据上，
SQL 合法、执行成功、每天一行、数字看起来正常——只是这列根本没有天。

第 13 条「若问题要求了时间范围却没有任何可用时间维度……」背后是 `requires_explicit_time`
这个指标级声明。它唯一的检查点在 `_apply_time_filters`，只被 rule 候选与结构化路径调用；
客户实际走的自然语言路径不经过它。声明了，等于没声明。

两条都是「依据已在语义模型里，检查没接上」——最便宜的一类编译器缺失。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.service import _error_diagnosis
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'
_MODELER_JARGON = ("SQL", "Embedding", "Revision", "Release", "SQLSTATE", "Corrector", "fallback")


def _translate(release, s2sql: str):
    return S2SqlSemanticTranslator().translate(
        release=release, dataset_id="sales_dataset", corrected_s2sql=s2sql
    )


def _with_monthly_order_date(release):
    dimensions = tuple(
        item.model_copy(update={"time_granularity": TimeGranularity.MONTH})
        if item.id == "order_date"
        else item
        for item in release.dimensions
    )
    return release.model_copy(update={"dimensions": dimensions})


def _with_explicit_time_required(release, metric_id: str = "net_revenue"):
    metrics = tuple(
        item.model_copy(update={"requires_explicit_time": True}) if item.id == metric_id else item
        for item in release.metrics
    )
    return release.model_copy(update={"metrics": metrics})


# ── 第 22 条：声明的粒度是这列数据的真实粒度 ──────────────────────────────────


def test_a_finer_grain_than_declared_is_refused(sales_release) -> None:
    """月粒度的列没有「天」。按天截断会得到每天一行的合法结果，数字全是错的。"""

    with pytest.raises(SemanticParsingError) as raised:
        _translate(
            _with_monthly_order_date(sales_release),
            f'SELECT DATE_TRUNC(\'day\', "下单日期") AS "_日_", SUM("净收入") FROM {_T} GROUP BY 1',
        )

    assert raised.value.code == "S2SQL_TIME_GRANULARITY_TOO_FINE"
    assert "下单日期" in str(raised.value)


@pytest.mark.parametrize("grain", ["month", "quarter", "year"])
def test_the_declared_grain_or_coarser_is_fine(sales_release, grain: str) -> None:
    translated = _translate(
        _with_monthly_order_date(sales_release),
        f'SELECT DATE_TRUNC(\'{grain}\', "下单日期") AS "_期_", SUM("净收入") FROM {_T} GROUP BY 1',
    )

    assert translated.dimension_ids == ("order_date",)


def test_an_undeclared_grain_keeps_todays_behaviour(sales_release) -> None:
    """没声明粒度就没有依据，不猜：按天照常翻译。"""

    translated = _translate(
        sales_release,
        f'SELECT DATE_TRUNC(\'day\', "下单日期") AS "_日_", SUM("净收入") FROM {_T} GROUP BY 1',
    )

    assert translated.dimension_ids == ("order_date",)


# ── 第 13 条：requires_explicit_time 在自然语言路径上也要生效 ──────────────────


def test_a_metric_that_requires_a_time_range_refuses_a_query_without_one(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(
            _with_explicit_time_required(sales_release),
            f'SELECT "区域", SUM("净收入") FROM {_T} GROUP BY "区域"',
        )

    assert raised.value.code == "EXPLICIT_TIME_REQUIRED"
    assert "净收入" in str(raised.value)


def test_a_filter_on_a_non_time_dimension_does_not_count(sales_release) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(
            _with_explicit_time_required(sales_release),
            f'SELECT SUM("净收入") FROM {_T} WHERE "区域" = \'华东\'',
        )

    assert raised.value.code == "EXPLICIT_TIME_REQUIRED"


def test_a_time_predicate_satisfies_it(sales_release) -> None:
    translated = _translate(
        _with_explicit_time_required(sales_release),
        f'SELECT "区域", SUM("净收入") FROM {_T} '
        f"WHERE \"下单日期\" >= '2026-08-01' AND \"下单日期\" < '2026-09-01' GROUP BY \"区域\"",
    )

    assert translated.metric_ids == ("net_revenue",)


def test_only_the_metrics_in_the_query_are_consulted(sales_release) -> None:
    """退款金额要求时间范围，但这次问的是净收入——与它无关。"""

    translated = _translate(
        _with_explicit_time_required(sales_release, metric_id="refund_amount"),
        f'SELECT "区域", SUM("净收入") FROM {_T} GROUP BY "区域"',
    )

    assert translated.metric_ids == ("net_revenue",)


# ── 两条都要有一句提问者听得懂的话，不能掉进「计算方式不支持」 ───────────────────


@pytest.mark.parametrize(
    ("code", "must_mention"),
    [
        ("S2SQL_TIME_GRANULARITY_TOO_FINE", "粒度"),
        ("EXPLICIT_TIME_REQUIRED", "时间"),
    ],
)
def test_the_asker_is_told_what_is_wrong(code: str, must_mention: str) -> None:
    diagnosis = _error_diagnosis(SemanticParsingError("x", code=code))

    assert must_mention in diagnosis.user_hint
    assert "计算方式" not in diagnosis.user_hint
    assert not any(word in diagnosis.user_hint for word in _MODELER_JARGON)
