"""建模期录入的语义元数据必须真的到达模型。

复刻审计（2026-08-26，上游 supersonic@af08d86）发现的缺口形状不是「字段少」，
而是「字段声明了、也无损往返了，但编译期不写、问数期读不到」:

- MetricSpec.format 字段在、prompt 在读(parser.py)，编译期零写入点，恒为 None。
  对应上游 PromptHelper 渲染的 FORMAT 'PERCENT'。百分比指标(已乘/未乘 100)
  模型无从判断，占比类问题容易差 100 倍。
- DimensionSpec 根本没有 data_type 字段。对应上游 DATATYPE 'ARRAY'，
  ARRAY/MAP/JSON 列上模型会生成普通 = 比较。
"""

from __future__ import annotations

from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.query.parser import _dimension_payload


def _catalog_variant(sales_catalog, *, metric_format=None, dimension_data_type=None):
    """在既有 sales_catalog 上改一个字段,其余保持原样。"""

    metrics = tuple(
        item.model_copy(update={"data_format_type": metric_format})
        if item.name == "净收入"
        else item
        for item in sales_catalog.metrics
    )
    dimensions = tuple(
        item.model_copy(update={"data_type": dimension_data_type})
        if item.name == "区域"
        else item
        for item in sales_catalog.dimensions
    )
    return sales_catalog.model_copy(update={"metrics": metrics, "dimensions": dimensions})


def _metric_by_name(release, name: str):
    return next(item for item in release.metrics if item.name == name)


def _dimension_by_name(release, name: str):
    return next(item for item in release.dimensions if item.name == name)


def test_metric_data_format_type_survives_compilation(sales_catalog) -> None:
    """建模时选的展示格式必须编译进查询投影,否则 prompt 恒读到 None。"""

    catalog = _catalog_variant(sales_catalog, metric_format="PERCENT")
    release = compile_semantic_catalog(catalog)

    assert _metric_by_name(release, "净收入").format == "PERCENT"


def test_metric_without_data_format_type_stays_none(sales_catalog) -> None:
    """没配格式的指标不得凭空造一个。"""

    release = compile_semantic_catalog(sales_catalog)
    assert _metric_by_name(release, "净收入").format is None


def test_dimension_data_type_survives_compilation(sales_catalog) -> None:
    """维度的物理数据类型要能被模型看到:ARRAY/JSON 列不能当普通列比较。"""

    catalog = _catalog_variant(sales_catalog, dimension_data_type="ARRAY")
    release = compile_semantic_catalog(catalog)

    assert _dimension_by_name(release, "区域").data_type == "ARRAY"


def test_dimension_data_type_reaches_the_prompt(sales_catalog) -> None:
    """编译进去还不够,必须真的出现在送给模型的维度清单里。"""

    catalog = _catalog_variant(sales_catalog, dimension_data_type="ARRAY")
    release = compile_semantic_catalog(catalog)
    payload = _dimension_payload(release, release.datasets[0])

    entry = next(item for item in payload if item["name"] == "区域")
    assert entry["data_type"] == "ARRAY"


def test_dimension_falls_back_to_the_physical_column_type(sales_catalog) -> None:
    """维度没单独声明类型时回落物理列类型,对齐上游 DataSetSchemaBuilder 的缺失回退。

    FieldSpec.data_type 有默认值,所以回落后总有类型可给模型;真正需要防的是
    「有类型却不告诉模型」,而不是「凑一个空键」。
    """

    release = compile_semantic_catalog(sales_catalog)
    payload = _dimension_payload(release, release.datasets[0])

    entry = next(item for item in payload if item["name"] == "区域")
    field = next(f for f in release.fields if f.id == _dimension_by_name(release, "区域").field_id)
    assert entry["data_type"] == field.data_type
