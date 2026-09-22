"""按父节点筛就要含下级。

客户实机：「2024年销售费用总共多少」，模型写出完全合规的
``"科目名称" = '销售费用' AND "年份" = 2024``，六道治理关全绿、执行成功，
返回一行 `[None]`——因为分录只记在末级科目上（销售费用-差旅费、
销售费用-业务招待费），父科目自己一条分录都没有。会计上父科目余额本来就是
子科目之和，只拿它自己那一条，答案必错。

展开依据是建模者声明的层级（`DimensionValueSpec.parent_value`），不是名字前缀
这种巧合：`销售费用-差旅费` 恰好以 `销售费用` 开头是这家客户的命名习惯。
"""

from __future__ import annotations

# `semantic.s2sql_translator` 与 `query` 互相 import：先落 query 这一侧，
# 否则单独跑这个文件会撞上半初始化的模块（与 test_textual_s2sql_pipeline 同序）。
import knowflow_analytics.query  # noqa: F401
from knowflow_analytics.contracts import DimensionValueSpec, FilterOperator
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator


def _with_region_tree(sales_release):
    """华东 → 华东-上海 → 华东-上海-浦东；华南是棵独立的叶子。"""

    values = (
        DimensionValueSpec(id="east", dimension_id="region", value="华东", display_name="华东"),
        DimensionValueSpec(
            id="east_sh",
            dimension_id="region",
            value="华东-上海",
            display_name="华东-上海",
            parent_value="华东",
        ),
        DimensionValueSpec(
            id="east_sh_pd",
            dimension_id="region",
            value="华东-上海-浦东",
            display_name="华东-上海-浦东",
            parent_value="华东-上海",
        ),
        DimensionValueSpec(id="south", dimension_id="region", value="华南", display_name="华南"),
    )
    return sales_release.model_copy(update={"dimension_values": values})


def _translate(release, predicate: str):
    return S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=f'SELECT SUM("净收入") FROM "销售经营" WHERE {predicate}',
    )


def test_filtering_a_parent_reaches_the_whole_subtree(sales_release) -> None:
    translated = _translate(_with_region_tree(sales_release), "\"区域\" = '华东'")

    # 取值是参数化送下去的，断言打在参数上——SQL 文本里只有 :p0。
    bound = set(translated.physical_query.parameters.values())
    # 只展开一层会漏掉孙子节点，而那一层往往正是真正记账的地方。
    # 父节点自己也留着：它可能有自己的记录，丢掉就少算。
    assert bound == {"华东", "华东-上海", "华东-上海-浦东"}


def test_the_expansion_is_visible_on_the_answer(sales_release) -> None:
    """chip 只说「区域 = 华东」而 SQL 查了三个取值，就是口径不一致。

    但把展开后的取值全列进 chip 同样不行：真实科目表里一个父科目底下可能几十个
    子科目，那张卡会被撑爆。所以缀一个条数。
    """

    translated = _translate(_with_region_tree(sales_release), "\"区域\" = '华东'")

    assert translated.hierarchy_rollups == (("region", "华东", 2),)

    interpretation = AnalyticsQueryService._interpretation(
        _with_region_tree(sales_release),
        translated.audit_query,
        (),
        translated.hierarchy_rollups,
    )
    assert "区域 = 华东（含下级 2 个）" in interpretation.filters


def test_a_filter_that_was_not_rolled_up_reads_exactly_as_written(sales_release) -> None:
    translated = _translate(_with_region_tree(sales_release), "\"区域\" = '华南'")

    interpretation = AnalyticsQueryService._interpretation(
        _with_region_tree(sales_release),
        translated.audit_query,
        (),
        translated.hierarchy_rollups,
    )
    assert "区域 = 华南" in interpretation.filters
    assert not any("含下级" in item for item in interpretation.filters)


def test_a_leaf_value_is_left_exactly_as_written(sales_release) -> None:
    translated = _translate(_with_region_tree(sales_release), "\"区域\" = '华东-上海-浦东'")

    region = next(item for item in translated.audit_query.filters if item.dimension_id == "region")
    assert region.operator is FilterOperator.EQ
    assert region.value == "华东-上海-浦东"


def test_a_value_outside_any_hierarchy_is_left_alone(sales_release) -> None:
    translated = _translate(_with_region_tree(sales_release), "\"区域\" = '华南'")

    region = next(item for item in translated.audit_query.filters if item.dimension_id == "region")
    assert region.operator is FilterOperator.EQ
    assert region.value == "华南"


def test_a_release_without_any_declared_parent_is_untouched(sales_release) -> None:
    """绝大多数项目没有声明层级，这条规则对它们必须根本不存在。"""

    translated = _translate(sales_release, "\"区域\" = '华东'")

    region = next(item for item in translated.audit_query.filters if item.dimension_id == "region")
    assert region.operator is FilterOperator.EQ
    assert region.value == "华东"


def test_a_disabled_child_is_not_pulled_in(sales_release) -> None:
    """停用的取值不该因为挂在树上就被查回来。"""

    release = _with_region_tree(sales_release)
    values = tuple(
        item.model_copy(update={"enabled": False}) if item.id == "east_sh_pd" else item
        for item in release.dimension_values
    )
    translated = _translate(
        release.model_copy(update={"dimension_values": values}), "\"区域\" = '华东'"
    )

    assert set(translated.physical_query.parameters.values()) == {"华东", "华东-上海"}
