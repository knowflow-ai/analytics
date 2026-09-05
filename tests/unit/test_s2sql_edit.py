"""续跑编辑的合同：只改文本 S2SQL 的 AST，粒度与期间比这类形状原样保留。"""

from __future__ import annotations

import pytest

from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.s2sql_edit import (
    UNSUPPORTED_CODE,
    add_dimension,
    editable_select,
    remove_dimension,
    replace_filter_value,
    replace_metric,
    set_time_window,
)

MONTHLY_RATIO = (
    'SELECT DATE_TRUNC(\'MONTH\', "销售日期"), RATIO_ROLL("销售金额") '
    'FROM "销售明细分析" WHERE "商品类别" = \'咖啡\' GROUP BY DATE_TRUNC(\'MONTH\', "销售日期")'
)
METRICS = {"销售金额", "销售数量"}


def _is_metric(name: str) -> bool:
    return name in METRICS


class TestShapeGate:
    def test_a_plain_select_is_editable(self) -> None:
        assert editable_select(MONTHLY_RATIO) is not None

    @pytest.mark.parametrize(
        "s2sql",
        [
            'WITH "t" AS (SELECT "区域" FROM "销售经营") SELECT "区域" FROM "t"',
            'SELECT "区域" FROM "销售经营" UNION ALL SELECT "区域" FROM "销售经营"',
            'SELECT "区域" FROM (SELECT "区域" FROM "销售经营") AS "t"',
            "not sql at all",
        ],
    )
    def test_other_shapes_are_refused(self, s2sql: str) -> None:
        assert editable_select(s2sql) is None
        with pytest.raises(SemanticParsingError) as raised:
            replace_filter_value(s2sql, "区域", "华东")
        assert raised.value.code == UNSUPPORTED_CODE


class TestRefilter:
    def test_the_value_changes_and_the_monthly_ratio_shape_survives(self) -> None:
        """实机：「按月咖啡的环比」→「商品类别」换成「烘焙」必须还是按月环比。"""
        edited = replace_filter_value(MONTHLY_RATIO, "商品类别", "烘焙")
        assert "'烘焙'" in edited and "'咖啡'" not in edited
        assert "DATE_TRUNC('MONTH', \"销售日期\")" in edited
        assert 'RATIO_ROLL("销售金额")' in edited
        assert "GROUP BY" in edited

    def test_other_predicates_stay_and_a_missing_filter_is_added(self) -> None:
        base = (
            'SELECT "区域", "净收入" FROM "销售经营" '
            'WHERE "渠道" = \'线上\' AND "净收入" > 10 GROUP BY "区域"'
        )
        edited = replace_filter_value(base, "区域", "华东")
        assert "\"渠道\" = '线上'" in edited
        assert '"净收入" > 10' in edited
        assert "\"区域\" = '华东'" in edited

    def test_in_lists_on_the_same_dimension_are_replaced_too(self) -> None:
        base = (
            'SELECT "区域", "净收入" FROM "销售经营" '
            'WHERE "区域" IN (\'华东\', \'华南\') GROUP BY "区域"'
        )
        edited = replace_filter_value(base, "区域", "华北")
        assert "IN (" not in edited
        assert "\"区域\" = '华北'" in edited

    def test_the_literal_is_escaped(self) -> None:
        edited = replace_filter_value(MONTHLY_RATIO, "商品类别", "O'Neil")
        assert "'O''Neil'" in edited


class TestAddAndRemoveDimension:
    def test_add_keeps_the_time_bucket_and_extends_group_by(self) -> None:
        edited = add_dimension(MONTHLY_RATIO, "门店名称", is_metric=_is_metric)
        assert edited.startswith(
            'SELECT DATE_TRUNC(\'MONTH\', "销售日期"), "门店名称", RATIO_ROLL("销售金额")'
        )
        assert edited.endswith('GROUP BY DATE_TRUNC(\'MONTH\', "销售日期"), "门店名称"')

    def test_add_creates_group_by_for_a_groupless_aggregate(self) -> None:
        edited = add_dimension('SELECT SUM("净收入") FROM "销售经营"', "区域", is_metric=_is_metric)
        assert edited == 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'

    def test_add_is_idempotent(self) -> None:
        once = add_dimension(MONTHLY_RATIO, "门店名称", is_metric=_is_metric)
        assert add_dimension(once, "门店名称", is_metric=_is_metric) == once

    def test_remove_drops_projection_group_and_order_but_keeps_value_filters(self) -> None:
        base = (
            'SELECT "区域", "渠道", "净收入" FROM "销售经营" WHERE "区域" = \'华东\' '
            'GROUP BY "区域", "渠道" ORDER BY "区域" DESC, "净收入" DESC'
        )
        edited = remove_dimension(base, "区域", is_metric=lambda n: n == "净收入")
        assert edited == (
            'SELECT "渠道", "净收入" FROM "销售经营" WHERE "区域" = \'华东\' '
            'GROUP BY "渠道" ORDER BY "净收入" DESC'
        )

    def test_remove_the_aliased_time_bucket_takes_its_alias_out_of_order_by(self) -> None:
        base = (
            'SELECT DATE_TRUNC(\'MONTH\', "下单日期") AS "月份", "净收入" FROM "销售经营" '
            'GROUP BY DATE_TRUNC(\'MONTH\', "下单日期") ORDER BY "月份"'
        )
        edited = remove_dimension(base, "下单日期", is_metric=lambda n: n == "净收入")
        assert edited == 'SELECT "净收入" FROM "销售经营"'

    def test_remove_never_leaves_an_empty_projection(self) -> None:
        with pytest.raises(SemanticParsingError) as raised:
            remove_dimension('SELECT "区域" FROM "销售经营"', "区域", is_metric=_is_metric)
        assert raised.value.code == UNSUPPORTED_CODE


class TestReplaceMetric:
    def test_a_ratio_keeps_its_shape_with_the_new_metric(self) -> None:
        edited = replace_metric(MONTHLY_RATIO, "销售数量", is_metric=_is_metric)
        assert 'RATIO_ROLL("销售数量")' in edited
        assert "销售金额" not in edited
        assert "DATE_TRUNC('MONTH', \"销售日期\")" in edited

    def test_plain_aggregates_collapse_to_one_bare_metric_and_metric_orders_go(self) -> None:
        base = (
            'SELECT "区域", SUM("净收入") AS "收入", AVG("净收入") FROM "销售经营" '
            'GROUP BY "区域" HAVING SUM("净收入") > 100 ORDER BY "收入" DESC, "区域"'
        )
        edited = replace_metric(base, "退款金额", is_metric=lambda n: n in {"净收入", "退款金额"})
        assert edited == 'SELECT "区域", "退款金额" FROM "销售经营" GROUP BY "区域" ORDER BY "区域"'

    def test_a_bare_metric_projection_is_replaced(self) -> None:
        base = 'SELECT "区域", "净收入" FROM "销售经营" GROUP BY "区域"'
        edited = replace_metric(base, "退款金额", is_metric=lambda n: n in {"净收入", "退款金额"})
        assert edited == 'SELECT "区域", "退款金额" FROM "销售经营" GROUP BY "区域"'


class TestTimeWindow:
    def test_the_window_replaces_every_range_on_the_time_dimension(self) -> None:
        base = (
            'SELECT "区域", "净收入" FROM "销售经营" WHERE "下单日期" >= \'2026-01-01\' '
            'AND "下单日期" < \'2026-02-01\' AND "区域" = \'华东\' GROUP BY "区域"'
        )
        edited = set_time_window(base, "下单日期", "2026-08-01")
        assert edited == (
            'SELECT "区域", "净收入" FROM "销售经营" WHERE "区域" = \'华东\' '
            'AND "下单日期" >= \'2026-08-01\' GROUP BY "区域"'
        )

    def test_all_time_drops_the_range_and_keeps_the_ratio_shape(self) -> None:
        base = MONTHLY_RATIO.replace(
            "WHERE \"商品类别\" = '咖啡'",
            "WHERE \"商品类别\" = '咖啡' AND \"销售日期\" >= '2026-08-01'",
        )
        edited = set_time_window(base, "销售日期", None)
        assert edited == MONTHLY_RATIO
