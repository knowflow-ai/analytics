"""默认计数指标的身份只跟模型走，不跟主标识列走。

现场（2026-09-16）：建模者换了主标识，默认计数指标的 ID 跟着列名变了，系统把它当成
从没见过的资源，已审核过的别名作废，发布被「没做别名审核」拦下；用户没加任何字段。
"""

from __future__ import annotations

import json

from test_m2_modeling import _catalog

from knowflow_analytics.contracts import TermSpec
from knowflow_analytics.modeling.ai_artifacts import (
    ensure_default_count_metrics,
    reconcile_query_scopes,
)
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog, SemanticContextEntry

ORDERS_COUNT = "metric:default_count:model_orders"
CUSTOMERS_COUNT = "metric:default_count:model_customers"
LEGACY_ORDERS_COUNT = "metric:default_count:model_orders:field:analytics_v0:orders:id"


def _with_orders_composite_primary(catalog: SemanticCatalog) -> SemanticCatalog:
    """把订单表主标识从 id 换成 customer_id + id，派生列于是从 id 变成 customer_id。"""

    models = []
    for model in catalog.models:
        if model.id != "model_orders":
            models.append(model)
            continue
        identifiers = tuple(
            item.model_copy(update={"type": "primary"}) if item.biz_name == "customer_id" else item
            for item in model.model_detail.identifiers
        )
        models.append(
            model.model_copy(
                update={
                    "model_detail": model.model_detail.model_copy(
                        update={"identifiers": identifiers}
                    )
                }
            )
        )
    return SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": tuple(models)}).model_dump(mode="python")
    )


def test_default_count_metric_id_follows_the_model_not_the_identifier_column() -> None:
    _, created = ensure_default_count_metrics(_catalog())

    # 第三个是没有主标识的那张表：它数的是行，ID 同样只跟模型走。
    assert {item.id for item in created} == {
        CUSTOMERS_COUNT,
        ORDERS_COUNT,
        "metric:default_count:model_order_sql_contract",
    }


def test_changing_the_primary_identifier_keeps_the_reviewed_default_count() -> None:
    catalog = reconcile_query_scopes(_catalog())
    reviewed_metrics = tuple(
        item.model_copy(update={"name": "订单笔数", "alias": "单量,订单数"})
        if item.id == ORDERS_COUNT
        else item
        for item in catalog.metrics
    )
    reviewed = SemanticCatalog.model_validate(
        catalog.model_copy(update={"metrics": reviewed_metrics}).model_dump(mode="python")
    )

    changed = reconcile_query_scopes(_with_orders_composite_primary(reviewed))

    metric = next(item for item in changed.metrics if item.id == ORDERS_COUNT)
    assert metric.name == "订单笔数"
    assert metric.alias == "单量,订单数"
    assert metric.metric_define_by_field_params.expr == "COUNT(customer_id)"
    assert metric.ext["knowflow"]["sourceFieldId"] == "field:analytics_v0:orders:customer_id"
    assert not [item for item in changed.metrics if item.id.startswith(f"{ORDERS_COUNT}:")]
    route = next(
        item for item in changed.analysis_topic_routes if item.root_model_id == "model_orders"
    )
    assert route.default_count_metric_id == ORDERS_COUNT


def test_legacy_default_count_id_is_migrated_together_with_every_reference() -> None:
    catalog = reconcile_query_scopes(_catalog())
    legacy = SemanticCatalog.model_validate(
        json.loads(
            json.dumps(catalog.model_dump(mode="json")).replace(ORDERS_COUNT, LEGACY_ORDERS_COUNT)
        )
    )
    legacy = SemanticCatalog.model_validate(
        legacy.model_copy(
            update={
                "terms": (
                    *legacy.terms,
                    TermSpec(
                        id="term-order-volume", name="订单量", metric_ids=(LEGACY_ORDERS_COUNT,)
                    ),
                ),
                "semantic_context": (
                    *legacy.semantic_context,
                    SemanticContextEntry(
                        id="context-orders-count",
                        target_type="metric",
                        target_id=LEGACY_ORDERS_COUNT,
                        kind="definition",
                        text="一条订单记一笔",
                        source_type="human_convention",
                    ),
                ),
            }
        ).model_dump(mode="python")
    )
    assert LEGACY_ORDERS_COUNT in {item.id for item in legacy.metrics}

    migrated = reconcile_query_scopes(legacy)

    metric_ids = {item.id for item in migrated.metrics}
    assert ORDERS_COUNT in metric_ids
    assert LEGACY_ORDERS_COUNT not in metric_ids
    sales = next(item for item in migrated.data_sets if item.id == "dataset_sales")
    dataset_metric_ids = {
        metric_id
        for config in sales.data_set_detail.data_set_model_configs
        for metric_id in config.metrics
    }
    assert ORDERS_COUNT in dataset_metric_ids
    assert LEGACY_ORDERS_COUNT not in dataset_metric_ids
    term = next(item for item in migrated.terms if item.id == "term-order-volume")
    assert term.metric_ids == (ORDERS_COUNT,)
    context = next(item for item in migrated.semantic_context if item.id == "context-orders-count")
    assert context.target_id == ORDERS_COUNT
    route = next(
        item for item in migrated.analysis_topic_routes if item.root_model_id == "model_orders"
    )
    assert route.default_count_metric_id == ORDERS_COUNT
