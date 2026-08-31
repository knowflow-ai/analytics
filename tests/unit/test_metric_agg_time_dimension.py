"""指标各自的聚合时间轴。

同一个模型上「收入按支付时间、订单数按下单时间」是常态。补齐前只有数据集级
一个默认时间维度，所有指标共用——不报错，只会给出一个看起来正常的错数字。
这是全链路唯一会静默出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from knowflow_analytics.contracts import (
    Aggregation,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    MetricKind,
    MetricSpec,
    SemanticRelease,
)
from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.query.errors import ClarificationSignal
from knowflow_analytics.query.parser import _apply_time_filters

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _two_time_axes(release: SemanticRelease) -> SemanticRelease:
    """在 fixture 上加一条「支付时间」，构造两个时间轴的真实场景。"""

    order_date = next(item for item in release.dimensions if item.semantic_type == "time")
    paid_field = FieldSpec(
        id="orders.paid_at",
        model_id="orders",
        name="支付时间",
        column="paid_at",
        data_type="timestamp",
        kind=FieldKind.DIMENSION,
        dimension_type="time",
    )
    paid_dimension = DimensionSpec(
        id="paid_at",
        name="支付时间",
        model_id="orders",
        field_id=paid_field.id,
        semantic_type="time",
    )
    dataset = release.datasets[0]
    return release.model_copy(
        update={
            "fields": (*release.fields, paid_field),
            "dimensions": (*release.dimensions, paid_dimension),
            "datasets": (
                dataset.model_copy(
                    update={
                        "dimension_ids": (*dataset.dimension_ids, paid_dimension.id),
                        "default_time_dimension_id": order_date.id,
                    }
                ),
                *release.datasets[1:],
            ),
        }
    )


def _declare(release: SemanticRelease, **by_metric: str) -> SemanticRelease:
    return release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"agg_time_dimension_id": by_metric[item.id]})
                if item.id in by_metric
                else item
                for item in release.metrics
            )
        }
    )


def _plan(release: SemanticRelease, metric_ids: list[str], mapped: list[str] | None = None):
    filters, _ = _apply_time_filters(
        question="最近 7 天的情况",
        release=release,
        dataset=release.datasets[0],
        mapped_dimension_ids=mapped or [],
        selected_metric_ids=metric_ids,
        existing_filters=[],
        now=NOW,
    )
    used = {item.dimension_id for item in filters}
    assert len(used) == 1, "时间过滤必须落在唯一一个时间维度上"
    return used.pop()


def test_metric_time_axis_beats_the_dataset_default(sales_release) -> None:
    """补齐前这里会用数据集默认的「订单日期」，给出一个看起来正常的错数字。"""

    release = _declare(_two_time_axes(sales_release), net_revenue="paid_at")
    assert _plan(release, ["net_revenue"]) == "paid_at"


def test_dataset_default_still_applies_when_the_metric_declares_nothing(sales_release) -> None:
    """未声明的指标行为必须与补齐前完全一致。"""

    release = _two_time_axes(sales_release)
    assert release.datasets[0].default_time_dimension_id == "order_date"
    assert _plan(release, ["order_count"]) == "order_date"


def test_a_time_dimension_named_in_the_question_still_wins(sales_release) -> None:
    """用户明确按下单时间问，就不能被指标声明的支付时间改写。"""

    release = _declare(_two_time_axes(sales_release), net_revenue="paid_at")
    assert _plan(release, ["net_revenue"], mapped=["order_date"]) == "order_date"


def test_conflicting_metric_time_axes_ask_instead_of_guessing(sales_release) -> None:
    """「本月收入和订单数」跨两条时间轴,任选一个都会错——必须问。"""

    release = _declare(
        _two_time_axes(sales_release),
        net_revenue="paid_at",
        order_count="order_date",
    )
    with pytest.raises(ClarificationSignal) as excinfo:
        _plan(release, ["net_revenue", "order_count"])
    assert excinfo.value.code == "AMBIGUOUS_TIME_DIMENSION"
    assert set(excinfo.value.element_ids) == {"paid_at", "order_date"}


def test_same_axis_on_several_metrics_is_not_a_conflict(sales_release) -> None:
    release = _declare(
        _two_time_axes(sales_release),
        net_revenue="paid_at",
        refund_amount="paid_at",
    )
    assert _plan(release, ["net_revenue", "refund_amount"]) == "paid_at"


def test_derived_metric_cannot_declare_its_own_time_axis() -> None:
    """派生指标的时间轴由依赖的原子指标决定,再声明一次无法判断以哪个为准。"""

    with pytest.raises(ValidationError):
        MetricSpec(
            id="ratio",
            name="退款率",
            model_id="orders",
            kind=MetricKind.DERIVED,
            formula="{refund_amount} / {net_revenue}",
            agg_time_dimension_id="paid_at",
        )


def test_atomic_metric_accepts_the_declaration() -> None:
    metric = MetricSpec(
        id="net_revenue",
        name="净收入",
        model_id="orders",
        field_id="orders.net_amount",
        aggregation=Aggregation.SUM,
        agg_time_dimension_id="paid_at",
    )
    assert metric.agg_time_dimension_id == "paid_at"


def _with_declaration(catalog, metric_id: str, dimension_id: str | None):
    return catalog.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"agg_time_dimension_id": dimension_id})
                if item.id == metric_id
                else item
                for item in catalog.metrics
            )
        }
    )


def test_compiler_carries_a_valid_declaration_into_the_release(sales_catalog) -> None:
    time_dimension = next(
        item for item in sales_catalog.dimensions if item.type.lower().startswith("time")
    )
    catalog = _with_declaration(sales_catalog, "net_revenue", time_dimension.id)
    release = compile_semantic_catalog(catalog)
    compiled = next(item for item in release.metrics if item.id == "net_revenue")
    assert compiled.agg_time_dimension_id == time_dimension.id


def test_compiler_rejects_an_unknown_time_dimension(sales_catalog) -> None:
    """静默忽略会让用户以为已经设上了,而问数仍在用数据集默认值。"""

    catalog = _with_declaration(sales_catalog, "net_revenue", "no_such_dimension")
    with pytest.raises(SemanticValidationError) as excinfo:
        compile_semantic_catalog(catalog)
    assert excinfo.value.code == "AGG_TIME_DIMENSION_INVALID"


def test_compiler_rejects_a_non_time_dimension(sales_catalog) -> None:
    categorical = next(
        item for item in sales_catalog.dimensions if not item.type.lower().startswith("time")
    )
    catalog = _with_declaration(sales_catalog, "net_revenue", categorical.id)
    with pytest.raises(SemanticValidationError) as excinfo:
        compile_semantic_catalog(catalog)
    assert excinfo.value.code == "AGG_TIME_DIMENSION_INVALID"


def test_catalog_without_the_field_compiles_exactly_as_before(sales_catalog) -> None:
    """存量目录没有这个键,必须原样编译通过。"""

    release = compile_semantic_catalog(sales_catalog)
    assert all(item.agg_time_dimension_id is None for item in release.metrics)
