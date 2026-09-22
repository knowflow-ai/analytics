"""重采字典取值时必须重算层级，否则存量项目永远拿不到父指针。

2026-09-22 线上实测：客户项目 137 个已发布取值，存了 `parent_value` 的**一个都没有**。
根因是 `_with_hierarchy_parents` 当时只有一个调用点，在 `_preset_new_dimension_values`
里，而那个方法开头就 `if not created_ids: return updated`——**只在新建维度时跑**。
客户的「科目名称」早就存在，再发布多少次都填不上，问数期按父科目筛照样零行。

还有一层更隐蔽：`apply_dimension_dictionary_preview` 重建 `DimensionValueSpec` 时
不带 `parent_value`，所以在接上这一步之前，**每重采一次字典反而会把已有的父指针抹掉**。
"""

from __future__ import annotations

import inspect

import pytest

from knowflow_analytics.application import (
    AnalyticsApplication,
    _with_hierarchy_parents,
)
from knowflow_analytics.contracts import DimensionValueSpec


class _Profiler:
    def __init__(self, parents=None, error: Exception | None = None) -> None:
        self._parents = parents or {}
        self._error = error
        self.calls: list[tuple[str, ...]] = []

    def resolve_hierarchy_parents(self, *, semantic_spec, dimension_ids):
        self.calls.append(tuple(dimension_ids))
        if self._error is not None:
            raise self._error
        return self._parents


def _values() -> tuple[DimensionValueSpec, ...]:
    return (
        DimensionValueSpec(
            id="a", dimension_id="account", value="销售费用", display_name="销售费用"
        ),
        DimensionValueSpec(
            id="b",
            dimension_id="account",
            value="销售费用-差旅费",
            display_name="销售费用-差旅费",
        ),
        DimensionValueSpec(id="c", dimension_id="city", value="上海", display_name="上海"),
    )


def _parented(values, dimension_id: str, value: str):
    return next(
        item.parent_value
        for item in values
        if item.dimension_id == dimension_id and item.value == value
    )


class TestFillingTheParentPointers:
    def test_declared_pairs_land_on_the_values(self) -> None:
        profiler = _Profiler({"account": {"销售费用-差旅费": "销售费用"}})

        filled = _with_hierarchy_parents(
            _values(),
            profiler=profiler,
            semantic_spec=object(),
            dimension_ids=("account",),
        )

        assert _parented(filled, "account", "销售费用-差旅费") == "销售费用"
        # 根节点没有上级，不该被编出一个来。
        assert _parented(filled, "account", "销售费用") is None
        assert profiler.calls == [("account",)]

    def test_a_dimension_outside_the_refresh_is_untouched(self) -> None:
        profiler = _Profiler({"account": {"销售费用-差旅费": "销售费用"}})

        filled = _with_hierarchy_parents(
            _values(),
            profiler=profiler,
            semantic_spec=object(),
            dimension_ids=("account",),
        )

        assert _parented(filled, "city", "上海") is None

    def test_a_database_that_will_not_answer_does_not_break_the_write(self) -> None:
        """层级是锦上添花，读不到不该让整次采集/发布失败。"""

        profiler = _Profiler(error=RuntimeError("datasource unreachable"))

        filled = _with_hierarchy_parents(
            _values(),
            profiler=profiler,
            semantic_spec=object(),
            dimension_ids=("account",),
        )

        assert filled == _values()

    def test_no_declared_hierarchy_leaves_everything_alone(self) -> None:
        filled = _with_hierarchy_parents(
            _values(),
            profiler=_Profiler({}),
            semantic_spec=object(),
            dimension_ids=("account",),
        )

        assert filled == _values()


@pytest.mark.parametrize(
    "method_name",
    ["apply_dimension_dictionary_preview", "_preset_new_dimension_values"],
)
def test_every_path_that_writes_dictionary_values_refills_the_hierarchy(method_name) -> None:
    """漏调是这次的 bug 本身，而把整条预览流程跑起来要真 PostgreSQL。

    所以这里只钉「有没有调」：两条写 `dimension_values` 的路都必须过
    `_with_hierarchy_parents`。谁把调用删掉，这里先红。
    """

    source = inspect.getsource(getattr(AnalyticsApplication, method_name))

    assert "_with_hierarchy_parents(" in source
