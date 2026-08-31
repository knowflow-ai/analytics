"""维度层级：同一把尺子由粗到细的一组维度。

模型收到的维度是一张扁平表，「省」和「市」之间没有任何关系。用户说「按地区看」
时它只能在几个都像的维度里挑一个，说「再细一点」时它不知道下一级是什么。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowflow_analytics.contracts import (
    DimensionSpec,
    FieldKind,
    FieldSpec,
    HierarchySpec,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import HierarchyContract
from knowflow_analytics.query.parser import _dimension_payload, _hierarchy_payload


def _with_region_levels(release: SemanticRelease) -> SemanticRelease:
    """在 fixture 上加「省 > 市」两级，构造真实的歧义场景。"""

    extra_fields = []
    extra_dimensions = []
    for column, name in (("province", "省份"), ("city", "城市")):
        field = FieldSpec(
            id=f"orders.{column}",
            model_id="orders",
            name=name,
            column=column,
            kind=FieldKind.DIMENSION,
            dimension_type="categorical",
        )
        extra_fields.append(field)
        extra_dimensions.append(
            DimensionSpec(id=column, name=name, model_id="orders", field_id=field.id)
        )
    dataset = release.datasets[0]
    return release.model_copy(
        update={
            "fields": (*release.fields, *extra_fields),
            "dimensions": (*release.dimensions, *extra_dimensions),
            "hierarchies": (
                HierarchySpec(
                    id="region",
                    model_id="orders",
                    name="行政区划",
                    aliases=("地区", "区域"),
                    levels=("province", "city"),
                ),
            ),
            "datasets": (
                dataset.model_copy(
                    update={"dimension_ids": (*dataset.dimension_ids, "province", "city")}
                ),
                *release.datasets[1:],
            ),
        }
    )


def test_hierarchy_reaches_the_model_schema(sales_release) -> None:
    """层级必须真的送进 prompt——只声明不消费的字段等于不存在。"""

    release = _with_region_levels(sales_release)
    dataset = release.datasets[0]
    names = {str(item["name"]) for item in _dimension_payload(release, dataset)}
    payload = _hierarchy_payload(release, dataset, names)
    assert payload == [
        {
            "name": "行政区划",
            "levels_coarse_to_fine": ["省份", "城市"],
            "aliases": ("地区", "区域"),
        }
    ]


def test_levels_the_model_cannot_see_are_dropped(sales_release) -> None:
    """送一个模型看不见的维度名，只会诱导它引用不存在的字段。"""

    release = _with_region_levels(sales_release)
    dataset = release.datasets[0]
    assert _hierarchy_payload(release, dataset, {"省份"}) == [], "只剩一级不构成层级"


def test_release_without_hierarchies_is_unchanged(sales_release) -> None:
    dataset = sales_release.datasets[0]
    names = {str(item["name"]) for item in _dimension_payload(sales_release, dataset)}
    assert _hierarchy_payload(sales_release, dataset, names) == []


def test_a_single_level_is_not_a_hierarchy() -> None:
    with pytest.raises(ValidationError):
        HierarchySpec(id="region", model_id="orders", name="行政区划", levels=("province",))


def test_repeated_levels_are_rejected() -> None:
    with pytest.raises(ValidationError):
        HierarchySpec(
            id="region", model_id="orders", name="行政区划", levels=("province", "province")
        )


def _with_hierarchy(catalog, levels: tuple[str, ...], model_id: str = "orders"):
    return catalog.model_copy(
        update={
            "hierarchies": (
                HierarchyContract(id="region", model_id=model_id, name="行政区划", levels=levels),
            )
        }
    )


def test_compiler_rejects_an_unknown_level(sales_catalog) -> None:
    """指向不存在维度的层级会让「按地区」继续瞎猜，而用户以为已经配好了。"""

    dimension_id = sales_catalog.dimensions[0].id
    catalog = _with_hierarchy(sales_catalog, (dimension_id, "no_such_dimension"))
    with pytest.raises(SemanticValidationError) as excinfo:
        compile_semantic_catalog(catalog)
    assert excinfo.value.code == "HIERARCHY_LEVEL_INVALID"


def test_compiler_rejects_a_level_from_another_model(sales_catalog) -> None:
    """跨模型分组要先 join，会改变粒度，不是同一把尺子上的刻度。"""

    dimension_id = sales_catalog.dimensions[0].id
    catalog = _with_hierarchy(sales_catalog, (dimension_id, dimension_id + "_x"), "other_model")
    with pytest.raises(SemanticValidationError) as excinfo:
        compile_semantic_catalog(catalog)
    assert excinfo.value.code == "HIERARCHY_LEVEL_INVALID"


def test_catalog_without_hierarchies_compiles_exactly_as_before(sales_catalog) -> None:
    assert compile_semantic_catalog(sales_catalog).hierarchies == ()


def _catalog_with_two_level_hierarchy(sales_catalog):
    model_id = sales_catalog.models[0].id
    levels = tuple(item.id for item in sales_catalog.dimensions if item.model_id == model_id)[:3]
    assert len(levels) >= 3, "fixture 需要至少三个同模型维度"
    return (
        sales_catalog.model_copy(
            update={
                "hierarchies": (
                    HierarchyContract(
                        id="region", model_id=model_id, name="行政区划", levels=levels
                    ),
                )
            }
        ),
        levels,
    )


def test_deleting_a_level_prunes_the_hierarchy_instead_of_breaking_compilation(
    sales_catalog,
) -> None:
    """删掉被层级引用的维度，若不摘掉它，编译期 HIERARCHY_LEVEL_INVALID 会让整个
    Revision 编译不过——用户只是删了一个维度，却发现什么都发布不了。"""

    from knowflow_analytics.modeling.deletion import CatalogDeletionPlanner, ResourceKind

    catalog, levels = _catalog_with_two_level_hierarchy(sales_catalog)
    planner = CatalogDeletionPlanner()
    impact = planner.preview(catalog, resource_kind=ResourceKind.DIMENSION, resource_id=levels[0])
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id=levels[0],
        expected_impact_hash=impact.impact_hash,
    )
    assert updated.hierarchies[0].levels == levels[1:]
    compile_semantic_catalog(updated)  # 必须仍能编译


def test_a_hierarchy_falling_below_two_levels_is_deleted(sales_catalog) -> None:
    from knowflow_analytics.modeling.deletion import CatalogDeletionPlanner, ResourceKind

    model_id = sales_catalog.models[0].id
    levels = tuple(item.id for item in sales_catalog.dimensions if item.model_id == model_id)[:2]
    catalog = sales_catalog.model_copy(
        update={
            "hierarchies": (
                HierarchyContract(id="region", model_id=model_id, name="行政区划", levels=levels),
            )
        }
    )
    planner = CatalogDeletionPlanner()
    impact = planner.preview(catalog, resource_kind=ResourceKind.DIMENSION, resource_id=levels[0])
    updated = planner.apply(
        catalog,
        resource_kind=ResourceKind.DIMENSION,
        resource_id=levels[0],
        expected_impact_hash=impact.impact_hash,
    )
    assert updated.hierarchies == ()
    compile_semantic_catalog(updated)
