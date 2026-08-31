"""Drilldown continuation contract.

Tokens mirror sel1: opaque HMAC refs bound to the exact
actor/project/query/release context, recovered by re-enumerating frozen
Dataset membership.  The base semantics always come from the server side —
these tests freeze issuance, binding, expiry, and the continuation semantics.
"""

from __future__ import annotations

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    QueryMetricFilter,
    QueryOrder,
    QueryResult,
    SemanticQuery,
)
from knowflow_analytics.query.contracts import QueryState, StructuredQueryRequest
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.service import AnalyticsQueryService, _apply_drilldown
from knowflow_analytics.semantic import SemanticTranslator


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
    def execute(self, *, query, release):
        return QueryResult(
            columns=tuple(item.element_id for item in query.columns),
            rows=(),
            row_count=0,
        )


def _service(sales_release, sales_index) -> AnalyticsQueryService:
    return AnalyticsQueryService(
        releases=_ReleaseProvider(sales_release, sales_index),
        orchestrator=CandidateOrchestrator(mapper=SemanticMapper()),
        translator=SemanticTranslator(),
        executor=_Executor(),
        selection_secret="drilldown-secret-of-at-least-32-bytes",
    )


def _base_query() -> SemanticQuery:
    return SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("region",),
        limit=10,
    )


def _completed(service, *, actor_id):
    response = service.query_structured(
        StructuredQueryRequest(project_id="sales", semantic_query=_base_query()),
        actor_id=actor_id,
    )
    assert response.state is QueryState.COMPLETED
    return response


def test_completed_response_issues_governed_drilldown_options(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")

    labels = {(item.kind, item.label) for item in response.drilldown}
    # 已用成员（区域 / 净收入）不再作为候选；其余冻结成员按治理名签发。
    assert ("dimension", "区域") not in labels
    assert ("dimension", "渠道") in labels
    assert ("dimension", "客户分层") in labels
    assert ("metric", "退款金额") in labels
    assert all(item.token.startswith("drl1.") for item in response.drilldown)


def test_drilldown_is_not_issued_without_an_actor(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = service.query_structured(
        StructuredQueryRequest(project_id="sales", semantic_query=_base_query()),
    )
    assert response.drilldown == ()


def test_split_by_dimension_executes_structured_continuation(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    option = next(
        item for item in response.drilldown if item.kind == "dimension" and item.label == "渠道"
    )

    continuation = service.query_drilldown(
        project_id="sales",
        query_id=response.query_id,
        token=option.token,
        base_query=response.semantic_query,
        base_release_id=response.release_id,
        base_spec_hash=response.spec_hash,
        actor_id="user-1",
    )

    assert continuation.state is QueryState.COMPLETED
    assert continuation.semantic_query.dimension_ids == ("region", "channel")
    assert continuation.semantic_query.metric_ids == ("net_revenue",)
    # 续跑响应继续可钻，且不再提供已用的「渠道」。
    labels = {item.label for item in continuation.drilldown if item.kind == "dimension"}
    assert "渠道" not in labels


def test_switch_metric_replaces_projection(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    option = next(
        item for item in response.drilldown if item.kind == "metric" and item.label == "退款金额"
    )

    continuation = service.query_drilldown(
        project_id="sales",
        query_id=response.query_id,
        token=option.token,
        base_query=response.semantic_query,
        base_release_id=response.release_id,
        base_spec_hash=response.spec_hash,
        actor_id="user-1",
    )

    assert continuation.state is QueryState.COMPLETED
    assert continuation.semantic_query.metric_ids == ("refund_amount",)
    assert continuation.semantic_query.dimension_ids == ("region",)


def test_drilldown_token_binds_actor_query_and_release(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    option = response.drilldown[0]
    common = {
        "project_id": "sales",
        "base_query": response.semantic_query,
        "base_release_id": response.release_id,
        "base_spec_hash": response.spec_hash,
    }

    # 跨 actor 重放。
    with pytest.raises(SemanticParsingError) as exc:
        service.query_drilldown(
            query_id=response.query_id, token=option.token, actor_id="user-2", **common
        )
    assert exc.value.code == "CANDIDATE_NOT_FOUND"

    # 跨查询重放。
    with pytest.raises(SemanticParsingError) as exc:
        service.query_drilldown(
            query_id="q_other", token=option.token, actor_id="user-1", **common
        )
    assert exc.value.code == "CANDIDATE_NOT_FOUND"

    # 篡改签名。
    tampered = option.token[:-2] + ("AA" if not option.token.endswith("AA") else "BB")
    with pytest.raises(SemanticParsingError) as exc:
        service.query_drilldown(
            query_id=response.query_id, token=tampered, actor_id="user-1", **common
        )
    assert exc.value.code == "CANDIDATE_NOT_FOUND"

    # 发布移动后 fail-closed。
    with pytest.raises(SemanticParsingError) as exc:
        service.query_drilldown(
            project_id="sales",
            query_id=response.query_id,
            token=option.token,
            base_query=response.semantic_query,
            base_release_id=response.release_id,
            base_spec_hash="sha256:another-spec",
            actor_id="user-1",
        )
    assert exc.value.code == "STALE_QUERY_SELECTION"


def test_drilldown_token_expires(sales_release, sales_index, monkeypatch):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    option = response.drilldown[0]

    import knowflow_analytics.query.service as service_module

    real_time = service_module.time.time
    monkeypatch.setattr(service_module.time, "time", lambda: real_time() + 3_600)
    with pytest.raises(SemanticParsingError) as exc:
        service.query_drilldown(
            project_id="sales",
            query_id=response.query_id,
            token=option.token,
            base_query=response.semantic_query,
            base_release_id=response.release_id,
            base_spec_hash=response.spec_hash,
            actor_id="user-1",
        )
    assert exc.value.code == "STALE_QUERY_SELECTION"


def test_apply_drilldown_semantics():
    base = SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("region",),
        metric_filters=(
            QueryMetricFilter(metric_id="net_revenue", operator="gt", value=100),
        ),
        order_by=(
            QueryOrder(element_id="net_revenue", direction="desc"),
            QueryOrder(element_id="region", direction="asc"),
        ),
        limit=10,
    )

    split = _apply_drilldown(base, "dimension", "channel")
    assert split.dimension_ids == ("region", "channel")
    assert split.metric_filters == base.metric_filters
    assert split.order_by == base.order_by

    # 重复加同一维度不产生重复列。
    dedup = _apply_drilldown(base, "dimension", "region")
    assert dedup.dimension_ids == ("region",)

    switched = _apply_drilldown(base, "metric", "refund_amount")
    assert switched.metric_ids == ("refund_amount",)
    # 引用旧指标的过滤与排序被清掉，维度排序保留。
    assert switched.metric_filters == ()
    assert switched.aggregation_overrides == ()
    assert switched.order_by == (QueryOrder(element_id="region", direction="asc"),)
