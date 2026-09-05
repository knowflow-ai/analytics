from __future__ import annotations

from datetime import UTC, datetime

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    Aggregation,
    AnalysisTopicPathSpec,
    AnalysisTopicRouteSpec,
    Cardinality,
    DatasetSpec,
    DimensionSpec,
    DimensionValueSpec,
    FieldKind,
    FieldSpec,
    MetricSpec,
    ModelSpec,
    QueryResult,
    RelationCondition,
    RelationSpec,
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
from knowflow_analytics.query.service import _UNION_DATASET_ID, AnalyticsQueryService
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

    # 同一事实根的两个内部作用域差异对用户不可见，他答不了。此前出的是一张零选项的
    # 澄清卡——那本质就是拒答，只是穿了卡片的壳。改为直说，并把话讲给能修的人听。
    assert first.state is QueryState.FAILED
    assert "重新发布" in first.diagnostics.user_hint or "重新发布" in str(first.error.message)
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
    # 作用域不再是生成前"选中"的，而是从生成结果里实际用到的成员反推出来的。
    assert discovery.detail["scope_resolution"]["code"] == "QUERY_SCOPE_DERIVED_FROM_QUERY"


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
    # 这条走的是 Rule 路径（夹具没给 LLM）：并集只在有 LLM 时启用，
    # 因为 Rule 不理解问句，给它更大的词汇表只会让它混根。
    assert discovery.detail["scope_resolution"]["code"] == "QUERY_SCOPE_SELECTED"
    assert discovery.detail["scope_resolution"]["selected_dataset_id"] == "sales_dataset"
    # 生成发生在并集作用域上（模型要看到全部候选成员），绑定才回到真实作用域——
    # 上面的 semantic_query.dataset_id 断言钉的就是绑定结果。
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

    def test_a_meaningless_question_is_refused_not_turned_into_a_menu(
        self, sales_release
    ) -> None:
        """一条证据都没有的问题，选完指标也走不下去——给菜单是把死路包装成选择。

        并集之前这里出的是指标卡。现在生成阶段直接告诉我们模型什么都表达不出来，
        照实拒答比让用户挑一个再撞墙诚实。
        """

        release = _routed_release(sales_release)
        # 并集只在有 LLM 时启用，所以这里必须给一个——没有 LLM 走的是 Rule 路径。
        service, _gateway, _executor = _service(
            release,
            llm_gateway=_FixedS2SqlGateway('SELECT SUM("净收入") FROM "销售经营"'),
            query_embedding=False,
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="随便看看",
                dataset_ids=("sales_dataset", "customer_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is not QueryState.CLARIFICATION_REQUIRED, (
            "一条证据都没有的问题，选完指标也走不下去——给菜单是把死路包装成选择"
        )


class _CapturingGaps:
    def __init__(self) -> None:
        self.saved: list[object] = []

    def save_failure(self, record, *, actor_id, project_id) -> None:
        self.saved.append(record)


class TestVocabularyGapIsKeptForTheModeler:
    """「系统没接住用户的说法」记在同一处，靠 kind 区分这一轮怎么收场。

    并集之后澄清大幅减少，`clarified` 这一类的输入随之变少——那是产品取舍，不影响这条
    记录契约本身：带正解的可以一键采纳成别名，拒答的还得人去诊断。端到端触发频率另议，
    这里钉的是记录器答应了什么。
    """

    def _service_with(self, sales_release, gaps):
        return _service(_routed_release(sales_release), query_embedding=False, query_failures=gaps)

    def test_an_unknown_filter_value_is_kept_with_its_near_miss(self, sales_release) -> None:
        gaps = _CapturingGaps()
        service, _gateway, _executor = self._service_with(sales_release, gaps)
        published = service._releases.published

        service._record_vocabulary_gap(
            QueryRequest(project_id="sales", question="哪些门店售卖卡布奇洛"),
            kind="unknown_value",
            published=published,
            effective_question="哪些门店售卖卡布奇洛",
            actor_id="tenant-1",
            code="UNKNOWN_FILTER_VALUE",
            message="「卡布奇洛」不在「商品名称」的已发布取值里",
            resolution="卡布奇诺",
        )

        assert len(gaps.saved) == 1
        assert gaps.saved[0].kind == "unknown_value"
        assert gaps.saved[0].resolution == "卡布奇诺"

    def test_a_refusal_carries_no_answer(self, sales_release) -> None:
        """拒答只知道失败了，正解未知——不能编一个出来。"""

        gaps = _CapturingGaps()
        service, _gateway, _executor = self._service_with(sales_release, gaps)

        service._record_vocabulary_gap(
            QueryRequest(project_id="sales", question="各城市有哪些门店"),
            kind="refused",
            published=service._releases.published,
            effective_question="各城市有哪些门店",
            actor_id="tenant-1",
            code="QUERY_EXECUTION_FAILED",
            message="postgres query failed",
        )

        assert gaps.saved[0].kind == "refused"
        assert gaps.saved[0].resolution == ""

    def test_recording_never_breaks_the_answer(self, sales_release) -> None:
        """记录是旁路：它自己出错不能把一次已经算好的回答变成失败。"""

        class _Exploding:
            def save_failure(self, record, *, actor_id, project_id):
                raise RuntimeError("disk full")

        service, _gateway, _executor = self._service_with(sales_release, _Exploding())

        service._record_vocabulary_gap(
            QueryRequest(project_id="sales", question="x"),
            kind="clarified",
            published=service._releases.published,
            effective_question="x",
            actor_id="tenant-1",
            code="SEMANTIC_CLARIFIED",
            message="用户确认要看的是「净收入」",
            resolution="净收入",
        )


class TestCrossScopeNameCollisionInTheUnion:
    """并集把不同作用域里同名的成员摆到一起，必须给限定名，不能靠"有重名就不用并集"绕开。

    两个作用域各自都叫「净收入」，各自内部毫无歧义；并集造出了单作用域里不存在的重名，
    模型无法用名字表达选择。真实仓库里每张事实表都有「金额」「数量」——按重名回退等于
    并集永远用不上。

    复用建模期跨模型重名治理的同一条确定性规则：成员名是模型名子串时用模型名，否则
    模型名+成员名。限定名只活在生成阶段，翻译前换回规范名——限定名指向哪个成员本身
    就是这次选择，换回去后只有拥有该成员的作用域能翻译成功，正好是绑定信号。
    """

    def _colliding(self, sales_release):
        from knowflow_analytics.contracts import Aggregation, DatasetSpec, MetricSpec

        customer_metric = MetricSpec(
            id="customer_revenue", name="净收入", model_id="customers",
            field_id="customers.id", aggregation=Aggregation.COUNT_DISTINCT,
        )
        customer_scope = DatasetSpec(
            id="customer_scope", name="客户范围", model_ids=("customers",),
            metric_ids=(customer_metric.id,), dimension_ids=("customer_segment",),
        )
        return _routed_release(sales_release).model_copy(
            update={
                "metrics": (*sales_release.metrics, customer_metric),
                "datasets": (sales_release.datasets[0], customer_scope),
            }
        )

    def test_colliding_members_get_a_qualified_name_in_the_union(self, sales_release) -> None:
        from knowflow_analytics.query.service import _union_scope

        built = _union_scope(self._colliding(sales_release), ("sales_dataset", "customer_scope"))

        assert built is not None, "有重名就退回原路径的话，并集永远用不上"
        generation_release, _union_id, renames = built
        names = {item.id: item.name for item in generation_release.metrics}
        assert names["net_revenue"] != names["customer_revenue"]
        # 裸名不能留下：符号表会因此重新变回歧义。
        assert "净收入" not in set(names.values())
        # 还原表按成员 ID 保身份：两个限定名会还原成同一个规范名，只换字符串会让
        # 目标作用域把不属于它的那个静默替换成自己那个。
        assert {name: value[0] for name, value in renames.items()} == {
            names["net_revenue"]: "net_revenue",
            names["customer_revenue"]: "customer_revenue",
        }

    def test_the_bare_name_is_not_left_as_an_alias(self, sales_release) -> None:
        from knowflow_analytics.query.service import _union_scope

        built = _union_scope(self._colliding(sales_release), ("sales_dataset", "customer_scope"))
        assert built is not None
        generation_release = built[0]

        for metric in generation_release.metrics:
            if metric.id in {"net_revenue", "customer_revenue"}:
                assert "净收入" not in metric.aliases

    def test_a_qualified_name_is_restored_before_binding(self, sales_release) -> None:
        """限定名换回规范名之后，只有拥有该成员的作用域能翻译成功——那就是绑定。"""
        from knowflow_analytics.query.service import _restore_union_names, _union_scope

        built = _union_scope(self._colliding(sales_release), ("sales_dataset", "customer_scope"))
        assert built is not None
        generation_release, _union_id, renames = built
        qualified = {item.id: item.name for item in generation_release.metrics}["net_revenue"]

        restored = _restore_union_names(
            f'SELECT SUM("{qualified}") FROM "销售经营"',
            renames,
            frozenset({"net_revenue"}),
        )

        assert '"净收入"' in restored
        assert qualified not in restored

    def test_a_qualified_name_outside_the_scope_makes_it_unusable(self, sales_release) -> None:
        """限定名指向的成员不属于该作用域时判定不成立——否则会被静默替换成同名的另一个。"""
        import pytest as _pytest

        from knowflow_analytics.query.errors import SemanticParsingError
        from knowflow_analytics.query.service import _restore_union_names, _union_scope

        built = _union_scope(self._colliding(sales_release), ("sales_dataset", "customer_scope"))
        assert built is not None
        generation_release, _union_id, renames = built
        qualified = {item.id: item.name for item in generation_release.metrics}["customer_revenue"]

        with _pytest.raises(SemanticParsingError):
            _restore_union_names(
                f'SELECT SUM("{qualified}") FROM "销售经营"',
                renames,
                frozenset({"net_revenue"}),
            )

    def test_a_residual_collision_still_falls_back(self, sales_release) -> None:
        """限定名也撞车时照旧 fail-closed 走原路径，不硬造名字。"""
        from knowflow_analytics.contracts import Aggregation, DatasetSpec, MetricSpec
        from knowflow_analytics.query.service import _union_scope

        # 同一个模型里的重名：限定名区分不了，仍由既有的同名澄清处理。
        twin = MetricSpec(
            id="orders_twin", name="净收入", model_id="orders",
            field_id="orders.net_amount", aggregation=Aggregation.SUM,
        )
        release = _routed_release(sales_release).model_copy(
            update={
                "metrics": (*sales_release.metrics, twin),
                "datasets": (
                    sales_release.datasets[0].model_copy(
                        update={"metric_ids": (*sales_release.datasets[0].metric_ids, twin.id)}
                    ),
                    DatasetSpec(
                        id="customer_scope", name="客户范围", model_ids=("customers",),
                        metric_ids=(), dimension_ids=("customer_segment",),
                    ),
                ),
            }
        )

        assert _union_scope(release, ("sales_dataset", "customer_scope")) is None

    def test_a_non_nested_tie_is_the_only_case_left_that_asks(self, sales_release) -> None:
        """并集之后，唯一还需要问人的是「生成完了仍有多个互不嵌套的事实根能执行」。

        嵌套的作用域由粒度收敛解开，同一事实根的重复由建模者去修——剩下的这种在冻结
        路由下极少见，正说明卡片已经退成兜底而不是常态。这里钉的是机制本身：非嵌套的
        并列解不开，而嵌套的能解开。
        """

        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)

        # 订单 —many_to_one→ 客户：嵌套，取最粗的那个。
        assert (
            service._coarsest_scope(release, ("sales_dataset", "customer_scope"))
            == "customer_scope"
        )
        # 没有从属关系时解不开——那时才问人。
        flat = release.model_copy(update={"relations": ()})
        assert service._coarsest_scope(flat, ("sales_dataset", "customer_scope")) is None

    def test_the_card_names_members_that_tell_the_scopes_apart(self, sales_release) -> None:
        """兜底卡的选项是能区分这几个范围的成员——选中成员即定下拥有它的范围。"""

        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)

        owners = service._scope_choice_owners(release, ("sales_dataset", "customer_scope"))
        distinguishing = {
            element_id for (_kind, element_id), scopes in owners.items() if len(scopes) == 1
        }

        assert distinguishing, "一个能区分的成员都没有，卡就是空的"
        # 两个范围共有的成员不进选项：选了它也定不下范围。
        assert "customer_segment" not in distinguishing

    def test_a_word_the_model_guessed_is_kept_as_a_weak_signal(self, sales_release) -> None:
        """并集之后澄清接近于零，词典就没有输入了——除非把"模型猜的"也记下来。

        「业绩」谁都没匹配上，模型看着全部成员自己挑了销售金额。它这次猜对了，但猜的
        结果不稳定（实机见过同一句话这次 SUM(金额) 下次 SUM(数量)）。同一说法被猜过很
        多次，本身就是该把它写进业务词典的信号——只是性质是"模型猜的"，不能和用户亲口
        确认的混为一谈。
        """

        from knowflow_analytics.query.service import _inferred_member_names

        release = _routed_release(sales_release)
        service, _gateway, _executor = _service(release, query_embedding=False)
        evidence = service._orchestrator.collect_evidence(
            question="各门店的销售额",
            dataset_ids=("sales_dataset",),
            index=service._releases.published.index_snapshot,
        )
        from knowflow_analytics.contracts import SemanticQuery

        # 精确命中的成员不算"猜的"。
        assert (
            _inferred_member_names(
                release,
                SemanticQuery(dataset_id="sales_dataset", metric_ids=("net_revenue",)),
                evidence,
            )
            == ()
        )
        # 没被任何精确证据命中的才算。
        assert "订单数" in _inferred_member_names(
            release,
            SemanticQuery(dataset_id="sales_dataset", metric_ids=("order_count",)),
            evidence,
        )

    def test_nothing_is_guessed_when_there_is_no_evidence_at_all(self, sales_release) -> None:
        """拿不到证据时不硬记：分不清是模型猜的还是根本没跑映射。"""

        from knowflow_analytics.contracts import SemanticQuery
        from knowflow_analytics.query.service import _inferred_member_names

        assert (
            _inferred_member_names(
                _routed_release(sales_release),
                SemanticQuery(dataset_id="sales_dataset", metric_ids=("net_revenue",)),
                None,
            )
            == ()
        )


class TestUnionRespectsColumnPermissions:
    """并集把多个作用域的成员摆到一起给模型看——列级白名单必须照样生效。

    这是权限边界：白名单在这里漏一个成员，用户就会在 Prompt 里看到他无权看的指标，
    而且模型可能真的用它出结果。
    """

    def test_a_hidden_metric_never_reaches_the_prompt(self, sales_release) -> None:
        release = _routed_release(sales_release)
        _gateway = _PromptCapturingGateway('SELECT SUM("净收入") FROM "销售经营"')
        service, _embedding, executor = _service(
            release, llm_gateway=_gateway, query_embedding=False
        )
        # 只放行「净收入」和一个维度，其余指标一律不可见。
        allowed = frozenset({"net_revenue", "region", "customer_segment"})

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="净收入",
                dataset_ids=("sales_dataset", "customer_scope"),
                allowed_element_ids=tuple(allowed),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
        prompts = " ".join(str(item) for item in _gateway.prompts)
        assert prompts, "没抓到 Prompt，这条断言就是空转"
        hidden = {
            item.name
            for item in release.metrics
            if item.id not in allowed and item.name not in {"净收入"}
        }
        for name in hidden:
            assert name not in prompts, f"白名单外的「{name}」出现在了 Prompt 里"

    def test_a_hidden_metric_cannot_be_used_even_if_the_model_names_it(
        self, sales_release
    ) -> None:
        """模型硬写一个被隐藏的成员时必须失败，不能靠"它看不到"当唯一防线。"""

        release = _routed_release(sales_release)
        service, _gateway, executor = _service(
            release,
            llm_gateway=_FixedS2SqlGateway('SELECT SUM("退款金额") FROM "销售经营"'),
            query_embedding=False,
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="净收入",
                dataset_ids=("sales_dataset", "customer_scope"),
                allowed_element_ids=("net_revenue", "region"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is not QueryState.COMPLETED
        assert executor.calls == 0


class _PromptCapturingGateway:
    """记下模型实际看到的 Prompt——不抓下来，权限断言就是空转。"""

    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.calls = 0
        self.prompts: list[object] = []

    def generate_json(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs.get("messages"))
        return {"thought": "t", "sql": self.sql}



def _two_facts_one_entity(sales_release):
    """两个事实根都挂在同一个实体上，彼此不成链。

    「客户分层」两边都能表达，但"按客户分层看什么"的答案完全不同（订单 / 退货）。
    这是并集反推里唯一会走到澄清分支的形态：粒度收敛不适用，两个绑定都成立。
    """

    returns_model = ModelSpec(
        id="returns", name="退货", schema_name="analytics_v0", table="returns"
    )
    returns_fields = (
        FieldSpec(
            id="returns.id",
            model_id="returns",
            name="退货ID",
            column="id",
            kind=FieldKind.IDENTIFIER,
        ),
        FieldSpec(
            id="returns.customer_id",
            model_id="returns",
            name="客户ID",
            column="customer_id",
            kind=FieldKind.IDENTIFIER,
        ),
    )
    returns_metric = MetricSpec(
        id="return_count",
        name="退货单量",
        model_id="returns",
        field_id="returns.id",
        aggregation=Aggregation.COUNT,
    )
    sales = sales_release.datasets[0].model_copy(
        update={
            "model_ids": ("orders", "customers"),
            "dimension_ids": ("region", "channel", "order_date", "customer_segment"),
        }
    )
    returns_scope = DatasetSpec(
        id="returns_scope",
        name="退货分析",
        model_ids=("returns", "customers"),
        metric_ids=(returns_metric.id,),
        dimension_ids=("customer_segment",),
    )
    return sales_release.model_copy(
        update={
            "models": (*sales_release.models, returns_model),
            "fields": (*sales_release.fields, *returns_fields),
            "metrics": (*sales_release.metrics, returns_metric),
            "relations": (
                *sales_release.relations,
                RelationSpec(
                    id="returns_customer",
                    left_model_id="returns",
                    right_model_id="customers",
                    cardinality=Cardinality.MANY_TO_ONE,
                    conditions=(
                        RelationCondition(
                            left_field_id="returns.customer_id",
                            right_field_id="customers.id",
                        ),
                    ),
                ),
            ),
            "datasets": (sales, returns_scope),
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
                    dataset_id=returns_scope.id,
                    root_model_id="returns",
                    paths=(
                        AnalysisTopicPathSpec(
                            target_model_id="customers",
                            relation_ids=("returns_customer",),
                        ),
                    ),
                ),
            ),
        }
    )


class _SequenceGateway:
    """按调用次序给不同的 SQL——用来分开"第一遍"和"ALL 重试"这两次生成。"""

    def __init__(self, *sqls: str) -> None:
        self.sqls = list(sqls)
        self.calls = 0

    def generate_json(self, **_kwargs):
        sql = self.sqls[min(self.calls, len(self.sqls) - 1)]
        self.calls += 1
        return {"thought": "t", "sql": sql}


class TestUnionSurvivesTheRetryPath:
    """并集只在生成阶段存在，而生成会发生两次（第一遍 + ALL 重试）。

    第二次要是拿不到并集，第一次写得出来的查询第二次就写不出来了——重试反而
    比第一次弱，且没有任何报错说明原因。
    """

    def test_the_all_retry_still_sees_the_union(self, sales_release) -> None:
        release = _routed_release(sales_release)
        gateway = _SequenceGateway(
            # 第一遍：引用一个谁都没有的成员，翻译在每个作用域上都失败。
            'SELECT SUM("查无此指标") FROM "销售经营"',
            # ALL 重试：写对了。它必须仍然能从并集里叫出「净收入」。
            'SELECT SUM("净收入") FROM "销售经营"',
        )
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="净收入",
                dataset_ids=("sales_dataset", "customer_scope"),
            ),
            actor_id="tenant-1",
        )

        assert gateway.calls == 2, "第一遍没失败，这条测试就没测到重试"
        assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
        assert executor.calls == 1

    def test_a_scope_clarification_is_not_swallowed_by_the_retry(
        self, sales_release
    ) -> None:
        """澄清不是"这遍没写好"，重试写一百遍也还是同一个问题。

        它必须直接抵达用户，而不是被当成解析失败塞进 ALL 重试——那样用户看到的
        会是"没答上来"，而不是本该出现的选择。
        """

        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway('SELECT "客户分层" FROM "销售经营" GROUP BY "客户分层"')
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="客户分层",
                dataset_ids=("sales_dataset", "returns_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.CLARIFICATION_REQUIRED, (
            response.model_dump_json(indent=2)
        )
        # 卡片必须是能答的：两个范围各自都要有代表，否则选哪个都到不了另一边。
        labels = {item.label for item in response.options}
        assert "净收入" in labels, labels
        assert "退货单量" in labels, labels
        # 共有成员不构成区分，出现在选项里等于给了一个选了也没用的答案。
        assert "客户分层" not in labels, labels
        # 作用域名本身永远不出现在用户面前。
        assert not {"销售经营", "退货分析"} & labels, labels
        assert gateway.calls == 1, "澄清被当成解析失败重试了"
        assert executor.calls == 0

    def test_choosing_a_member_from_that_card_actually_runs(self, sales_release) -> None:
        """卡片有选项不等于卡片能用。

        选中的成员必须真的把事实根定下来并跑出结果；否则用户点了一圈又回到同一
        张卡，比直接拒答更糟。
        """

        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway(
            'SELECT "客户分层" FROM "销售经营" GROUP BY "客户分层"',
            'SELECT "退货单量", "客户分层" FROM "退货分析" GROUP BY "客户分层"',
        )
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )
        request = QueryRequest(
            project_id="sales",
            question="客户分层",
            dataset_ids=("sales_dataset", "returns_scope"),
        )

        clarification = service.query(request, actor_id="tenant-1")
        chosen = next(
            item for item in clarification.options if item.label == "退货单量"
        )

        completed = service.query(
            request.model_copy(
                update={
                    "selected_candidate_id": chosen.candidate_id,
                    "expected_release_id": clarification.release_id,
                    "expected_spec_hash": clarification.spec_hash,
                    "expected_index_snapshot_id": clarification.index_snapshot_id,
                }
            ),
            actor_id="tenant-1",
        )

        assert completed.state is QueryState.COMPLETED, completed.model_dump_json(indent=2)
        assert executor.calls == 1

    def test_the_bound_sql_leaves_no_trace_of_the_union(self, sales_release) -> None:
        """执行之后，任何人能看到的 S2SQL 都必须是真实作用域的口径。

        它不只是给人看的：查询规则命中时会拿它去真实作用域上重新翻译，多轮改写把
        它交给下一轮的模型，回答卡的口径说明也读它。留着并集的限定名和表名，这三处
        全部指向一个不存在的作用域。
        """

        release = _two_facts_one_entity(sales_release)
        # 模型只看得见并集，所以它写的是并集那个名字（这里是「销售经营」）。
        gateway = _SequenceGateway(
            'SELECT "退货单量", "客户分层" FROM "销售经营" GROUP BY "客户分层"'
        )
        service, _embedding, _executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="各客户分层的退货单量",
                dataset_ids=("sales_dataset", "returns_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
        # 普通 wire 里连并集这个词都不该出现。（诊断投影是另一回事：它对 owner
        # 如实说明生成确实跑在并集上，那是解释而不是泄漏。）
        assert _UNION_DATASET_ID not in response.model_dump_json()
        # 并集借用的是另一个作用域的名字，绑定后必须换成真正执行的那个。
        assert "销售经营" not in response.corrected_s2sql, response.corrected_s2sql
        assert "退货分析" in response.corrected_s2sql

    def test_a_broken_query_is_not_reported_as_a_cross_root_question(
        self, sales_release
    ) -> None:
        """一个作用域都绑不上，不等于问题跨了事实根。

        实测（demo_cafe「每个门店卖得最好的商品是什么」）：模型写了 CTE，引用自己
        定义的列别名，用到的成员其实全在同一个作用域里。翻译器说的是"不认识
        _总销售金额_"，用户收到的却是"请拆开提问"——一个不用拆的问题被支开了。

        那条具体的 SQL 现在能跑了（见 `TestNamesDefinedInsideTheQuery`），所以这里改用
        另一种同类毛病：名字本身写错。它一样与事实根无关。
        """

        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway(
            'SELECT "客户分层", "查无此成员" FROM "销售经营" GROUP BY "客户分层"'
        )
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="各客户分层的退货单量",
                dataset_ids=("sales_dataset", "returns_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.FAILED
        assert response.error.code != "CROSS_FACT_METRICS_UNSUPPORTED", (
            "SQL 本身的毛病被说成了跨事实根"
        )
        assert executor.calls == 0

    def test_a_join_query_broken_everywhere_reports_its_own_defect(
        self, sales_release
    ) -> None:
        """一个认得全部成员的作用域仍翻不出，说出的必须是这条查询自己的毛病。

        实机「卖得最好的产品是哪个」：模型把聚合只写在 ORDER BY 里，每个作用域都
        拒掉；随后"问并集"撞上并集路由——它是把多个事实根的路径拼起来的，需要
        JOIN 的查询在它上面只会撞出 ANALYSIS_TOPIC_METRIC_OUTSIDE_ROOT /
        AMBIGUOUS_JOIN_PATH 这种与这条 SQL 无关、模型也改不了的码。这里用行数
        上限复现同一形态：销售作用域认得全部成员、只是行数超限；退货作用域根本
        没有「净收入」。
        """
        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway(
            'SELECT "客户分层", "净收入" FROM "销售经营" GROUP BY "客户分层" LIMIT 1000000'
        )
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )
        response = service.query(
            QueryRequest(
                project_id="sales",
                question="各客户分层的退货单量",
                dataset_ids=("sales_dataset", "returns_scope"),
                include_diagnostics=True,
            ),
            actor_id="tenant-1",
        )
        assert response.state is QueryState.FAILED
        assert "QUERY_SCOPE_DEFERRED_TO_GENERATION" in response.model_dump_json()
        assert response.error.code == "QUERY_LIMIT_EXCEEDED", response.error.model_dump_json()
        assert executor.calls == 0

    def test_a_genuinely_cross_root_query_still_says_so(self, sales_release) -> None:
        """反过来也要成立：真跨根时那句「请拆开提问」是对的，不能一起改没了。"""

        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway('SELECT "净收入", "退货单量" FROM "销售经营"')
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="净收入和退货单量",
                dataset_ids=("sales_dataset", "returns_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.FAILED
        assert response.error.code == "CROSS_FACT_METRICS_UNSUPPORTED", (
            response.error.model_dump_json()
        )
        assert executor.calls == 0

    def test_retargeting_leaves_the_querys_own_cte_names_alone(self, sales_release) -> None:
        """改表名只改作用域表，不动这条查询自己定义的 CTE。

        把 `FROM agg` 也改成作用域名，`ranked` 就转去读受治理表，CTE 里定义的别名随即
        失效——「每组取前 N」这类必然带 CTE 的查询会整条失败（实机「每个门店卖得最好的
        商品是什么」正是这样挂掉的）。
        """

        release = _two_facts_one_entity(sales_release)
        gateway = _SequenceGateway(
            'WITH agg AS ('
            ' SELECT "客户分层", SUM("退货单量") AS x FROM "销售经营" GROUP BY "客户分层"'
            '), ranked AS ('
            ' SELECT "客户分层", x, RANK() OVER (ORDER BY x DESC) AS rn FROM agg'
            ') SELECT "客户分层", x FROM ranked WHERE rn = 1'
        )
        service, _embedding, executor = _service(
            release, llm_gateway=gateway, query_embedding=False
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="退货单量最高的客户分层",
                dataset_ids=("sales_dataset", "returns_scope"),
            ),
            actor_id="tenant-1",
        )

        assert response.state is QueryState.COMPLETED, response.model_dump_json(indent=2)
        assert executor.calls == 1
        # CTE 名字原样保留，没有被改成作用域名。
        assert " agg" in response.corrected_s2sql, response.corrected_s2sql
