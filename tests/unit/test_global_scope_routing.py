from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    DatasetSpec,
    DimensionSpec,
    DimensionValueSpec,
    MetricSpec,
    QueryResult,
    TermSpec,
)
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.query.contracts import (
    MapMode,
    QueryRequest,
    QueryState,
    SemanticAmbiguityMember,
)
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator
from knowflow_analytics.semantic.index import (
    EmbeddingBatch,
    SemanticElementType,
    SemanticIndexBuilder,
)


class _CountingEmbeddingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        self.calls.append(tuple(texts))
        return EmbeddingBatch(
            model_id="counting",
            dimension=8,
            vectors=tuple(self._vector(text) for text in texts),
        )

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        values = [0.0] * 8
        for index, character in enumerate(text):
            values[(ord(character) + index) % len(values)] += 1.0
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return tuple(value / norm for value in values)


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, query, release):
        self.calls += 1
        return QueryResult(columns=("value",), rows=((1,),), row_count=1)


class _InvalidS2SqlGateway:
    def generate_json(self, **_kwargs):
        return {"thought": "invalid", "sql": 'SELECT "未知指标" FROM "销售经营"'}


class _MetricChoosingGateway:
    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        return {
            "thought": "用户已确认根事实指标",
            "sql": 'SELECT SUM("业务数量总计") FROM "销售经营"',
        }


class _FixedS2SqlGateway:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls = 0

    def generate_json(self, **_kwargs):
        self.calls += 1
        return {"thought": "在已选 Scope 内裁决别名", "sql": self.sql}


class _AmbiguityOnlyEmbeddingGateway:
    """Only the test wording and the two colliding names share a vector."""

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        vectors = tuple(
            (1.0, 0.0)
            if "火星" in text
            else (0.9, 0.435889894)
            if "业务数量总计" in text
            else (0.0, 1.0)
            for text in texts
        )
        return EmbeddingBatch(model_id="ambiguity-only", dimension=2, vectors=vectors)


def _service(
    release,
    *,
    llm_gateway=None,
    query_embedding: bool = True,
    query_failures=None,
):
    gateway = _CountingEmbeddingGateway()
    index = SemanticIndexBuilder(gateway).build(release)
    gateway.calls.clear()
    executor = _Executor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(embedding_gateway=gateway if query_embedding else None),
            llm_parser=(LlmS2SqlParser(llm_gateway) if llm_gateway is not None else None),
        ),
        translator=SemanticTranslator(),
        executor=executor,
        query_failures=query_failures,
    )
    return service, gateway, executor


def _selection_context_for(
    service: AnalyticsQueryService,
    *,
    question: str,
    dataset_ids: tuple[str, ...],
    actor_id: str = "",
):
    published = service._releases.published
    return service._selection_context(
        request=QueryRequest(
            project_id=published.release.project_id,
            question=question,
            dataset_ids=dataset_ids,
        ),
        published=published,
        dataset_ids=dataset_ids,
        actor_id=actor_id,
    )


def _decoded_option(
    service: AnalyticsQueryService,
    option,
    *,
    question: str,
    dataset_ids: tuple[str, ...],
    actor_id: str = "",
) -> tuple[str | None, str | tuple[str, ...] | None]:
    published = service._releases.published
    dataset_id, semantic_ids = service._decode_selection_token(
        option.candidate_id,
        release=published.release,
        context=_selection_context_for(
            service,
            question=question,
            dataset_ids=dataset_ids,
            actor_id=actor_id,
        ),
    )
    return (
        dataset_id,
        semantic_ids[0] if len(semantic_ids) == 1 else semantic_ids if semantic_ids else None,
    )


def _semantic_option(
    service: AnalyticsQueryService,
    options,
    semantic_selection_id: str,
    *,
    question: str,
    dataset_ids: tuple[str, ...],
):
    return next(
        option
        for option in options
        if _decoded_option(
            service,
            option,
            question=question,
            dataset_ids=dataset_ids,
        )[1]
        == semantic_selection_id
    )


def _routed_release(sales_release):
    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    customer_scope = DatasetSpec(
        id="customer_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(),
        dimension_ids=("customer_segment",),
    )
    return sales_release.model_copy(
        update={
            "datasets": (sales, customer_scope),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=customer_scope.id,
                    root_model_id="customers",
                ),
            ),
        }
    )


def _weak_sales_metric_release(sales_release):
    """Two roots share an exact region value, but only orders owns the weak metric."""

    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    item_scope = DatasetSpec(
        id="item_scope",
        name="订单明细分析",
        model_ids=("order_items", "orders"),
        metric_ids=(),
        dimension_ids=("product", "region"),
    )
    metrics = tuple(
        item.model_copy(update={"aliases": ("净销售额",)}) if item.id == "net_revenue" else item
        for item in sales_release.metrics
    )
    return sales_release.model_copy(
        update={
            "metrics": metrics,
            "datasets": (sales, item_scope),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=item_scope.id,
                    root_model_id="order_items",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="orders",
                            relation_ids=("orders_items",),
                        ),
                    ),
                ),
            ),
        }
    )


def _routed_cross_type_same_name_release(sales_release, *, with_term: bool = False):
    """A valid routed Scope whose canonical names remain distinguishable.

    The root metric keeps its raw name, while the remote dimension is compiled
    as ``客户.业务数量总计``.  Their raw display names intentionally collide,
    so pre-LLM clarification can exercise typed metric/dimension tokens.
    """

    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    metrics = tuple(
        item.model_copy(update={"name": "业务数量总计", "aliases": ()})
        if item.id == "net_revenue"
        else item
        for item in sales_release.metrics
    )
    dimensions = tuple(
        item.model_copy(update={"name": "业务数量总计", "aliases": ()})
        if item.id == "customer_segment"
        else item
        for item in sales_release.dimensions
    )
    terms = sales_release.terms
    if with_term:
        terms = (
            *terms,
            TermSpec(
                id="business_object_term",
                name="业务对象",
                description="业务数量总计",
                dataset_ids=(sales.id,),
                metric_ids=("net_revenue",),
            ),
        )
    return sales_release.model_copy(
        update={
            "metrics": metrics,
            "dimensions": dimensions,
            "datasets": (sales,),
            "terms": terms,
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                            prefix="客户",
                        ),
                    ),
                ),
            ),
        }
    )


def _ambiguity_service(
    release,
    *,
    embedding_gateway=None,
    llm_gateway=None,
):
    index_gateway = embedding_gateway or _CountingEmbeddingGateway()
    index = SemanticIndexBuilder(index_gateway).build(release)
    llm_gateway = llm_gateway or _MetricChoosingGateway()
    executor = _Executor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(embedding_gateway=embedding_gateway),
            llm_parser=LlmS2SqlParser(llm_gateway),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )
    return service, llm_gateway, executor


def _assert_typed_ambiguity_continues(
    *,
    service,
    gateway: _MetricChoosingGateway,
    executor: _Executor,
    question: str,
    expected_method: str,
) -> None:
    first = service.query(
        QueryRequest(
            project_id="sales",
            question=question,
            dataset_ids=("sales_dataset",),
            include_diagnostics=True,
        )
    )

    assert first.state is QueryState.CLARIFICATION_REQUIRED, first
    assert gateway.calls == 0
    assert {
        _decoded_option(
            service,
            item,
            question=question,
            dataset_ids=("sales_dataset",),
        )[1]
        for item in first.options
    } == {
        "element:metric:net_revenue",
        "element:dimension:customer_segment",
    }
    discovery = next(item for item in first.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["scope_resolution"]["exact_metric_ids"] == []
    assert any(
        match["method"] == expected_method
        for projection in discovery.detail["mapping_attempts"]
        for match in projection["matches"]
        if match["element_id"] in {"net_revenue", "customer_segment"}
    )

    selected = _semantic_option(
        service,
        first.options,
        "element:metric:net_revenue",
        question=question,
        dataset_ids=("sales_dataset",),
    )
    second = service.query(
        QueryRequest(
            project_id="sales",
            question=question,
            dataset_ids=("sales_dataset",),
            selected_candidate_id=selected.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )

    assert second.state is QueryState.COMPLETED, second
    assert second.semantic_query.metric_ids == ("net_revenue",)
    assert second.semantic_query.dimension_ids == ()
    assert gateway.calls == 1
    assert executor.calls == 1


def test_keyword_ambiguity_returns_typed_options_and_the_selection_continues(
    sales_release,
) -> None:
    release = _routed_cross_type_same_name_release(sales_release)
    service, gateway, executor = _ambiguity_service(release)

    _assert_typed_ambiguity_continues(
        service=service,
        gateway=gateway,
        executor=executor,
        question="业务数量",
        expected_method="keyword",
    )


def test_embedding_ambiguity_returns_typed_options_and_the_selection_continues(
    sales_release,
) -> None:
    release = _routed_cross_type_same_name_release(sales_release)
    service, gateway, executor = _ambiguity_service(
        release,
        embedding_gateway=_AmbiguityOnlyEmbeddingGateway(),
    )

    _assert_typed_ambiguity_continues(
        service=service,
        gateway=gateway,
        executor=executor,
        question="火星",
        expected_method="embedding",
    )


def test_term_ambiguity_returns_typed_options_and_the_selection_continues(
    sales_release,
) -> None:
    release = _routed_cross_type_same_name_release(sales_release, with_term=True)
    service, gateway, executor = _ambiguity_service(release)

    _assert_typed_ambiguity_continues(
        service=service,
        gateway=gateway,
        executor=executor,
        question="业务对象",
        expected_method="term_description",
    )


def test_one_scope_distinct_names_sharing_an_alias_are_settled_by_the_final_llm(
    sales_release,
) -> None:
    release = _routed_release(sales_release).model_copy(
        update={
            "datasets": (_routed_release(sales_release).datasets[0],),
            "analysis_topic_routes": (_routed_release(sales_release).analysis_topic_routes[0],),
            "metrics": tuple(
                item.model_copy(update={"aliases": ("共享口径",)})
                if item.id in {"net_revenue", "refund_amount"}
                else item
                for item in sales_release.metrics
            ),
        }
    )
    index = SemanticIndexBuilder(_CountingEmbeddingGateway()).build(release)
    gateway = _FixedS2SqlGateway('SELECT SUM("净收入") FROM "销售经营"')
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(gateway),
        ),
        translator=SemanticTranslator(),
        executor=_Executor(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="共享口径",
            dataset_ids=("sales_dataset",),
        )
    )

    assert response.state is QueryState.COMPLETED, response
    assert gateway.calls == 1
    assert response.semantic_query.metric_ids == ("net_revenue",)
    assert response.resolved_by_llm[0].detected_text == "共享口径"
    assert _decoded_option(
        service,
        response.resolved_by_llm[0].chosen,
        question="共享口径",
        dataset_ids=("sales_dataset",),
    ) == ("sales_dataset", "element:metric:net_revenue")


def test_human_continuation_rebuilds_distinct_exact_alias_options(
    sales_release,
) -> None:
    routed = _routed_release(sales_release)
    release = routed.model_copy(
        update={
            "datasets": (routed.datasets[0],),
            "analysis_topic_routes": (routed.analysis_topic_routes[0],),
            "metrics": tuple(
                item.model_copy(update={"aliases": ("共享口径",)})
                if item.id in {"net_revenue", "refund_amount"}
                else item
                for item in sales_release.metrics
            ),
        }
    )
    gateway = _FixedS2SqlGateway('SELECT SUM("净收入"), SUM("退款金额") FROM "销售经营"')
    service, _embedding, executor = _service(
        release,
        llm_gateway=gateway,
        query_embedding=False,
    )
    request = QueryRequest(
        project_id="sales",
        question="共享口径",
        dataset_ids=("sales_dataset",),
    )

    first = service.query(request, actor_id="tenant-1")
    assert first.state is QueryState.CLARIFICATION_REQUIRED
    selected = next(item for item in first.options if item.element_id == "net_revenue")
    gateway.sql = 'SELECT SUM("净收入") FROM "销售经营"'
    second = service.query(
        request.model_copy(
            update={
                "selected_candidate_id": selected.candidate_id,
                "expected_release_id": first.release_id,
                "expected_spec_hash": first.spec_hash,
                "expected_index_snapshot_id": first.index_snapshot_id,
            }
        ),
        actor_id="tenant-1",
    )

    assert second.state is QueryState.COMPLETED, second.model_dump_json(indent=2)
    assert second.semantic_query.metric_ids == ("net_revenue",)
    assert second.semantic_decisions[0].source == "human"
    assert executor.calls == 1


def _cross_scope_same_bare_id_release(sales_release):
    shared_metric = MetricSpec(
        id="shared",
        name="业务数量总计",
        model_id="orders",
        field_id="orders.net_amount",
        aggregation=Aggregation.SUM,
    )
    shared_dimension = DimensionSpec(
        id="shared",
        name="业务数量总计",
        model_id="customers",
        field_id="customers.segment",
    )
    shared_value = DimensionValueSpec(
        id="shared_value",
        dimension_id="shared",
        value="VIP",
        display_name="VIP",
    )
    orders_scope = DatasetSpec(
        id="orders_scope",
        name="订单范围",
        model_ids=("orders",),
        metric_ids=("shared",),
        dimension_ids=("region",),
    )
    customers_scope = DatasetSpec(
        id="customers_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(),
        dimension_ids=("shared",),
    )
    return sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, shared_metric),
            "dimensions": (*sales_release.dimensions, shared_dimension),
            "dimension_values": (*sales_release.dimension_values, shared_value),
            "datasets": (orders_scope, customers_scope),
            "terms": (),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=orders_scope.id,
                    root_model_id="orders",
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=customers_scope.id,
                    root_model_id="customers",
                ),
            ),
        }
    )


def test_typed_element_scope_filtering_separates_cross_scope_same_bare_ids(
    sales_release,
) -> None:
    release = _cross_scope_same_bare_id_release(sales_release)
    all_scopes = ("orders_scope", "customers_scope")

    assert AnalyticsQueryService._scope_datasets_to_selected_element(
        release,
        all_scopes,
        "shared",
        element_type=SemanticElementType.METRIC,
        require_time=False,
    ) == ("orders_scope",)
    assert AnalyticsQueryService._scope_datasets_to_selected_element(
        release,
        all_scopes,
        "shared",
        element_type=SemanticElementType.DIMENSION,
        require_time=False,
    ) == ("customers_scope",)
    assert AnalyticsQueryService._scope_datasets_to_selected_element(
        release,
        all_scopes,
        "shared_value",
        element_type=SemanticElementType.DIMENSION_VALUE,
        require_time=False,
    ) == ("customers_scope",)


def test_same_root_internal_scope_variants_are_not_user_choices(
    sales_release,
) -> None:
    routed = _routed_release(sales_release)
    sales = routed.datasets[0]
    alternate = sales.model_copy(update={"id": "sales_alternate", "name": "销售经营备用"})
    first_route = routed.analysis_topic_routes[0]
    release = routed.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"aliases": ("共享口径",)})
                if item.id in {"net_revenue", "refund_amount"}
                else item
                for item in routed.metrics
            ),
            "datasets": (sales, alternate),
            "analysis_topic_routes": (
                first_route,
                first_route.model_copy(update={"dataset_id": alternate.id}),
            ),
        }
    )
    index = SemanticIndexBuilder(_CountingEmbeddingGateway()).build(release)
    gateway = _FixedS2SqlGateway('SELECT SUM("净收入") FROM "销售经营"')
    executor = _Executor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(),
            llm_parser=LlmS2SqlParser(gateway),
        ),
        translator=SemanticTranslator(),
        executor=executor,
    )

    first = service.query(QueryRequest(project_id="sales", question="共享口径"))

    assert first.state is QueryState.CLARIFICATION_REQUIRED
    assert gateway.calls == 0
    assert first.options == ()
    assert "重新发布" in first.question
    assert executor.calls == 0


def test_semantic_options_never_map_an_element_to_a_dataset_outside_the_request(
    sales_release,
) -> None:
    customer_metric = MetricSpec(
        id="customer_count",
        name="客户数量",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    sales = sales_release.datasets[0]
    customer_scope = DatasetSpec(
        id="customer_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(customer_metric.id,),
        dimension_ids=("customer_segment",),
    )
    release = sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, customer_metric),
            "datasets": (sales, customer_scope),
        }
    )

    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset",),
    )
    options = service._semantic_options(
        release,
        ("net_revenue", "customer_count"),
        selection_context=context,
        require_time=False,
        allowed_dataset_ids=("sales_dataset",),
    )

    assert len(options) == 1
    assert options[0].dataset_id == "sales_dataset"
    assert _decoded_option(
        service,
        options[0],
        question="测试",
        dataset_ids=("sales_dataset",),
    ) == (None, "element:metric:net_revenue")


def test_typed_semantic_options_do_not_inject_a_same_id_dimension_value(
    sales_release,
) -> None:
    colliding_value = DimensionValueSpec(
        id="net_revenue",
        dimension_id="region",
        value="净收入",
        display_name="净收入值",
    )
    release = sales_release.model_copy(
        update={
            "dimension_values": (*sales_release.dimension_values, colliding_value),
        }
    )

    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset",),
    )
    options = service._semantic_options(
        release,
        ("net_revenue", "channel"),
        selection_context=context,
        require_time=False,
        typed_members=(
            SemanticAmbiguityMember(
                element_type=SemanticElementType.METRIC,
                element_id="net_revenue",
            ),
            SemanticAmbiguityMember(
                element_type=SemanticElementType.DIMENSION,
                element_id="channel",
            ),
        ),
        allowed_dataset_ids=("sales_dataset",),
    )

    assert {
        _decoded_option(
            service,
            item,
            question="测试",
            dataset_ids=("sales_dataset",),
        )[1]
        for item in options
    } == {
        "element:metric:net_revenue",
        "element:dimension:channel",
    }


def test_scope_fallback_is_presented_as_governed_metrics_not_datasets(
    sales_release,
) -> None:
    """作用域定不下来时给的是指标，且一个内部名都不许露出来。"""

    release = _routed_release(sales_release)

    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset", "customer_scope"),
    )
    options = service._scope_choice_options(
        release,
        ("sales_dataset", "customer_scope"),
        selection_context=context,
    )

    # 指标与维度都给：问句可能压根没有指标意图。
    assert {item.kind for item in options} <= {"metric", "dimension"}
    assert "净收入" in {item.label for item in options}
    rendered = " ".join(f"{item.label} {item.description}" for item in options)
    assert "销售经营" not in rendered
    assert "客户范围" not in rendered
    assert "查询作用域" not in rendered
    assert "sales_dataset" not in rendered


def test_entity_attribute_routes_to_its_owner_without_a_business_object_card(
    sales_release,
) -> None:
    """「客户分层」归客户所有；销售经营可达但只是借用，不再问用户要分析什么。"""

    release = _routed_release(sales_release)
    llm = _FixedS2SqlGateway('SELECT "客户分层" FROM "客户范围"')
    service, _embedding, executor = _service(
        release,
        llm_gateway=llm,
        query_embedding=False,
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="客户分层",
            dataset_ids=("sales_dataset", "customer_scope"),
            include_diagnostics=True,
        ),
        actor_id="tenant-1",
    )

    assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
    assert response.semantic_query.dataset_id == "customer_scope"
    assert response.semantic_query.dimension_ids == ("customer_segment",)
    assert executor.calls == 1
    discovery = next(item for item in response.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["scope_resolution"]["selected_dataset_id"] == "customer_scope"
    assert discovery.detail["scope_resolution"]["code"] == "QUERY_SCOPE_SELECTED"


def test_business_object_continuation_keeps_the_confirmed_dimension_without_scope_leak(
    sales_release,
) -> None:
    """Reviewed V2 contract: one opaque option carries semantic + fact grain."""

    single = _routed_cross_type_same_name_release(sales_release)
    customer = DatasetSpec(
        id="customer_scope",
        name="客户内部范围",
        model_ids=("customers",),
        metric_ids=(),
        dimension_ids=("customer_segment",),
    )
    release = single.model_copy(
        update={
            "datasets": (single.datasets[0], customer),
            "analysis_topic_routes": (
                *single.analysis_topic_routes,
                AnalysisTopicRouteSpec(
                    dataset_id=customer.id,
                    root_model_id="customers",
                ),
            ),
        }
    )
    service, gateway, executor = _ambiguity_service(
        release,
        llm_gateway=_FixedS2SqlGateway('SELECT "客户.业务数量总计" FROM "销售经营"'),
    )
    first = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset", "customer_scope"),
        )
    )
    assert first.state is QueryState.CLARIFICATION_REQUIRED, first
    dimension_options = [item for item in first.options if item.kind == "dimension"]
    assert len(dimension_options) == 2
    selected_orders = next(
        option for option in dimension_options if "分析粒度：订单" in option.description
    )
    forged_token = selected_orders.candidate_id[:-1] + (
        "0" if selected_orders.candidate_id[-1] != "0" else "1"
    )
    forged = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset", "customer_scope"),
            selected_candidate_id=forged_token,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )
    assert forged.state is QueryState.FAILED
    assert forged.error.code == "CANDIDATE_NOT_FOUND"
    assert gateway.calls == 0
    assert executor.calls == 0

    stale = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset", "customer_scope"),
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id="staged:retired",
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )
    assert stale.state is QueryState.FAILED
    assert stale.error.code == "STALE_QUERY_SELECTION"
    assert gateway.calls == 0
    assert executor.calls == 0

    replayed_for_another_question = service.query(
        QueryRequest(
            project_id="sales",
            question="另一个问题",
            dataset_ids=("sales_dataset", "customer_scope"),
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )
    assert replayed_for_another_question.state is QueryState.FAILED
    assert replayed_for_another_question.error.code == "CANDIDATE_NOT_FOUND"

    replayed_for_another_actor = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset", "customer_scope"),
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        ),
        actor_id="another-user",
    )
    assert replayed_for_another_actor.state is QueryState.FAILED
    assert replayed_for_another_actor.error.code == "CANDIDATE_NOT_FOUND"

    replayed_with_another_dataset_set = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset",),
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )
    assert replayed_with_another_dataset_set.state is QueryState.FAILED
    assert replayed_with_another_dataset_set.error.code == "CANDIDATE_NOT_FOUND"
    assert gateway.calls == 0
    assert executor.calls == 0

    third = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=("sales_dataset", "customer_scope"),
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
            include_diagnostics=True,
        )
    )

    assert third.state is QueryState.COMPLETED, third.model_dump_json(indent=2)
    assert third.semantic_query.dataset_id == "sales_dataset"
    assert third.semantic_query.dimension_ids == ("customer_segment",)
    assert gateway.calls == 1
    assert executor.calls == 1


def test_one_human_card_bundles_semantic_and_business_grain_choices(
    sales_release,
) -> None:
    single = _routed_cross_type_same_name_release(sales_release)
    customer = DatasetSpec(
        id="customer_scope",
        name="客户内部范围",
        model_ids=("customers",),
        metric_ids=(),
        dimension_ids=("customer_segment",),
    )
    release = single.model_copy(
        update={
            "datasets": (single.datasets[0], customer),
            "analysis_topic_routes": (
                *single.analysis_topic_routes,
                AnalysisTopicRouteSpec(
                    dataset_id=customer.id,
                    root_model_id="customers",
                ),
            ),
        }
    )
    service, gateway, executor = _ambiguity_service(
        release,
        llm_gateway=_FixedS2SqlGateway('SELECT "客户.业务数量总计" FROM "销售经营"'),
    )
    dataset_ids = ("sales_dataset", "customer_scope")

    first = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=dataset_ids,
        )
    )

    assert first.state is QueryState.CLARIFICATION_REQUIRED
    dimension_options = [item for item in first.options if item.kind == "dimension"]
    assert len(dimension_options) == 2
    assert {
        _decoded_option(
            service,
            item,
            question="业务数量",
            dataset_ids=dataset_ids,
        )
        for item in dimension_options
    } == {
        ("sales_dataset", "element:dimension:customer_segment"),
        ("customer_scope", "element:dimension:customer_segment"),
    }
    assert {item.kind for item in first.options} == {"metric", "dimension"}
    selected_orders = next(item for item in dimension_options if "订单" in item.description)

    second = service.query(
        QueryRequest(
            project_id="sales",
            question="业务数量",
            dataset_ids=dataset_ids,
            selected_candidate_id=selected_orders.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
        )
    )

    assert second.state is QueryState.COMPLETED, second.model_dump_json(indent=2)
    assert second.semantic_query.dataset_id == "sales_dataset"
    assert second.semantic_query.dimension_ids == ("customer_segment",)
    # 作用域仍由同一个 token 绑定，但不再作为「业务对象」决策报给用户——
    # 它是内部执行计划，回答卡上不出现。
    assert {(item.source.value, item.chosen.kind) for item in second.semantic_decisions} == {
        ("human", "dimension"),
    }
    assert gateway.calls == 1
    assert executor.calls == 1


def test_business_object_and_semantic_confirmation_are_bundled_in_the_first_card(
    sales_release,
) -> None:
    """Reverse ambiguity is also one card, followed by parsing or refusal.

    This fixture deliberately leaves two canonical dimensions with the same
    business name, so textual S2SQL cannot express the final chosen ID and the
    frozen parser correctly fails.  The continuation contract under test is
    that the first option binds both the semantic ID and the business root, so
    a click never produces a second blocking card.
    """

    release = _routed_release(sales_release).model_copy(
        update={
            "dimensions": tuple(
                item.model_copy(update={"name": "共享维度", "aliases": ()})
                if item.id in {"region", "customer_segment"}
                else item
                for item in sales_release.dimensions
            )
        }
    )
    service, gateway, executor = _ambiguity_service(
        release,
        llm_gateway=_FixedS2SqlGateway(
            'SELECT "共享维度", SUM("净收入") FROM "销售经营" GROUP BY "共享维度"'
        ),
    )
    dataset_ids = ("sales_dataset", "customer_scope")

    first = service.query(
        QueryRequest(
            project_id="sales",
            question="共享维度",
            dataset_ids=dataset_ids,
        )
    )
    assert first.state is QueryState.CLARIFICATION_REQUIRED, first
    assert {option.kind for option in first.options} == {"dimension"}
    assert {
        _decoded_option(
            service,
            option,
            question="共享维度",
            dataset_ids=dataset_ids,
        )
        for option in first.options
    } == {
        # 归属决定作用域：客户分层归客户所有，销售经营只是借用，所以不再
        # 提供「销售经营 × 客户分层」这种让用户替系统挑执行计划的组合。
        ("sales_dataset", "element:dimension:region"),
        ("customer_scope", "element:dimension:customer_segment"),
    }
    selected_customer_segment = next(
        option
        for option in first.options
        if _decoded_option(
            service,
            option,
            question="共享维度",
            dataset_ids=dataset_ids,
        )
        == ("sales_dataset", "element:dimension:region")
    )

    second = service.query(
        QueryRequest(
            project_id="sales",
            question="共享维度",
            dataset_ids=dataset_ids,
            selected_candidate_id=selected_customer_segment.candidate_id,
            expected_release_id=first.release_id,
            expected_spec_hash=first.spec_hash,
            expected_index_snapshot_id=first.index_snapshot_id,
            include_diagnostics=True,
        )
    )

    assert second.state is QueryState.FAILED, second.model_dump_json(indent=2)
    assert second.error.code == "LLM_S2SQL_AMBIGUOUS_SYMBOL"
    discovery = next(item for item in second.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["scope_resolution"]["selected_dataset_id"] == "sales_dataset"
    assert gateway.calls >= 1
    assert executor.calls == 0


def test_card_projection_reuses_rule_candidate_admission_across_mapping_modes(
    sales_release,
) -> None:
    release = _routed_cross_type_same_name_release(sales_release)
    index = SemanticIndexBuilder(_CountingEmbeddingGateway()).build(release)
    orchestrator = CandidateOrchestrator(mapper=SemanticMapper())
    question = "各区域业务数量"
    evidence = orchestrator.collect_evidence(
        question=question,
        dataset_ids=("sales_dataset",),
        index=index,
    )

    selected = (
        orchestrator.discover_selected_scope(
            question=question,
            release=release,
            evidence=evidence,
            dataset_id="sales_dataset",
        )
        .candidates[0]
        .mapping
    )
    card_mapping = orchestrator.project_admitted_scope_mapping(
        question=question,
        release=release,
        evidence=evidence,
        dataset_id="sales_dataset",
    )

    assert selected.mode is MapMode.MODERATE
    assert card_mapping == selected
    assert card_mapping.semantic_ambiguity_groups


def test_opaque_scope_context_round_trips_a_dimension_value_without_serializing_ids(
    sales_release,
) -> None:
    release = _routed_release(sales_release)
    value = sales_release.dimension_values[0]
    semantic_token = f"value:{value.id}"
    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset", "customer_scope"),
    )

    token = service._selection_token(
        release=release,
        context=context,
        dataset_id="sales_dataset",
        semantic_selection_id=semantic_token,
    )
    selected_dataset_id, restored_semantic_token = service._decode_selection_token(
        token,
        release=release,
        context=context,
    )

    assert selected_dataset_id == "sales_dataset"
    assert restored_semantic_token == (semantic_token,)
    assert "sales_dataset" not in token
    assert value.id not in token


def test_selection_token_is_bound_to_conversation_and_semantic_clock(
    sales_release,
) -> None:
    release = _routed_release(sales_release)
    service, _gateway, _executor = _service(release, query_embedding=False)
    published = service._releases.published
    dataset_ids = ("sales_dataset", "customer_scope")
    request = QueryRequest(
        project_id="sales",
        question="测试",
        dataset_ids=dataset_ids,
        conversation_id="conversation-a",
    )
    context = service._selection_context(
        request=request,
        published=published,
        dataset_ids=dataset_ids,
        actor_id="tenant-1",
        semantic_now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    token = service._selection_token(
        release=release,
        context=context,
        dataset_id="sales_dataset",
    )

    for changed_context in (
        service._selection_context(
            request=request.model_copy(update={"conversation_id": "conversation-b"}),
            published=published,
            dataset_ids=dataset_ids,
            actor_id="tenant-1",
            semantic_now=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        service._selection_context(
            request=request,
            published=published,
            dataset_ids=dataset_ids,
            actor_id="tenant-1",
            semantic_now=datetime(2026, 8, 2, tzinfo=UTC),
        ),
    ):
        with pytest.raises(SemanticParsingError) as exc_info:
            service._decode_selection_token(
                token,
                release=release,
                context=changed_context,
            )
        assert exc_info.value.code == "CANDIDATE_NOT_FOUND"


def test_signed_selection_token_expires_before_catalog_resolution(
    sales_release,
    monkeypatch,
) -> None:
    release = _routed_release(sales_release)
    service, _gateway, _executor = _service(release, query_embedding=False)
    service._selection_token_ttl_seconds = 1
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset", "customer_scope"),
    )
    monkeypatch.setattr("knowflow_analytics.query.service.time.time", lambda: 1_000)
    token = service._selection_token(
        release=release,
        context=context,
        dataset_id="sales_dataset",
    )
    monkeypatch.setattr("knowflow_analytics.query.service.time.time", lambda: 1_002)

    with pytest.raises(SemanticParsingError) as exc_info:
        service._decode_selection_token(
            token,
            release=release,
            context=context,
        )

    assert exc_info.value.code == "STALE_QUERY_SELECTION"


def test_dotted_semantic_ids_round_trip_inside_the_signed_opaque_token(
    sales_release,
) -> None:
    """A dot is legal inside governed IDs and remains opaque to the client."""

    release = _routed_release(sales_release)
    dotted = release.model_copy(
        update={
            "metrics": tuple(
                item.model_copy(update={"id": "finance.net_revenue"})
                if item.id == "net_revenue"
                else item
                for item in release.metrics
            ),
            "dimensions": tuple(
                item.model_copy(update={"id": "geo.customer.segment"})
                if item.id == "customer_segment"
                else item.model_copy(update={"id": "calendar.order.date"})
                if item.id == "order_date"
                else item
                for item in release.dimensions
            ),
            "dimension_values": tuple(
                item.model_copy(update={"id": "geo.cn.east"})
                if item == release.dimension_values[0]
                else item
                for item in release.dimension_values
            ),
        }
    )
    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset", "customer_scope"),
    )
    for semantic_selection_id in (
        "element:metric:finance.net_revenue",
        "element:dimension:geo.customer.segment",
        "value:geo.cn.east",
        "time:calendar.order.date",
    ):
        token = service._selection_token(
            release=dotted,
            context=context,
            semantic_selection_id=semantic_selection_id,
        )
        assert service._decode_selection_token(
            token,
            release=dotted,
            context=context,
        ) == (None, (semantic_selection_id,))
        assert semantic_selection_id not in token


def test_same_root_internal_scopes_are_not_duplicate_user_choices(
    sales_release,
) -> None:
    release = _routed_release(sales_release)
    sales = next(item for item in release.datasets if item.id == "sales_dataset")
    duplicate = sales.model_copy(update={"id": "sales_duplicate", "name": "内部变体"})
    base_route = next(
        item for item in release.analysis_topic_routes if item.dataset_id == "sales_dataset"
    )
    release = release.model_copy(
        update={
            "datasets": (*release.datasets, duplicate),
            "analysis_topic_routes": (
                *release.analysis_topic_routes,
                base_route.model_copy(update={"dataset_id": duplicate.id}),
            ),
        }
    )

    service, _gateway, _executor = _service(release, query_embedding=False)
    context = _selection_context_for(
        service,
        question="测试",
        dataset_ids=("sales_dataset", "sales_duplicate", "customer_scope"),
    )
    options = service._scope_choice_options(
        release,
        ("sales_dataset", "sales_duplicate", "customer_scope"),
        selection_context=context,
    )

    # 同一指标同时属于两个同根作用域，选了也定不下事实根——不给假选择。
    assert options == ()
    # The retired public-hash token algorithm is reproducible from Release API
    # fields.  Even a mathematically valid old token for the hidden duplicate
    # must not bypass the empty-option fail-close.
    forged_hidden_scope = (
        "scope_"
        + content_hash(
            {
                "kind": "query_scope",
                "dataset_id": "sales_duplicate",
                "release_spec_hash": release.spec_hash,
            }
        ).removeprefix("sha256:")[:20]
    )
    published = service._releases.published
    response = service.query(
        QueryRequest(
            project_id="sales",
            question="测试",
            dataset_ids=("sales_dataset", "sales_duplicate", "customer_scope"),
            selected_candidate_id=forged_hidden_scope,
            expected_release_id=published.release.id,
            expected_spec_hash=published.release.spec_hash,
            expected_index_snapshot_id=published.index_snapshot.id,
        )
    )
    assert response.state is QueryState.FAILED
    assert response.error.code == "CANDIDATE_NOT_FOUND"


def test_global_router_retrieves_once_then_parses_only_the_metric_owner_scope(
    sales_release,
) -> None:
    service, gateway, executor = _service(_routed_release(sales_release))

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            include_diagnostics=True,
        )
    )

    assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
    assert response.semantic_query.dataset_id == "sales_dataset"
    assert len(gateway.calls) == 1
    discovery = next(item for item in response.trace if item.stage.value == "CANDIDATE_DISCOVERY")
    assert discovery.detail["scope_resolution"]["code"] == "QUERY_SCOPE_SELECTED"
    assert discovery.detail["scope_resolution"]["selected_dataset_id"] == "sales_dataset"
    assert all(item.startswith("sales_dataset:") for item in discovery.detail["mapping_modes"])
    assert executor.calls == 1


def test_final_and_all_views_reuse_the_same_global_retrieval(sales_release) -> None:
    service, gateway, executor = _service(
        _routed_release(sales_release),
        llm_gateway=_InvalidS2SqlGateway(),
    )

    response = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            include_diagnostics=True,
        )
    )

    assert response.state is QueryState.FAILED
    assert len(gateway.calls) == 1
    assert executor.calls == 0
    parse_events = response.trace[-1].detail.get("parse_events", [])
    assert any(item["event"] == "all_mapping" for item in parse_events)


def test_distinct_exact_metrics_from_different_roots_fail_before_s2sql_parsing(
    sales_release,
) -> None:
    customer_metric = MetricSpec(
        id="customer_count",
        name="客户数量",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    customer_scope = DatasetSpec(
        id="customer_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(customer_metric.id,),
        dimension_ids=("customer_segment",),
    )
    release = sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, customer_metric),
            "datasets": (sales, customer_scope),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=customer_scope.id,
                    root_model_id="customers",
                ),
            ),
        }
    )
    service, gateway, executor = _service(
        release,
    )

    response = service.query(QueryRequest(project_id="sales", question="净收入和客户数量"))

    assert response.state is QueryState.FAILED
    assert response.error.code == "CROSS_FACT_METRICS_UNSUPPORTED"
    assert response.diagnostics.category == "routing"
    assert "拆开提问" in response.diagnostics.user_hint
    assert len(gateway.calls) == 1
    assert executor.calls == 0


def test_cross_root_metric_phrase_ambiguity_resumes_with_a_business_metric_choice(
    sales_release,
) -> None:
    customer_metric = MetricSpec(
        id="customer_revenue",
        name="净收入",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    customer_scope = DatasetSpec(
        id="customer_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(customer_metric.id,),
        dimension_ids=("customer_segment",),
    )
    release = sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, customer_metric),
            "datasets": (sales, customer_scope),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=customer_scope.id,
                    root_model_id="customers",
                ),
            ),
        }
    )
    service, _gateway, executor = _service(release)

    clarification = service.query(QueryRequest(project_id="sales", question="净收入"))

    assert clarification.state is QueryState.CLARIFICATION_REQUIRED
    dataset_ids = tuple(sorted(item.id for item in release.datasets))
    assert {
        _decoded_option(
            service,
            item,
            question="净收入",
            dataset_ids=dataset_ids,
        )[1]
        for item in clarification.options
    } == {
        "element:metric:net_revenue",
        "element:metric:customer_revenue",
    }
    assert {item.kind for item in clarification.options} == {"metric"}
    selected = _semantic_option(
        service,
        clarification.options,
        "element:metric:customer_revenue",
        question="净收入",
        dataset_ids=dataset_ids,
    )
    completed = service.query(
        QueryRequest(
            project_id="sales",
            question="净收入",
            selected_candidate_id=selected.candidate_id,
            expected_release_id=clarification.release_id,
            expected_spec_hash=clarification.spec_hash,
            expected_index_snapshot_id=clarification.index_snapshot_id,
        )
    )

    assert completed.state is QueryState.COMPLETED
    assert completed.semantic_query.dataset_id == "customer_scope"
    assert completed.semantic_query.metric_ids == ("customer_revenue",)
    assert executor.calls == 1


def test_verbatim_scope_name_anchors_a_cross_root_metric_collision_in_one_hop(
    sales_release,
) -> None:
    customer_metric = MetricSpec(
        id="customer_revenue",
        name="净收入",
        model_id="customers",
        field_id="customers.id",
        aggregation=Aggregation.COUNT_DISTINCT,
    )
    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": (
                "region",
                "channel",
                "order_date",
                "customer_segment",
            ),
        }
    )
    customer_scope = DatasetSpec(
        id="customer_scope",
        name="客户范围",
        model_ids=("customers",),
        metric_ids=(customer_metric.id,),
        dimension_ids=("customer_segment",),
    )
    release = sales_release.model_copy(
        update={
            "metrics": (*sales_release.metrics, customer_metric),
            "datasets": (sales, customer_scope),
            "analysis_topic_routes": (
                AnalysisTopicRouteSpec(
                    dataset_id=sales.id,
                    root_model_id="orders",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("orders_customer",),
                        ),
                    ),
                ),
                AnalysisTopicRouteSpec(
                    dataset_id=customer_scope.id,
                    root_model_id="customers",
                ),
            ),
        }
    )
    service, _gateway, executor = _service(release)

    completed = service.query(QueryRequest(project_id="sales", question="客户范围的净收入"))

    assert completed.state is QueryState.COMPLETED
    assert completed.semantic_query.dataset_id == "customer_scope"
    assert completed.semantic_query.metric_ids == ("customer_revenue",)
    assert executor.calls == 1


class TestScopeIsNeverAskedAboutDirectly:
    """作用域是内部执行计划，任何时候都不许问用户。

    实机（2026-09-03，demo_cafe）：「各门店的业绩」「各门店的营业额」「上个月各门店的
    收入」「哪个门店卖得最多」四题都弹出「销售单 / 销售明细 / 门店」让用户挑——用户的
    问题是「业绩」没被词典覆盖，卡片却让他去挑内部执行计划，答非所问，而且问一万次
    系统也不会变聪明。

    改成问指标：选项取候选作用域的受治理业务指标（不是从证据取——实测证据里唯一的
    指标候选是从「门店」召回的默认计数 门店数量，正是要避开的噪声）。选中即确定事实根，
    用户的选择还能回补业务词典。
    """

    def test_a_clarification_never_offers_an_analysis_object(self, sales_release) -> None:
        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=("sales_dataset", "customer_scope"),
            ),
            actor_id="tenant-1",
        )

        kinds = {item.kind for item in getattr(response, "options", ())}
        assert "analysis_object" not in kinds, response.model_dump_json(indent=2)

    def test_undecidable_scope_offers_governed_business_metrics(self, sales_release) -> None:
        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=("sales_dataset", "customer_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.CLARIFICATION_REQUIRED
        assert {item.kind for item in response.options} <= {"metric", "dimension"}
        assert "净收入" in {item.label for item in response.options}
        # 指标排在维度前面：更常见的意图先出现。
        kinds = [item.kind for item in response.options]
        assert kinds == sorted(kinds, key=lambda kind: kind != "metric")

    def test_choosing_a_metric_fixes_the_fact_root_and_runs(self, sales_release) -> None:
        """选中的指标决定事实根——用户答的是业务问题，路由是系统的事。"""

        release = _routed_release(sales_release)
        llm = _FixedS2SqlGateway('SELECT SUM("净收入") FROM "销售经营"')
        service, _gateway, executor = _service(release, llm_gateway=llm, query_embedding=False)
        dataset_ids = ("sales_dataset", "customer_scope")

        first = service.query(
            QueryRequest(project_id="sales", question="随便看看", dataset_ids=dataset_ids),
            actor_id="tenant-1",
        )
        chosen = next(item for item in first.options if item.label == "净收入")

        second = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=dataset_ids,
                selected_candidate_id=chosen.candidate_id,
                expected_release_id=first.release_id,
                expected_spec_hash=first.spec_hash,
                expected_index_snapshot_id=first.index_snapshot_id,
            ),
            actor_id="tenant-1",
        )

        assert second.state is QueryState.COMPLETED, second.model_dump_json(indent=2)
        assert second.semantic_query.dataset_id == "sales_dataset"
        assert second.semantic_query.metric_ids == ("net_revenue",)
        assert executor.calls == 1

    def test_a_chosen_metric_the_query_ignores_is_not_executed(self, sales_release) -> None:
        """结算义务：生成的 SQL 没用上用户选的指标就不执行。

        指标卡不是换个说法的作用域卡——用户选的是「我要看净收入」，回来一个别的
        指标就是答非所问，哪怕事实根碰巧是对的。
        """

        release = _routed_release(sales_release)
        llm = _FixedS2SqlGateway('SELECT SUM("退款金额") FROM "销售经营"')
        service, _gateway, executor = _service(release, llm_gateway=llm, query_embedding=False)
        dataset_ids = ("sales_dataset", "customer_scope")

        first = service.query(
            QueryRequest(project_id="sales", question="随便看看", dataset_ids=dataset_ids),
            actor_id="tenant-1",
        )
        chosen = next(item for item in first.options if item.label == "净收入")

        second = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=dataset_ids,
                selected_candidate_id=chosen.candidate_id,
                expected_release_id=first.release_id,
                expected_spec_hash=first.spec_hash,
                expected_index_snapshot_id=first.index_snapshot_id,
            ),
            actor_id="tenant-1",
        )

        assert second.state is not QueryState.COMPLETED, second.model_dump_json(indent=2)
        assert executor.calls == 0

    def test_a_forged_metric_outside_the_shown_card_is_refused(self, sales_release) -> None:
        """token 绑定的是实际展示过的组合；没展示过的指标不能靠改 id 混进来。"""

        release = _routed_release(sales_release)
        service, _gateway, executor = _service(release, query_embedding=False)
        dataset_ids = ("sales_dataset", "customer_scope")

        first = service.query(
            QueryRequest(project_id="sales", question="随便看看", dataset_ids=dataset_ids),
            actor_id="tenant-1",
        )
        forged = service._selection_token(
            release=release,
            context=_selection_context_for(
                service, question="随便看看", dataset_ids=dataset_ids
            ),
            dataset_id="customer_scope",
            semantic_selection_id="scope_choice:metric:net_revenue",
        )

        second = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=dataset_ids,
                selected_candidate_id=forged,
                expected_release_id=first.release_id,
                expected_spec_hash=first.spec_hash,
                expected_index_snapshot_id=first.index_snapshot_id,
            ),
            actor_id="tenant-1",
        )

        assert second.state is QueryState.FAILED
        assert executor.calls == 0

    def test_a_question_without_metric_intent_can_pick_a_dimension(
        self, sales_release
    ) -> None:
        """「各门店都卖些什么」这类问题一个指标都不想要。

        实测：只给指标时，它和「哪些门店售卖 X」都被逼着在「销售金额 / 销售数量」
        里挑一个——与之前那张弱指标卡是同一个病，只是换了个位置。
        """

        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=("sales_dataset", "customer_scope"),
            ),
            actor_id="tenant-1",
        )

        assert "dimension" in {item.kind for item in response.options}
