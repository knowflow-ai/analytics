"""别名是这个取值的名字，不是另一个取值。

客户实机：业务词典里有一条 Term「业务招待费」（别名 招待费、请客费），声明关联
维度「科目名称」；取值 `销售费用-业务招待费` 也带着别名「业务招待费」。问
「2024年2月招待费是多少」，模型完全按我们给它的词典办事，写出
``"科目名称" = '业务招待费'``——而**库里只有原始取值**，于是零行，界面说
「没有查到数据」，用户读到的是一句关于他自己业务的假话。

建模者声明别名的意思就是「这个说法指的是这个取值」。兑现它是查表不是猜。
"""

from __future__ import annotations

# `semantic.s2sql_translator` 与 `query` 互相 import：先落 query 这一侧，
# 否则单独跑这个文件会撞上半初始化的模块（与 test_textual_s2sql_pipeline 同序）。
import knowflow_analytics.query  # noqa: F401
from knowflow_analytics.contracts import DimensionValueSpec
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator


def _with_values(sales_release, values):
    return sales_release.model_copy(update={"dimension_values": values})


def _bound(release, predicate: str) -> set[str]:
    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=f'SELECT SUM("净收入") FROM "销售经营" WHERE {predicate}',
    )
    return set(translated.physical_query.parameters.values())


def test_an_alias_is_filtered_as_the_published_value(sales_release) -> None:
    """夹具里「东区」是「华东」的别名。写别名就该筛到华东上。"""

    assert _bound(sales_release, "\"区域\" = '东区'") == {"华东"}


def test_an_alias_inside_an_in_list_is_resolved_too(sales_release) -> None:
    assert _bound(sales_release, "\"区域\" IN ('东区', '华南')") == {"华东", "华南"}


def test_a_published_value_is_never_rewritten(sales_release) -> None:
    """字面量本身就是合法取值时一个字不动——哪怕它恰好也是别的取值的别名。"""

    values = (
        DimensionValueSpec(
            id="east", dimension_id="region", value="华东", display_name="华东", aliases=("华南",)
        ),
        DimensionValueSpec(id="south", dimension_id="region", value="华南", display_name="华南"),
    )

    assert _bound(_with_values(sales_release, values), "\"区域\" = '华南'") == {"华南"}


def test_an_alias_pointing_at_two_values_is_left_alone(sales_release) -> None:
    """指向多个取值就不是查表而是猜。歧义有它自己的机制，不在这里替它决定。"""

    values = (
        DimensionValueSpec(
            id="east", dimension_id="region", value="华东", display_name="华东", aliases=("沿海",)
        ),
        DimensionValueSpec(
            id="south", dimension_id="region", value="华南", display_name="华南", aliases=("沿海",)
        ),
    )

    assert _bound(_with_values(sales_release, values), "\"区域\" = '沿海'") == {"沿海"}


def test_an_alias_of_another_dimension_does_not_leak_across(sales_release) -> None:
    """别名按维度分册。别的维度上的同名说法不该改写这里。"""

    values = (
        DimensionValueSpec(
            id="east", dimension_id="region", value="华东", display_name="华东", aliases=("东区",)
        ),
    )

    assert _bound(_with_values(sales_release, values), "\"渠道\" = '东区'") == {"东区"}


def test_a_disabled_value_lends_no_name(sales_release) -> None:
    values = (
        DimensionValueSpec(
            id="east",
            dimension_id="region",
            value="华东",
            display_name="华东",
            aliases=("东区",),
            enabled=False,
        ),
    )

    assert _bound(_with_values(sales_release, values), "\"区域\" = '东区'") == {"东区"}
