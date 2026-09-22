"""受治理取值进 prompt 时要带业务名，不能只给原始值。

实机 D008「2024年1月甲公司的应收账款是多少」：`账款方向` 存的是 1/2，业务含义
「应收/应付」写在取值的 ``display_name`` 与别名里。prompt 此前只给 ``raw_value``，
模型看到的是两个裸数字，于是去找别的、字面像的维度——写出
``报表项目='应收账款'``，六道治理关全绿、返回一个看起来正常的数，而口径是错的。

两者相同时不重复：绝大多数取值本来就是业务名，重复只会让 prompt 变长。

**别名不进这里**。第一版把取值别名也无条件加了进来，以为是空操作——冻结基线上
2 处回归（D004 correct→empty、D018 correct→refuse），而基线自身噪声是 0/20。
别名是新增的模型可见文本，不是"换个写法"；用户说的别名本来就由召回负责命中。
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime

from knowflow_analytics.contracts import DimensionValueSpec
from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import LlmS2SqlParser


def _values(release, index, question: str) -> list[dict[str, object]]:
    dataset = release.datasets[0]
    mapping = SemanticMapper().map(
        question=question,
        dataset_id=dataset.id,
        index=index,
        mode=MapMode.ALL,
    )
    content = LlmS2SqlParser._messages(
        question,
        release,
        dataset,
        mapping,
        now=datetime(2026, 8, 20, tzinfo=UTC),
    )[1]["content"]
    line = next((item for item in content.splitlines() if item.startswith("values=")), None)
    assert line is not None, "prompt 里没有 values= 这一段，值证据根本没到模型手里"
    return ast.literal_eval(line.removeprefix("values="))


def _renamed(release):
    """给「华东」一个与原始值不同的业务名。

    不直接把原始值改成 1（`账款方向` 的真实形状）是因为 ``raw_value`` 来自**索引**，
    而索引是按原 release 建的：改了 release 的取值，mapper 命中的仍是旧值，
    两边对不上，测到的就不是这条机制。这里测的是机制本身——
    ``display_name`` 与原始值不同时，业务名必须一起给。
    """

    original = release.dimension_values[0]
    return release.model_copy(
        update={
            "dimension_values": (
                DimensionValueSpec(
                    id=original.id,
                    dimension_id=original.dimension_id,
                    value=original.value,
                    display_name="华东大区",
                    aliases=("东区",),
                ),
                *release.dimension_values[1:],
            )
        }
    )


def test_a_value_that_is_already_its_own_business_name_is_not_repeated(
    sales_release, sales_index
):
    """``display_name`` 与原始值相同就不重复——prompt 的每个字都要有它的理由。"""

    entries = _values(sales_release, sales_index, "华东的净收入")

    east = next(item for item in entries if item["raw_value"] == "华东")
    assert set(east) == {"field_name", "raw_value"}, "相同就不该多给一个键"


def test_a_value_whose_business_name_differs_carries_it(sales_release, sales_index):
    """业务名与原始值不同时必须一起给——否则模型看到的只有原始值。

    真实形状是 `账款方向` 存 1/2、业务名「应收/应付」，模型只看到两个裸数字，
    于是去找别的、字面像的维度（实机 D008：写出 `报表项目='应收账款'`）。
    """

    entries = _values(_renamed(sales_release), sales_index, "华东的净收入")

    east = next(item for item in entries if item["raw_value"] == "华东")
    assert east["business_name"] == "华东大区"


def test_the_entry_shape_is_stable(sales_release, sales_index):
    """键名是合同：下游诊断与本测试都按它读，多一个少一个都要有人先想清楚。"""

    entries = _values(_renamed(sales_release), sales_index, "华东的净收入")

    assert entries
    for item in entries:
        assert set(item) <= {"field_name", "raw_value", "business_name"}
        assert "field_name" in item
        assert "raw_value" in item
