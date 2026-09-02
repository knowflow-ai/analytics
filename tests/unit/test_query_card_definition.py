"""概览卡片的「可重跑定义」接口合同。

卡片必须每次打开重算——钉结果就成了截图墙，打开永远是钉的那天的数字。所以需要
一份受治理语义查询。但普通查询响应**不出语义 ID** 是已评审合同
（`test_api_security` 里逐个 ID 断言），不能为了这个把它拆开。

于是单开一个接口：调用方明确索取，且只在「钉卡片」这一个动作上发生。取自诊断
产物，与下钻恢复基础查询同一处。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from knowflow_analytics.api import create_api
from knowflow_analytics.catalog.store import CatalogError
from knowflow_analytics.errors import AnalyticsError

_SECRET = "c" * 32
_HEADERS = {
    "X-KnowFlow-Service-Token": _SECRET,
    "X-KnowFlow-Actor-Id": "actor-1",
    "X-KnowFlow-Project-Id": "sales",
    "X-KnowFlow-Permission-Scope-Hash": "scope-hash",
}
_SEMANTIC_QUERY = {
    "dataset_id": "sales_dataset",
    "query_type": "aggregate",
    "metric_ids": ["metric:net_amount"],
    "dimension_ids": ["dimension:region"],
    "limit": 100,
}


class _Catalog:
    def __init__(self, artifact=None, raises=False):
        self._artifact = artifact
        self._raises = raises
        self.seen: dict[str, object] = {}

    def get_query_diagnostic(self, **kwargs):
        self.seen = kwargs
        if self._raises:
            raise CatalogError("purged")
        return self._artifact


def _artifact(response):
    return SimpleNamespace(
        response=response,
        release_id="rel-1",
        spec_hash="sha256:spec",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


def _app(catalog):
    from knowflow_analytics.application import AnalyticsApplication

    application = AnalyticsApplication.__new__(AnalyticsApplication)
    application.catalog = catalog
    return application


def _client(catalog):
    return TestClient(
        create_api(application=_app(catalog), service_secret=_SECRET),
        raise_server_exceptions=False,
    )


def test_it_returns_the_governed_query_and_its_version_binding():
    catalog = _Catalog(_artifact({"semantic_query": _SEMANTIC_QUERY}))

    response = _client(catalog).get(
        "/v1/analytics/projects/sales/query-card?query_id=q1", headers=_HEADERS
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["semantic_query"] == _SEMANTIC_QUERY
    # 版本绑定必须一起返回：卡片重跑时要能判断语义模型是否已经改版。
    assert payload["release_id"] == "rel-1"
    assert payload["spec_hash"] == "sha256:spec"


def test_it_is_scoped_to_the_asking_actor():
    """产物按 (actor, project, scope, query) 分槽。别人的回答钉不了。"""

    catalog = _Catalog(_artifact({"semantic_query": _SEMANTIC_QUERY}))

    _client(catalog).get(
        "/v1/analytics/projects/sales/query-card?query_id=q1", headers=_HEADERS
    )

    assert catalog.seen["actor_id"] == "actor-1"
    assert catalog.seen["project_id"] == "sales"
    assert catalog.seen["permission_scope_hash"] == "scope-hash"
    assert catalog.seen["query_id"] == "q1"


def test_an_expired_answer_is_refused_with_an_actionable_reason():
    """诊断产物 TTL 7 天、每人每项目 100 条。过期就是钉不了，重新问一次即可。

    重要的是**钉上之后**不再依赖产物存活——定义由调用方永久保存。TTL 只影响
    「能不能钉」这一刻。
    """

    with pytest.raises(AnalyticsError) as failure:
        _app(_Catalog(raises=True)).query_card_definition(
            project_id="sales",
            query_id="q1",
            actor_id="actor-1",
            permission_scope_hash="scope-hash",
        )

    assert failure.value.code == "QUERY_CARD_UNAVAILABLE"
    assert "重新提问" in str(failure.value)


def test_an_answer_without_a_rerunnable_query_is_refused():
    """澄清卡与失败回答没有语义查询，钉不上去——界面据此决定按钮何时出现。"""

    with pytest.raises(AnalyticsError) as failure:
        _app(_Catalog(_artifact({"state": "CLARIFICATION_REQUIRED"}))).query_card_definition(
            project_id="sales",
            query_id="q1",
            actor_id="actor-1",
            permission_scope_hash="scope-hash",
        )

    assert failure.value.code == "QUERY_CARD_UNSUPPORTED"


def test_it_requires_the_service_token():
    """这个接口出语义 ID，绝不能匿名可达。"""

    response = _client(_Catalog(_artifact({"semantic_query": _SEMANTIC_QUERY}))).get(
        "/v1/analytics/projects/sales/query-card?query_id=q1"
    )

    assert response.status_code == 401


def test_it_refuses_a_project_outside_the_request_scope():
    catalog = _Catalog(_artifact({"semantic_query": _SEMANTIC_QUERY}))

    response = _client(catalog).get(
        "/v1/analytics/projects/other/query-card?query_id=q1", headers=_HEADERS
    )

    assert response.status_code == 403


# ── 滚动时间窗 ──────────────────────────────────────────────────────
# 概览里「最近 N 天」几乎是最常见的一类卡。语义查询里的时间过滤是**绝对下界**，
# 直接钉上去这张卡就永远显示钉的那 30 天——不报错、不空白，安静地给旧窗口，
# 比报错更危险。


class TestRollingTimeWindow:
    def test_it_recomputes_the_bound_from_today(self):
        from datetime import datetime

        from knowflow_analytics.contracts import (
            FilterOperator,
            QueryFilter,
            SemanticQuery,
            SemanticQueryType,
        )
        from knowflow_analytics.query.service import apply_relative_time_window

        pinned = SemanticQuery(
            dataset_id="ds",
            query_type=SemanticQueryType.AGGREGATE,
            metric_ids=("m1",),
            dimension_ids=("d1",),
            filters=(
                QueryFilter(
                    dimension_id="t1", operator=FilterOperator.GTE, value="2026-08-03"
                ),
            ),
        )

        rolled = apply_relative_time_window(
            pinned, "t1", 30, now=datetime(2026, 12, 1, tzinfo=UTC)
        )

        bounds = [item.value for item in rolled.filters if item.dimension_id == "t1"]
        assert bounds == ["2026-11-01"]

    def test_it_replaces_rather_than_adds(self):
        """钉的那条绝对下界必须被换掉，不能与新窗口并存变成空集。"""

        from datetime import datetime

        from knowflow_analytics.contracts import (
            FilterOperator,
            QueryFilter,
            SemanticQuery,
            SemanticQueryType,
        )
        from knowflow_analytics.query.service import apply_relative_time_window

        pinned = SemanticQuery(
            dataset_id="ds",
            query_type=SemanticQueryType.AGGREGATE,
            metric_ids=("m1",),
            filters=(
                QueryFilter(
                    dimension_id="t1", operator=FilterOperator.GTE, value="2026-08-03"
                ),
                QueryFilter(dimension_id="d1", operator=FilterOperator.EQ, value="华东"),
            ),
        )

        rolled = apply_relative_time_window(
            pinned, "t1", 7, now=datetime(2026, 12, 1, tzinfo=UTC)
        )

        assert len([i for i in rolled.filters if i.dimension_id == "t1"]) == 1
        # 非时间的过滤原样保留：滚动的只是时间窗。
        assert any(i.dimension_id == "d1" and i.value == "华东" for i in rolled.filters)

    def test_the_endpoint_applies_it_before_executing(self):
        """端点要在执行前套上窗口，不能让调用方自己算日期——同一个语义里两份
        日期逻辑迟早漂移。"""

        seen: dict[str, object] = {}

        class _Application:
            def structured_query(self, request, **_kwargs):
                seen["filters"] = request.semantic_query.filters
                raise RuntimeError("stop after capturing")

        client = TestClient(
            create_api(application=_Application(), service_secret=_SECRET),
            raise_server_exceptions=False,
        )
        client.post(
            "/v1/analytics/structured-query",
            headers=_HEADERS,
            json={
                "project_id": "sales",
                "semantic_query": {
                    "dataset_id": "ds",
                    "query_type": "aggregate",
                    "metric_ids": ["m1"],
                    "filters": [
                        {"dimension_id": "t1", "operator": "gte", "value": "2020-01-01"}
                    ],
                },
                "time_window_dimension_id": "t1",
                "time_window_days": 30,
            },
        )

        bounds = [i.value for i in seen["filters"] if i.dimension_id == "t1"]
        assert bounds and bounds[0] != "2020-01-01"

    def test_without_a_window_the_pinned_bound_is_untouched(self):
        """不填窗口 = 固定区间。用户钉「8 月的数据」时要的就是它不动。"""

        seen: dict[str, object] = {}

        class _Application:
            def structured_query(self, request, **_kwargs):
                seen["filters"] = request.semantic_query.filters
                raise RuntimeError("stop after capturing")

        client = TestClient(
            create_api(application=_Application(), service_secret=_SECRET),
            raise_server_exceptions=False,
        )
        client.post(
            "/v1/analytics/structured-query",
            headers=_HEADERS,
            json={
                "project_id": "sales",
                "semantic_query": {
                    "dataset_id": "ds",
                    "query_type": "aggregate",
                    "metric_ids": ["m1"],
                    "filters": [
                        {"dimension_id": "t1", "operator": "gte", "value": "2020-01-01"}
                    ],
                },
            },
        )

        assert [i.value for i in seen["filters"]] == ["2020-01-01"]
