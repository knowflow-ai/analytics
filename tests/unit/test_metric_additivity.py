from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowflow_analytics.contracts import (
    Aggregation,
    DimensionSpec,
    MetricKind,
    MetricSpec,
    NonAdditiveDimension,
)


def _balance_metric(**overrides: object) -> MetricSpec:
    payload: dict[str, object] = {
        "id": "member_balance",
        "name": "会员余额",
        "model_id": "members",
        "field_id": "members.balance",
        "aggregation": Aggregation.SUM,
    }
    payload.update(overrides)
    return MetricSpec(**payload)  # type: ignore[arg-type]


def test_metric_is_additive_by_default() -> None:
    """既有模型不带该声明，行为必须与补齐前完全一致。"""

    metric = _balance_metric()
    assert metric.non_additive_dimension is None
    assert metric.is_additive_along("stat_date") is True


def test_semi_additive_metric_rejects_summing_along_the_declared_dimension() -> None:
    """余额类度量按门店可加、按时间不可加：跨时间求和会把 90 天余额相加。"""

    metric = _balance_metric(
        non_additive_dimension=NonAdditiveDimension(
            dimension_id="stat_date",
            window_choice=Aggregation.MAX,
        )
    )
    assert metric.is_additive_along("stat_date") is False
    assert metric.is_additive_along("store_code") is True


def test_window_choice_only_accepts_a_boundary_aggregation() -> None:
    """窗口取值只能取期末/期初，SUM 之类会重新引入被禁止的相加。"""

    with pytest.raises(ValidationError):
        _balance_metric(
            non_additive_dimension=NonAdditiveDimension(
                dimension_id="stat_date",
                window_choice=Aggregation.SUM,
            )
        )


def test_non_additive_declaration_requires_an_additive_aggregation() -> None:
    """COUNT_DISTINCT 等本就不随维度相加的聚合无需该声明，避免语义重复。"""

    with pytest.raises(ValidationError):
        _balance_metric(
            aggregation=Aggregation.MAX,
            non_additive_dimension=NonAdditiveDimension(
                dimension_id="stat_date",
                window_choice=Aggregation.MAX,
            ),
        )


def test_derived_metric_cannot_declare_non_additivity() -> None:
    """派生指标的可加性由其依赖指标决定，不能在此处二次声明。"""

    with pytest.raises(ValidationError):
        MetricSpec(
            id="balance_growth",
            name="余额增长",
            model_id="members",
            kind=MetricKind.DERIVED,
            formula="{member_balance} - 1",
            non_additive_dimension=NonAdditiveDimension(
                dimension_id="stat_date",
                window_choice=Aggregation.MAX,
            ),
        )


def test_window_groupings_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        NonAdditiveDimension(
            dimension_id="stat_date",
            window_choice=Aggregation.MAX,
            window_groupings=("user_id", "user_id"),
        )


def test_window_groupings_cannot_repeat_the_non_additive_dimension() -> None:
    with pytest.raises(ValidationError):
        NonAdditiveDimension(
            dimension_id="stat_date",
            window_choice=Aggregation.MAX,
            window_groupings=("stat_date",),
        )


def test_time_granularity_defaults_to_unset_and_survives_the_dimension_spec() -> None:
    """DTO 里早有 timeGranularity，此前编译时被丢弃。"""

    assert (
        DimensionSpec(
            id="stat_date",
            name="统计日期",
            model_id="members",
            field_id="members.stat_date",
            semantic_type="time",
        ).time_granularity
        is None
    )

    dimension = DimensionSpec(
        id="stat_date",
        name="统计日期",
        model_id="members",
        field_id="members.stat_date",
        semantic_type="time",
        time_granularity="day",
    )
    assert dimension.time_granularity == "day"


def test_time_granularity_requires_a_time_dimension() -> None:
    """非时间维度带粒度是建模错误，放行会让问数按不存在的粒度分组。"""

    with pytest.raises(ValidationError):
        DimensionSpec(
            id="city",
            name="城市",
            model_id="members",
            field_id="members.city",
            semantic_type="categorical",
            time_granularity="day",
        )


@pytest.mark.parametrize("granularity", ["day", "week", "month", "quarter", "year"])
def test_supported_time_granularities(granularity: str) -> None:
    assert (
        DimensionSpec(
            id="stat_date",
            name="统计日期",
            model_id="members",
            field_id="members.stat_date",
            semantic_type="time",
            time_granularity=granularity,
        ).time_granularity
        == granularity
    )


def test_rejects_a_granularity_outside_the_governed_set() -> None:
    with pytest.raises(ValidationError):
        DimensionSpec(
            id="stat_date",
            name="统计日期",
            model_id="members",
            field_id="members.stat_date",
            semantic_type="time",
            time_granularity="fortnight",
        )


def test_compiler_carries_time_granularity_from_the_upstream_dto() -> None:
    """编译器此前丢弃 type_params，粒度到不了 DimensionSpec。"""

    import json
    from pathlib import Path

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

    fixture = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for dimension in payload["dimensions"]:
        if dimension["id"] == "dimension_order_time":
            dimension["typeParams"] = {"isPrimary": "true", "timeGranularity": "month"}

    release = compile_semantic_catalog(SemanticCatalog.model_validate(payload))
    compiled = {item.id: item for item in release.dimensions}
    assert compiled["dimension_order_time"].time_granularity == "month"
    # 非时间维度不得携带粒度，否则会触发合同校验。
    assert compiled["dimension_channel"].time_granularity is None


def test_compiler_ignores_a_granularity_outside_the_governed_set() -> None:
    """上游是自由字符串；陌生取值应降级为未声明，而不是让整个 Revision 编译失败。"""

    import json
    from pathlib import Path

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

    fixture = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    for dimension in payload["dimensions"]:
        if dimension["id"] == "dimension_order_time":
            dimension["typeParams"] = {"isPrimary": "true", "timeGranularity": "hour"}

    release = compile_semantic_catalog(SemanticCatalog.model_validate(payload))
    compiled = {item.id: item for item in release.dimensions}
    assert compiled["dimension_order_time"].time_granularity is None


def _fixture_payload() -> dict:
    import json
    from pathlib import Path

    fixture = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _compile(payload: dict):
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

    return compile_semantic_catalog(SemanticCatalog.model_validate(payload))


def test_non_additive_declaration_travels_through_the_catalog_ext_slot() -> None:
    """上游 MetricContract.ext 是既有扩展位；不新造非对齐字段就能让用户设置该声明。"""

    payload = _fixture_payload()
    for metric in payload["metrics"]:
        if metric["id"] == "metric_revenue":
            metric["ext"] = {
                "nonAdditiveDimension": {
                    "dimensionId": "dimension_order_time",
                    "windowChoice": "max",
                }
            }

    compiled = {item.id: item for item in _compile(payload).metrics}
    declaration = compiled["metric_revenue"].non_additive_dimension
    assert declaration is not None
    assert declaration.dimension_id == "dimension_order_time"
    assert declaration.window_choice.value == "max"


def test_metric_without_the_ext_slot_stays_additive() -> None:
    compiled = {item.id: item for item in _compile(_fixture_payload()).metrics}
    assert compiled["metric_revenue"].non_additive_dimension is None


def test_a_malformed_ext_declaration_is_rejected_at_compile_time() -> None:
    """声明写错必须显式失败：静默忽略会让用户以为已经设上了。"""

    from knowflow_analytics.errors import SemanticValidationError

    payload = _fixture_payload()
    for metric in payload["metrics"]:
        if metric["id"] == "metric_revenue":
            metric["ext"] = {"nonAdditiveDimension": {"windowChoice": "max"}}

    with pytest.raises(SemanticValidationError):
        _compile(payload)


def test_ext_declaration_must_reference_a_dimension_that_exists() -> None:
    from knowflow_analytics.errors import SemanticValidationError

    payload = _fixture_payload()
    for metric in payload["metrics"]:
        if metric["id"] == "metric_revenue":
            metric["ext"] = {
                "nonAdditiveDimension": {
                    "dimensionId": "no_such_dimension",
                    "windowChoice": "max",
                }
            }

    with pytest.raises(SemanticValidationError):
        _compile(payload)


def test_ext_declaration_cannot_bypass_the_aggregation_rule() -> None:
    """model_copy 不跑校验器，因此这里必须显式复核合同规则。

    metric_order_count 是 COUNT_DISTINCT，本就不随维度相加，不允许携带该声明。
    """

    from knowflow_analytics.errors import SemanticValidationError

    payload = _fixture_payload()
    for metric in payload["metrics"]:
        if metric["id"] == "metric_order_count":
            metric["ext"] = {
                "nonAdditiveDimension": {
                    "dimensionId": "dimension_order_time",
                    "windowChoice": "max",
                }
            }

    with pytest.raises(SemanticValidationError):
        _compile(payload)
