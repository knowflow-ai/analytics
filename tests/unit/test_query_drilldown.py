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
    QueryFilter,
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

    labels = {(item.action, item.label) for item in response.drilldown}
    # 未用成员按治理名签发 add/replace；已用维度签发 remove（链条才能变短）。
    assert ("add", "区域") not in labels
    assert ("add", "渠道") in labels
    assert ("add", "客户分层") in labels
    assert ("remove", "区域") in labels
    assert ("remove", "渠道") not in labels
    assert ("replace", "退款金额") in labels
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
        item for item in response.drilldown if item.action == "add" and item.label == "渠道"
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
    # 续跑响应继续可钻：「渠道」从可加转为可移除。
    added = {item.label for item in continuation.drilldown if item.action == "add"}
    removable = {item.label for item in continuation.drilldown if item.action == "remove"}
    assert "渠道" not in added
    assert removable == {"区域", "渠道"}


def test_remove_dimension_shrinks_the_chain(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    split = service.query_drilldown(
        project_id="sales",
        query_id=response.query_id,
        token=next(
            item for item in response.drilldown if item.action == "add" and item.label == "渠道"
        ).token,
        base_query=response.semantic_query,
        base_release_id=response.release_id,
        base_spec_hash=response.spec_hash,
        actor_id="user-1",
    )
    assert split.semantic_query.dimension_ids == ("region", "channel")

    remove_region = next(
        item for item in split.drilldown if item.action == "remove" and item.label == "区域"
    )
    shrunk = service.query_drilldown(
        project_id="sales",
        query_id=split.query_id,
        token=remove_region.token,
        base_query=split.semantic_query,
        base_release_id=split.release_id,
        base_spec_hash=split.spec_hash,
        actor_id="user-1",
    )

    assert shrunk.state is QueryState.COMPLETED
    assert shrunk.semantic_query.dimension_ids == ("channel",)
    assert shrunk.semantic_query.metric_ids == ("net_revenue",)
    # 移除后「区域」回到可加集合。
    assert ("add", "区域") in {(item.action, item.label) for item in shrunk.drilldown}


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


def _with_default_time(sales_release):
    """Dataset 变体：声明受治理默认时间维，触发时间窗签发。"""

    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    return sales_release.model_copy(update={"datasets": (dataset,)})


def _filtered_query() -> SemanticQuery:
    return SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("region",),
        filters=(QueryFilter(dimension_id="region", operator="eq", value="华东"),),
        limit=10,
    )


def test_refilter_swaps_the_dimension_value(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = service.query_structured(
        StructuredQueryRequest(project_id="sales", semantic_query=_filtered_query()),
        actor_id="user-1",
    )
    assert response.state is QueryState.COMPLETED
    option = next(item for item in response.drilldown if item.action == "refilter")
    assert option.label == "区域"

    continuation = service.query_drilldown(
        project_id="sales",
        query_id=response.query_id,
        token=option.token,
        base_query=response.semantic_query,
        base_release_id=response.release_id,
        base_spec_hash=response.spec_hash,
        actor_id="user-1",
        value="华南",
    )

    assert continuation.state is QueryState.COMPLETED
    filters = continuation.semantic_query.filters
    assert len(filters) == 1
    assert filters[0].dimension_id == "region"
    assert filters[0].value == "华南"

    # refilter 不带值 fail-closed。
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
    assert exc.value.code == "DRILLDOWN_VALUE_REQUIRED"


def test_retime_replaces_the_governed_time_window(sales_release, sales_index):
    release = _with_default_time(sales_release)
    service = _service(release, sales_index)
    response = service.query_structured(
        StructuredQueryRequest(project_id="sales", semantic_query=_base_query()),
        actor_id="user-1",
    )
    windows = {item.label for item in response.drilldown if item.action == "retime"}
    assert windows == {"近 7 天", "近 30 天", "近 90 天", "不限时间"}

    option = next(
        item for item in response.drilldown if item.action == "retime" and item.label == "近 7 天"
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
    time_filters = [
        item for item in continuation.semantic_query.filters if item.dimension_id == "order_date"
    ]
    assert len(time_filters) == 1
    assert time_filters[0].operator.value == "gte"

    # 「不限时间」把时间窗过滤整体移除。
    all_time = next(
        item
        for item in continuation.drilldown
        if item.action == "retime" and item.label == "不限时间"
    )
    cleared = service.query_drilldown(
        project_id="sales",
        query_id=continuation.query_id,
        token=all_time.token,
        base_query=continuation.semantic_query,
        base_release_id=continuation.release_id,
        base_spec_hash=continuation.spec_hash,
        actor_id="user-1",
    )
    assert cleared.state is QueryState.COMPLETED
    assert not [
        item for item in cleared.semantic_query.filters if item.dimension_id == "order_date"
    ]


def test_no_time_windows_without_a_governed_default_time_dimension(sales_release, sales_index):
    service = _service(sales_release, sales_index)
    response = _completed(service, actor_id="user-1")
    assert not [item for item in response.drilldown if item.action == "retime"]


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

    split = _apply_drilldown(base, "add", "channel")
    assert split.dimension_ids == ("region", "channel")
    assert split.metric_filters == base.metric_filters
    assert split.order_by == base.order_by

    # 重复加同一维度不产生重复列。
    dedup = _apply_drilldown(base, "add", "region")
    assert dedup.dimension_ids == ("region",)

    switched = _apply_drilldown(base, "replace", "refund_amount")
    assert switched.metric_ids == ("refund_amount",)
    # 引用旧指标的过滤与排序被清掉，维度排序保留。
    assert switched.metric_filters == ()
    assert switched.aggregation_overrides == ()
    assert switched.order_by == (QueryOrder(element_id="region", direction="asc"),)

    removed = _apply_drilldown(split, "remove", "region")
    assert removed.dimension_ids == ("channel",)
    # 被移除维度的排序引用一并清掉；指标过滤（独立语义）保留。
    assert removed.order_by == (QueryOrder(element_id="net_revenue", direction="desc"),)
    assert removed.metric_filters == base.metric_filters


def test_visualization_marks_groupless_ratio_as_ratio_chart(sales_release):
    ratio_query = SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=(),
    )
    marked = AnalyticsQueryService._visualization(
        sales_release,
        ratio_query,
        "SELECT RATIO_TO_TOTAL(\"净收入\", \"区域\", '华东') FROM \"销售经营\"",
    )
    assert marked["type"] == "ratio"

    # 普通无分组聚合仍是 table；带分组的占比仍走 bar（可切饼图）。
    plain = AnalyticsQueryService._visualization(sales_release, ratio_query, "SELECT 1")
    assert plain["type"] == "table"
    grouped = AnalyticsQueryService._visualization(
        sales_release,
        SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
        ),
        "SELECT RATIO_TO_TOTAL(...)",
    )
    assert grouped["type"] == "bar"


def test_period_ratio_projection_keeps_aliased_columns_and_marks_delta(
    sales_release,
    sales_index,
):
    """同比/环比：结果列是 S2SQL 别名而非语义 ID。

    只按语义 ID 映射会让整行退化成「结果列 N」、并清空图表轴——
    这是曾经的线上表现，此测试锁住修复。
    """

    from knowflow_analytics.api import _ordinary_query_projection
    from knowflow_analytics.query.orchestrator import CandidateOrchestrator
    from knowflow_analytics.query.parser import LlmS2SqlParser
    from knowflow_analytics.query.contracts import QueryRequest
    from knowflow_analytics.semantic.index import EmbeddingBatch, SemanticIndexBuilder

    class _Embedding:
        def encode(self, texts):
            return EmbeddingBatch(
                model_id="constant", dimension=1, vectors=tuple((1.0,) for _ in texts)
            )

    class _Gateway:
        def generate_json(self, **_kwargs):
            return {
                "thought": "按月同比",
                "sql": (
                    'SELECT DATE_TRUNC(\'month\', "下单日期") AS "月份", '
                    'RATIO_OVER("净收入") AS "同比" FROM "销售经营" '
                    "GROUP BY DATE_TRUNC('month', \"下单日期\")"
                ),
            }

    class _RowExecutor:
        def execute(self, *, query, release):
            return QueryResult(
                columns=tuple(item.element_id for item in query.columns),
                rows=(("2026-08", 0.9),),
                row_count=1,
            )

    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    release = sales_release.model_copy(update={"datasets": (dataset,)})
    index = SemanticIndexBuilder(_Embedding()).build(release)
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(), llm_parser=LlmS2SqlParser(_Gateway())
        ),
        translator=SemanticTranslator(),
        executor=_RowExecutor(),
        selection_secret="drilldown-secret-of-at-least-32-bytes",
    )

    response = service.query(
        QueryRequest(
            project_id="sales", question="按月净收入同比", dataset_ids=("sales_dataset",)
        ),
        actor_id="user-1",
    )
    assert response.state is QueryState.COMPLETED

    projected = _ordinary_query_projection(response)
    assert projected["data"]["columns"] == ["月份", "同比"]
    # 派生时间维（DATE_TRUNC）是分组轴，比率列是数值系列且标记为增长率。
    assert projected["visualization"]["x"] == "月份"
    assert projected["visualization"]["y"] == ["同比"]
    assert projected["visualization"]["y_formats"] == ["delta"]


def test_ratio_column_alone_is_percent_formatted_not_the_metric_beside_it(
    sales_release,
):
    """SUM(指标) 与 RATIO_OVER(指标) 并列时，只有比率列按百分比展示。

    两列引用同一个指标、都不是直接语义投影，只看表达式区分不出谁是比率。
    曾把两列都标成 delta —— 380 会渲染成 +38000%，比不格式化更糟。
    """

    from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    release = sales_release.model_copy(update={"datasets": (dataset,)})
    s2sql = (
        'SELECT DATE_TRUNC(\'year\', "下单日期") AS "_年_", '
        'SUM("净收入") AS "_净收入_", '
        'RATIO_OVER("净收入") AS "_同比_" '
        'FROM "销售经营" GROUP BY DATE_TRUNC(\'year\', "下单日期")'
    )
    translated = S2SqlSemanticTranslator().translate(
        release=release, dataset_id="sales_dataset", corrected_s2sql=s2sql
    )

    kinds = {item.element_id: item.kind for item in translated.physical_query.columns}
    assert kinds == {"_年_": "dimension", "_净收入_": "calculation", "_同比_": "ratio"}

    visualization = AnalyticsQueryService._visualization(
        release, translated.audit_query, s2sql, translated.physical_query.columns
    )
    assert visualization["y"] == ("_净收入_", "_同比_")
    assert visualization["y_formats"] == ["number", "delta"]
    # 别名的包裹下划线不进展示名。
    assert AnalyticsQueryService._column_labels(
        release, translated.physical_query.columns
    ) == ("年", "净收入", "同比")
