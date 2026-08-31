"""下钻白名单退役后，存量目录必须照常加载。

`upstreamCommit` 退役时就是因为存量 payload 带着已删字段、而合同是
``extra="forbid"``，导致全线 INTERNAL_ERROR。Release 逐字存储目录，
`semantic_spec.modeling_catalog` 里 109 处模型和 39 处指标带着这两个键，
所以退役键必须在读取时对称丢弃。
"""

from __future__ import annotations

from pathlib import Path

from knowflow_analytics.modeling.catalog_contracts import (
    MetricContract,
    ModelContract,
    SemanticCatalog,
)

FIXTURE_CATALOG = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"


def _model_payload(**extra: object) -> dict[str, object]:
    return {
        "id": "model_orders",
        "name": "订单",
        "bizName": "orders",
        "modelDetail": {"queryType": "table_query", "tableQuery": "public.orders"},
        **extra,
    }


def test_model_loads_with_the_retired_drill_down_key() -> None:
    model = ModelContract.model_validate(
        _model_payload(drillDownDimensions=[{"dimensionId": "dimension_channel"}])
    )
    assert model.id == "model_orders"
    assert not hasattr(model, "drill_down_dimensions")


def test_model_loads_with_the_retired_snake_case_key() -> None:
    """canonical_payload 与 API 载荷两种拼写都可能落库。"""

    model = ModelContract.model_validate(
        _model_payload(drill_down_dimensions=[{"dimension_id": "dimension_channel"}])
    )
    assert model.id == "model_orders"


def test_metric_loads_with_the_retired_relate_dimension_key() -> None:
    metric = MetricContract.model_validate(
        {
            "id": "metric_revenue",
            "name": "收入",
            "bizName": "revenue",
            "modelId": "model_orders",
            "metricDefineType": "FIELD",
            "metricDefineByFieldParams": {
                "expr": "SUM(amount)",
                "fields": [{"fieldName": "amount"}],
            },
            "relateDimension": {
                "drillDownDimensions": [{"dimensionId": "dimension_channel", "necessary": True}]
            },
        }
    )
    assert metric.id == "metric_revenue"
    assert not hasattr(metric, "relate_dimension")


def test_unknown_keys_are_still_rejected() -> None:
    """只丢弃已知的退役键；真实结构漂移仍然必须拒载。"""

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelContract.model_validate(_model_payload(someUnknownKey=1))


def test_catalog_round_trip_drops_the_retired_keys_symmetrically() -> None:
    """投影比较两侧都过同一合同，退役键被对称丢弃后仍然相等。"""

    payload = {
        "projectId": "p",
        "revisionId": "r",
        "models": [_model_payload(drillDownDimensions=[])],
    }
    catalog = SemanticCatalog.model_validate(payload)
    reloaded = SemanticCatalog.model_validate(catalog.canonical_payload())
    assert reloaded.canonical_payload() == catalog.canonical_payload()


def test_release_spec_loads_with_the_retired_drill_down_keys() -> None:
    """spec 侧的存量兼容。退役时只处理了 catalog 合同这一侧,spec 侧漏了——
    37 个存量 revision 的 semantic_spec 全部带着 drill_down_dimensions 键,
    extra="forbid" 拒载,所有 revision 端点 INTERNAL_ERROR(2026-08-25 实际发生)。"""

    from knowflow_analytics.contracts import MetricSpec, ModelSpec

    model = ModelSpec.model_validate(
        {
            "id": "orders",
            "name": "订单",
            "schema_name": "public",
            "table": "orders",
            "drill_down_dimensions": [],
        }
    )
    assert model.id == "orders"

    metric = MetricSpec.model_validate(
        {
            "id": "net_revenue",
            "name": "净收入",
            "model_id": "orders",
            "field_id": "orders.net_amount",
            "aggregation": "sum",
            "drill_down_dimensions": [
                {"dimension_id": "region", "necessary": True, "inherited_from_model": False}
            ],
        }
    )
    assert metric.aggregation.value == "sum"
    assert not hasattr(metric, "drill_down_dimensions")


def test_validate_for_publish_tolerates_missing_new_catalog_keys() -> None:
    """发布前的投影比对必须过同一合同归一化,与 catalog_projection_is_bound 同则。

    存量 revision 的 modeling_catalog 是老合同序列化的:没有 hierarchies、
    没有 aggTimeDimensionId。原样全等比对会把每一次合同加字段都变成
    「所有存量 revision 不能发布/评测」(MODELING_PROJECTION_DRIFT,
    2026-08-25 实际发生于 evaluate)。
    """

    import json

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.revision import RevisionEditor

    catalog = json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog

    semantic_catalog = SemanticCatalog.model_validate(catalog)
    spec = compile_semantic_catalog(semantic_catalog)
    # 模拟老投影:丢掉新键(旧合同序列化不会写它们)
    stored_projection = json.loads(json.dumps(spec.modeling_catalog))
    stored_projection.pop("hierarchies", None)
    for metric in stored_projection.get("metrics", []):
        metric.pop("aggTimeDimensionId", None)
    stored_spec = spec.model_copy(update={"modeling_catalog": stored_projection})

    from knowflow_analytics.modeling.contracts import ModelingRevision

    revision = ModelingRevision(
        id=semantic_catalog.revision_id,
        project_id=semantic_catalog.project_id,
        schema_snapshot_hash="sha256:test",
        etag=1,
        state="validated",
        semantic_spec=stored_spec,
        semantic_catalog=semantic_catalog,
    )
    RevisionEditor().validate_for_publish(revision)  # 不得报 MODELING_PROJECTION_DRIFT


def test_validate_for_publish_still_rejects_real_drift() -> None:
    """归一化只补默认键;目录与投影的真实不一致仍然必须拒绝。"""

    import json

    import pytest

    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
    from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog
    from knowflow_analytics.modeling.contracts import ModelingRevision

    catalog = json.loads(FIXTURE_CATALOG.read_text(encoding="utf-8"))
    semantic_catalog = SemanticCatalog.model_validate(catalog)
    spec = compile_semantic_catalog(semantic_catalog)
    drifted_projection = json.loads(json.dumps(spec.modeling_catalog))
    drifted_projection["metrics"][0]["name"] = "被改过的名字"
    # 真实漂移在 ModelingRevision 构造期就被 catalog_projection_is_bound 拒载,
    # 根本到不了发布——这正是想要的:归一化只补默认键,不放过内容不一致。
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="projection differ"):
        ModelingRevision(
            id=semantic_catalog.revision_id,
            project_id=semantic_catalog.project_id,
            schema_snapshot_hash="sha256:test",
            etag=1,
            state="validated",
            semantic_spec=spec.model_copy(update={"modeling_catalog": drifted_projection}),
            semantic_catalog=semantic_catalog,
        )
