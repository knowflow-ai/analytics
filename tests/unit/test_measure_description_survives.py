"""度量的业务口径必须一路活到 Release：这是问数准确率的主导变量。

实机发现（Release rel_72ffa832，逐字段核对）：4 个维度字段、2 个时间字段全有描述，
3 个真正的业务度量（净金额、退款金额、探针2金额）描述**全部为空**。而字段命名的
系统提示词明确要求「度量要写清口径（如"不含退款"）」——AI 被要求写了，写出来的
东西在管道里被丢掉。

丢在三个地方，每一处单独看都像小疏漏，合起来是"AI 建模写不出指标定义"：

1. `MeasureContract` 没有 description 字段——维度合同有，度量合同没有，无处可放；
2. 由度量创建指标时 `description=measure.name`，把定义写成同义反复，还让
   「定义已填」看起来是真的；
3. 编译期 `description=dimension.description if dimension is not None else ""`，
   只从维度一侧取，度量字段一律拿到空串。

后果链：指标定义为空 → 别名提示词锚在空描述上、又被「不得编造与描述无关的业务
含义」约束 → 只能产出名字的同义变体（实测 6 个指标无一例外）→ 用户说的「销售额」
永远匹配不上 → 走完整套弱指标裁决与澄清，或者静默选错指标。
"""

from __future__ import annotations

from knowflow_analytics.contracts import FieldKind
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    MeasureContract,
    ModelFieldContract,
)
from knowflow_analytics.modeling.catalog_editor import upsert_model_aggregate

_KOU_JING = "订单实收金额，已扣退款与优惠"


def _orders(catalog):
    return next(item for item in catalog.models if item.id == "orders")


def _with_measure_description(catalog, description: str):
    """把 orders 的净金额度量换成带口径的版本。"""

    model = _orders(catalog)
    detail = model.model_detail
    measures = tuple(
        item.model_copy(update={"description": description})
        if item.expr == "net_amount"
        else item
        for item in detail.measures
    )
    updated = model.model_copy(
        update={"model_detail": detail.model_copy(update={"measures": measures})}
    )
    return catalog.model_copy(
        update={
            "models": tuple(
                updated if item.id == "orders" else item for item in catalog.models
            )
        }
    )


def _with_a_new_measure(catalog, *, description: str):
    """在 orders 上新增一列与一个待物化的度量。

    比改动既有度量干净：既有指标按名字引用度量，改名或改 biz_name 会打断它们。
    新列不被任何东西引用，只用来观察"新建指标时定义从哪来"。
    """

    model = _orders(catalog)
    detail = model.model_detail
    updated = model.model_copy(
        update={
            "model_detail": detail.model_copy(
                update={
                    "fields": (
                        *detail.fields,
                        ModelFieldContract(field_name="gross_margin", data_type="numeric"),
                    ),
                    "measures": (
                        *detail.measures,
                        MeasureContract(
                            name="毛利",
                            agg="SUM",
                            expr="gross_margin",
                            biz_name="gross_margin",
                            is_create_metric=1,
                            description=description,
                        ),
                    ),
                }
            )
        }
    )
    return catalog.model_copy(
        update={
            "models": tuple(
                updated if item.id == "orders" else item for item in catalog.models
            )
        }
    )


class TestTheContractCanHoldIt:
    def test_measure_contract_has_a_description(self):
        """维度合同一直有 description，度量合同没有——AI 写的口径无处可放。"""

        measure = MeasureContract(
            name="净金额", agg="SUM", expr="net_amount", biz_name="net_amount",
            description=_KOU_JING,
        )

        assert measure.description == _KOU_JING

    def test_it_defaults_to_empty_so_existing_catalogs_still_load(self):
        """存量 Catalog 没有这个键，加载不能因此失败。"""

        measure = MeasureContract(
            name="净金额", agg="SUM", expr="net_amount", biz_name="net_amount"
        )

        assert measure.description == ""


class TestItSurvivesCompilation:
    def test_a_measure_field_keeps_its_description(self, sales_catalog):
        """编译期此前只从维度一侧取描述，度量字段一律拿到空串。"""

        release = compile_semantic_catalog(
            _with_measure_description(sales_catalog, _KOU_JING)
        )
        field = next(item for item in release.fields if item.column == "net_amount")

        assert field.kind is FieldKind.MEASURE
        assert field.description == _KOU_JING

    def test_dimension_descriptions_are_untouched(self, sales_catalog):
        """修的是"度量取不到"，不是改描述的来源。"""

        release = compile_semantic_catalog(sales_catalog)
        dimensions = [
            item for item in release.fields if item.kind is FieldKind.DIMENSION
        ]

        assert dimensions  # 前提：夹具里确实有维度字段
        for item in dimensions:
            original = next(
                candidate
                for model in sales_catalog.models
                for candidate in model.model_detail.dimensions
                if candidate.expr == item.column
            )
            assert item.description == original.description

    def test_a_measure_without_a_description_stays_empty(self, sales_catalog):
        """没写口径就是空，不许拿名字凑数——凑出来的"已填"比空着更糟。"""

        release = compile_semantic_catalog(sales_catalog)
        field = next(item for item in release.fields if item.column == "net_amount")

        assert field.description == ""


class TestTheMetricDefinitionIsNotATautology:
    def test_a_metric_built_from_a_measure_inherits_the_business_meaning(
        self, sales_catalog
    ):
        """指标定义原先固定写成 measure.name，等于把名字复读一遍。"""

        materialized = upsert_model_aggregate(
            sales_catalog,
            _orders(_with_a_new_measure(sales_catalog, description=_KOU_JING)),
        )

        metric = next(
            item for item in materialized.metrics if item.biz_name == "gross_margin"
        )
        assert metric.description == _KOU_JING
        assert metric.description != metric.name

    def test_no_description_means_no_definition_not_the_name(self, sales_catalog):
        """口径没写时定义留空。写成名字会让「缺定义」这个真实风险彻底隐形。"""

        materialized = upsert_model_aggregate(
            sales_catalog,
            _orders(_with_a_new_measure(sales_catalog, description="")),
        )

        metric = next(
            item for item in materialized.metrics if item.biz_name == "gross_margin"
        )
        assert metric.description == ""


def test_the_business_meaning_reaches_the_published_metric(sales_catalog):
    """端到端：口径要一路到 Release 的 MetricSpec，那才是问数真正读的那一层。

    中间任何一段断掉，前面几条测试都还是绿的，但线上依然拿不到口径——
    这条是把整条链当成一个东西来验。
    """

    materialized = upsert_model_aggregate(
        sales_catalog,
        _orders(_with_a_new_measure(sales_catalog, description=_KOU_JING)),
    )

    release = compile_semantic_catalog(materialized)

    metric = next(item for item in release.metrics if item.name == "毛利")
    assert metric.description == _KOU_JING
