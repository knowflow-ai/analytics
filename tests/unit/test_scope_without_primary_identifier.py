"""没有主标识的表也该能被问到。

原先的规矩是：默认计数只从已确认的主标识派生，而既无主标识、又无业务指标的模型不
进任何作用域——整表字段问不到。两条加起来把建模者往「每张表都得指一个主标识」上逼，
而 AI 总能指出一个来。客户现场那根 411/44224 的列就是这么被标成主标识的。

对齐 Cube 的条件性要求：独立一张表照样可查，只有当它参与连接且带可加度量时才需要
主键。所以每个模型都得到一个默认计数——有主标识的数键，没有的数行——并且都能成为
自己作用域的事实根。「不能凭空发明实体键」这条没有放松：行数指标数的就是行，它从不
声称自己是实体数。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowflow_analytics.modeling.ai_artifacts import (
    ensure_default_count_metrics,
    reconcile_query_scopes,
)
from knowflow_analytics.modeling.analysis_topics import default_count_metric_id
from knowflow_analytics.modeling.catalog_contracts import IdentifierType, SemanticCatalog

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"
# 夹具里这张表没有任何标识列。
_NO_IDENTIFIER_MODEL = "model_order_sql_contract"
_ORDERS = "model_orders"


def _catalog() -> SemanticCatalog:
    return SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _without_orders_primary(catalog: SemanticCatalog) -> SemanticCatalog:
    """把订单表的主标识降成外部标识——正是数据否决主标识之后的样子。"""

    models = tuple(
        item
        if item.id != _ORDERS
        else item.model_copy(
            update={
                "model_detail": item.model_detail.model_copy(
                    update={
                        "identifiers": tuple(
                            entry.model_copy(update={"type": IdentifierType.FOREIGN})
                            for entry in item.model_detail.identifiers
                        )
                    }
                )
            }
        )
        for item in catalog.models
    )
    return SemanticCatalog.model_validate(
        catalog.model_copy(update={"models": models}).model_dump(mode="python")
    )


def test_every_model_gets_a_default_count() -> None:
    _, created = ensure_default_count_metrics(_catalog())

    assert {item.model_id for item in created} == {item.id for item in _catalog().models}


def test_a_model_without_a_primary_identifier_counts_rows() -> None:
    _, created = ensure_default_count_metrics(_catalog())

    rows = next(item for item in created if item.model_id == _NO_IDENTIFIER_MODEL)
    keys = next(item for item in created if item.model_id == _ORDERS)

    assert rows.metric_define_by_field_params.expr == "COUNT(*)"
    assert rows.metric_define_by_field_params.fields == ()
    # 有主标识的照旧数键，不受影响。
    assert keys.metric_define_by_field_params.expr.startswith("COUNT(")
    assert keys.metric_define_by_field_params.fields != ()


def test_it_says_what_it_counts() -> None:
    """名字一样（用户就是这么问的），说明不一样——一个数实体，一个数行。"""

    _, created = ensure_default_count_metrics(_catalog())

    rows = next(item for item in created if item.model_id == _NO_IDENTIFIER_MODEL)

    assert "行" in rows.description
    assert rows.ext["knowflow"]["sourceFieldId"] is None


def test_such_a_model_becomes_its_own_fact_root() -> None:
    """既无主标识也无业务指标的表，此前整表字段问不到。"""

    catalog = reconcile_query_scopes(_catalog())

    route = next(
        (
            item
            for item in catalog.analysis_topic_routes
            if item.root_model_id == _NO_IDENTIFIER_MODEL
        ),
        None,
    )

    assert route is not None, "每张实表都该有自己的作用域"
    assert route.default_count_metric_id == default_count_metric_id(_NO_IDENTIFIER_MODEL)


def test_losing_the_primary_identifier_rederives_the_count_instead_of_keeping_a_stale_column() -> (
    None
):
    """数据否决主标识之后，默认计数不能还在数那根已经不是键的列。"""

    catalog = reconcile_query_scopes(_catalog())
    demoted = reconcile_query_scopes(_without_orders_primary(catalog))

    metric = next(item for item in demoted.metrics if item.id == default_count_metric_id(_ORDERS))

    assert metric.metric_define_by_field_params.expr == "COUNT(*)"
    assert metric.ext["knowflow"]["sourceFieldId"] is None


@pytest.mark.parametrize("model_id", [_ORDERS, _NO_IDENTIFIER_MODEL])
def test_the_compiled_release_keeps_every_default_count_queryable(model_id: str) -> None:
    from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog

    release = compile_semantic_catalog(reconcile_query_scopes(_catalog()))
    metric_id = default_count_metric_id(model_id)

    metric = next(item for item in release.metrics if item.id == metric_id)
    route = next(item for item in release.analysis_topic_routes if item.root_model_id == model_id)
    dataset = next(item for item in release.datasets if item.id == route.dataset_id)

    assert metric.id in dataset.metric_ids
    assert route.default_count_metric_id == metric_id
