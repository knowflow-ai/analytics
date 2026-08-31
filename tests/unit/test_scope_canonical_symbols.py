from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.contracts import (
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    SemanticQuery,
    SemanticQueryType,
)
from knowflow_analytics.query.contracts import MapMode
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.parser import (
    LlmS2SqlParser,
    _dimension_payload,
    _metric_payload_entry,
    serialize_s2sql,
)
from knowflow_analytics.query.symbols import SemanticSymbolTable
from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator


class _ConstantEmbeddingGateway:
    def encode(self, texts):
        return EmbeddingBatch(
            model_id="constant",
            dimension=1,
            vectors=tuple((1.0,) for _ in texts),
        )


def _qualified_release(sales_release):
    dimensions = tuple(
        item.model_copy(
            update={
                "name": "名称",
                "aliases": (("客户名称",) if item.id == "customer_segment" else item.aliases),
            }
        )
        if item.id in {"region", "customer_segment"}
        else item
        for item in sales_release.dimensions
    )
    route = AnalysisTopicRouteSpec(
        dataset_id="sales_dataset",
        root_model_id="orders",
        paths=(
            AnalysisTopicPathSpec(
                target_model_id="customers",
                relation_ids=("orders_customer",),
                prefix="客户",
            ),
        ),
    )
    dataset = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": ("region", "customer_segment"),
        }
    )
    return sales_release.model_copy(
        update={
            "dimensions": dimensions,
            "datasets": (dataset,),
            "analysis_topic_routes": (route,),
        }
    )


def test_scope_qualified_names_are_the_only_unambiguous_s2sql_symbols(sales_release):
    release = _qualified_release(sales_release)
    symbols = SemanticSymbolTable.from_release(release, dataset_id="sales_dataset")

    assert symbols.canonical_name("region") == "名称"
    assert symbols.canonical_name("customer_segment") == "客户.名称"
    assert symbols.resolve_first("客户.名称").id == "customer_segment"
    assert symbols.resolve_first("名称").id == "region"
    for element_id in (*release.datasets[0].metric_ids, *release.datasets[0].dimension_ids):
        assert symbols.resolve_first(symbols.canonical_name(element_id)).id == element_id


def test_scope_qualified_names_reach_index_and_final_parser_prompt(sales_release):
    release = _qualified_release(sales_release)
    index = SemanticIndexBuilder(_ConstantEmbeddingGateway()).build(release)
    assert any(
        item.phrase == "客户.名称"
        and item.element_id == "customer_segment"
        and item.dataset_ids == ("sales_dataset",)
        for item in index.entries
    )
    mapping = SemanticMapper().map(
        question="按客户名称统计净收入",
        dataset_id="sales_dataset",
        index=index,
        mode=MapMode.STRICT,
    )
    content = LlmS2SqlParser._messages(
        "按客户名称统计净收入",
        release,
        release.datasets[0],
        mapping,
        now=datetime(2026, 8, 27, tzinfo=UTC),
    )[1]["content"]

    assert "'name': '客户.名称'" in content


def test_every_prompt_name_and_alias_round_trips_to_its_advertised_member(sales_release):
    release = _qualified_release(sales_release)
    dataset = release.datasets[0]
    symbols = SemanticSymbolTable.from_release(release, dataset_id=dataset.id)
    entries_by_id = {
        item.id: entry
        for item, entry in zip(
            (item for item in release.dimensions if item.id in dataset.dimension_ids),
            _dimension_payload(release, dataset, symbols=symbols),
            strict=True,
        )
    }
    dimension_names = {
        item.id: symbols.canonical_name(item.id)
        for item in release.dimensions
        if item.id in dataset.dimension_ids
    }
    entries_by_id.update(
        {
            item.id: _metric_payload_entry(
                item,
                dimension_names,
                canonical_name=symbols.canonical_name(item.id),
                symbols=symbols,
            )
            for item in release.metrics
            if item.id in dataset.metric_ids
        }
    )

    for element_id, entry in entries_by_id.items():
        for advertised in (entry["name"], *entry["aliases"]):
            assert symbols.resolve_first(advertised).id == element_id
    assert "名称" not in entries_by_id["customer_segment"]["aliases"]


def test_scope_qualified_name_translates_back_to_the_governed_dimension(sales_release):
    release = _qualified_release(sales_release)

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT "客户.名称", SUM("净收入") FROM "销售经营" '
            'GROUP BY "客户.名称"'
        ),
    )

    assert translated.audit_query.dimension_ids == ("customer_segment",)
    assert translated.audit_query.metric_ids == ("net_revenue",)
    assert "orders_customer" in translated.physical_query.relation_ids


@pytest.mark.parametrize("dimension_id", ["region", "customer_segment"])
def test_structured_serializer_round_trips_every_colliding_scope_member(
    sales_release,
    dimension_id,
):
    release = _qualified_release(sales_release)
    s2sql = serialize_s2sql(
        SemanticQuery(
            dataset_id="sales_dataset",
            query_type=SemanticQueryType.DETAIL,
            dimension_ids=(dimension_id,),
        ),
        release=release,
    )

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=s2sql,
    )

    assert translated.audit_query.dimension_ids == (dimension_id,)
