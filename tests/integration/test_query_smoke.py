from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.postgres import PostgresExecutor
from knowflow_analytics.gateways.embedding import HttpEmbeddingGateway
from knowflow_analytics.gateways.model import HttpModelGateway
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.modeling.profiler import PostgreSqlSemanticProfiler
from knowflow_analytics.query.parser import LlmS2SqlParser
from tests.integration.test_modeling_http_loop import (
    _PROJECT_ID,
    _SECRET,
    _Api,
    _build_catalog_via_product_api,
    _catalog_engine,
    _revision_path,
    _version,
)
from tests.support import create_sales_fixture


class _CountingModelGateway:
    def __init__(self, delegate: HttpModelGateway) -> None:
        self._delegate = delegate
        self.purposes: list[str] = []

    def generate_json(self, **kwargs: Any) -> dict[str, Any]:
        self.purposes.append(str(kwargs["purpose"]))
        return self._delegate.generate_json(**kwargs)


@pytest.mark.postgres
def test_small_query_baseline_uses_the_api_built_release():
    settings = _live_settings()
    create_sales_fixture(settings["database_url"])

    catalog_schema = f"query_smoke_{uuid.uuid4().hex}"
    catalog_admin_engine = create_engine(settings["database_url"])
    with catalog_admin_engine.begin() as connection:
        connection.execute(CreateSchema(catalog_schema))
    catalog_engine = _catalog_engine(settings["database_url"], catalog_schema)
    datasource_engine = create_engine(settings["database_url"])
    catalog = CatalogStore(catalog_engine)
    catalog.create_schema()
    executor = PostgresExecutor(settings["database_url"])
    http_model_gateway = HttpModelGateway(
        base_url=settings["ragflow_base_url"],
        service_token=settings["service_token"],
        tenant_id=settings["tenant_id"],
        llm_id=settings["llm_id"],
        timeout_seconds=240,
    )
    model_gateway = _CountingModelGateway(http_model_gateway)
    embedding_gateway = HttpEmbeddingGateway(
        base_url=settings["ragflow_base_url"],
        service_token=settings["service_token"],
        tenant_id=settings["tenant_id"],
        embedding_id=settings["embedding_id"],
        timeout_seconds=240,
    )
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=PostgreSqlIntrospector(datasource_engine),
        semantic_profiler=PostgreSqlSemanticProfiler(datasource_engine),
        executor=executor,
        embedding_gateway=embedding_gateway,
        llm_parser=LlmS2SqlParser(model_gateway),
        require_evaluation_for_publish=False,
    )
    try:
        with TestClient(
            create_api(
                application=application,
                service_secret=_SECRET,
                allow_debug_sql=True,
                requests_per_minute=1_000,
                expensive_requests_per_minute=1_000,
            )
        ) as client:
            api = _Api(client)
            revision = _build_catalog_via_product_api(api)
            revision = _review_region_dictionary(api, revision)
            revision = api.request("POST", _revision_path(revision, "validate"))
            published = api.request(
                "POST",
                _revision_path(revision, "publish"),
                {"confirmation": "publish"},
            )
            results = tuple(_run_case(api, item) for item in _selected_cases())

        failures = [item for item in results if not item["passed"]]
        report = {
            "release_id": published["release"]["id"],
            "spec_hash": published["release"]["spec_hash"],
            "total": len(results),
            "passed": len(results) - len(failures),
            "accuracy": (len(results) - len(failures)) / len(results),
            "silent_wrong_count": sum(item["silent_wrong"] for item in results),
            "false_accept_count": sum(item["false_accept"] for item in results),
            "false_refusal_count": sum(item["false_refusal"] for item in results),
            "model_call_count": len(model_gateway.purposes),
            "model_call_purposes": dict(Counter(model_gateway.purposes)),
            "parser_counts": dict(
                Counter(item["actual_parser"] for item in results if item["actual_parser"])
            ),
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, default=str))
        assert failures == []
        assert report["silent_wrong_count"] == 0
        assert report["false_accept_count"] == 0
        assert report["false_refusal_count"] == 0
    finally:
        http_model_gateway.close()
        embedding_gateway.close()
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()
        with catalog_admin_engine.begin() as connection:
            connection.execute(DropSchema(catalog_schema, cascade=True, if_exists=True))
        catalog_admin_engine.dispose()


def _review_region_dictionary(api: _Api, revision: dict[str, Any]) -> dict[str, Any]:
    preview = api.request(
        "POST",
        _revision_path(revision, "dimension-dictionary/previews"),
        {
            **_version(revision),
            "dimension_ids": ["dimension_region"],
        },
        operation="dimension_dictionary.preview",
    )
    aliases = {
        "华东": ["东区", "华东地区"],
        "华南": ["南区", "华南地区"],
    }
    result = api.request(
        "POST",
        _revision_path(
            revision,
            f"dimension-dictionary/previews/{preview['id']}/apply",
        ),
        {
            **_version(revision),
            "confirmation": "apply",
            "decisions": [
                {
                    "candidate_id": item["id"],
                    "accept": True,
                    "aliases": aliases.get(item["value"], []),
                }
                for item in preview["candidates"]
            ],
        },
        operation="dimension_dictionary.review",
    )
    return result["revision"]


def _run_case(api: _Api, case: dict[str, Any]) -> dict[str, Any]:
    response = api.request(
        "POST",
        "/v1/analytics/query",
        {
            "project_id": _PROJECT_ID,
            "question": case["question"],
            "dataset_ids": ["dataset_sales"],
            "include_debug_sql": True,
        },
    )
    expected_completed = case["state"] == "COMPLETED"
    actual_completed = response["state"] == "COMPLETED"
    state_matches = response["state"] == case["state"]
    semantics_match = state_matches
    result_matches = state_matches
    if expected_completed and actual_completed:
        query = response["semantic_query"]
        semantics_match = (
            sorted(query["metric_ids"]) == sorted(case["metrics"])
            and sorted(query["dimension_ids"]) == sorted(case["dimensions"])
            and query["filters"] == case.get("filters", [])
            and query["order_by"] == case.get("order_by", [])
            and query["limit"] == case.get("limit")
        )
        actual_rows = [tuple(row) for row in response["data"]["rows"]]
        expected_rows = [tuple(row) for row in case["rows"]]
        if case.get("unordered"):
            actual_rows.sort(key=str)
            expected_rows.sort(key=str)
        result_matches = actual_rows == expected_rows
    elif not expected_completed and state_matches:
        result_matches = response["error"]["code"] == case["error_code"]
    passed = state_matches and semantics_match and result_matches
    parser = next(
        (
            step.get("detail", {}).get("parser")
            for step in response.get("trace", [])
            if step.get("stage") == "FINAL_PARSING" and step.get("status") == "completed"
        ),
        None,
    )
    return {
        "id": case["id"],
        "question": case["question"],
        "passed": passed,
        "expected_state": case["state"],
        "actual_state": response["state"],
        "silent_wrong": bool(actual_completed and expected_completed and not passed),
        "false_accept": bool(actual_completed and not expected_completed),
        "false_refusal": bool(not actual_completed and expected_completed),
        "actual_parser": parser,
        "actual_semantic_query": response.get("semantic_query"),
        "actual_rows": response.get("data", {}).get("rows"),
        "actual_error": response.get("error"),
    }


def _cases() -> tuple[dict[str, Any], ...]:
    return (
        _case("q01", "净收入", metrics=["metric_net_revenue"], rows=[["380"]]),
        _case(
            "q02",
            "各区域净收入",
            metrics=["metric_net_revenue"],
            dimensions=["dimension_region"],
            rows=[["华东", "300"], ["华南", "80"]],
            unordered=True,
        ),
        _case(
            "q03",
            "华东净收入",
            metrics=["metric_net_revenue"],
            filters=[{"dimension_id": "dimension_region", "operator": "eq", "value": "华东"}],
            rows=[["300"]],
        ),
        _case(
            "q04",
            "各区域订单数",
            metrics=["metric_order_count"],
            dimensions=["dimension_region"],
            rows=[["华东", 2], ["华南", 1]],
            unordered=True,
        ),
        _case(
            "q05",
            "各区域净收入 Top 1",
            metrics=["metric_net_revenue"],
            dimensions=["dimension_region"],
            order_by=[{"element_id": "metric_net_revenue", "direction": "desc"}],
            limit=1,
            rows=[["华东", "300"]],
        ),
        _case(
            "q06",
            "2026年8月各区域净收入",
            metrics=["metric_net_revenue"],
            dimensions=["dimension_region"],
            filters=[
                {
                    "dimension_id": "dimension_order_date",
                    "operator": "gte",
                    "value": "2026-08-01",
                },
                {
                    "dimension_id": "dimension_order_date",
                    "operator": "lt",
                    "value": "2026-09-01",
                },
            ],
            rows=[["华东", "300"], ["华南", "80"]],
            unordered=True,
        ),
        _case(
            "q07",
            "各客户分层净收入",
            metrics=["metric_net_revenue"],
            dimensions=["dimension_customer_segment"],
            rows=[["重点", "100"], ["普通", "280"]],
            unordered=True,
        ),
        _case(
            "q08",
            "重点客户净收入",
            metrics=["metric_net_revenue"],
            filters=[
                {
                    "dimension_id": "dimension_customer_segment",
                    "operator": "eq",
                    "value": "重点",
                }
            ],
            rows=[["100"]],
        ),
        _case(
            "q09",
            "客单价",
            metrics=["metric_avg_order_value"],
            rows=[["126.6666666666666667"]],
        ),
        {
            "id": "q10",
            # Growth-rate wording is still refused up front; 同比/环比 now reach
            # the governed RATIO_OVER/RATIO_ROLL translation instead.
            "question": "各区域净收入增长率",
            "state": "FAILED",
            "error_code": "UNSUPPORTED_ANALYTIC_OPERATION",
        },
        {
            "id": "q11",
            "question": "明天天气怎么样",
            "state": "FAILED",
            "error_code": "NO_SEMANTIC_MAPPING",
        },
    )


def _selected_cases() -> tuple[dict[str, Any], ...]:
    cases = _cases()
    configured = os.getenv("KNOWFLOW_ANALYTICS_QUERY_CASE_IDS", "").strip()
    if not configured:
        return cases
    selected_ids = {item.strip() for item in configured.split(",") if item.strip()}
    unknown = selected_ids - {item["id"] for item in cases}
    if unknown:
        raise ValueError(f"unknown query baseline case ids: {sorted(unknown)}")
    return tuple(item for item in cases if item["id"] in selected_ids)


def _case(
    case_id: str,
    question: str,
    *,
    metrics: list[str],
    rows: list[list[Any]],
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order_by: list[dict[str, str]] | None = None,
    limit: int | None = None,
    unordered: bool = False,
) -> dict[str, Any]:
    return {
        "id": case_id,
        "question": question,
        "state": "COMPLETED",
        "metrics": metrics,
        "dimensions": dimensions or [],
        "filters": filters or [],
        "order_by": order_by or [],
        "limit": limit,
        "rows": rows,
        "unordered": unordered,
    }


def _live_settings() -> dict[str, str]:
    required = {
        "database_url": os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL"),
        # 租户由请求 actor 携带;冒烟脚本用它的执行者身份。
        "tenant_id": os.getenv("KNOWFLOW_SMOKE_TENANT_ID", "smoke-tenant"),
        "llm_id": os.getenv("KNOWFLOW_ANALYTICS_LLM_ID"),
        "embedding_id": os.getenv("KNOWFLOW_ANALYTICS_EMBEDDING_ID"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        pytest.skip(f"live query benchmark is missing settings: {', '.join(missing)}")
    return {
        **required,
        "ragflow_base_url": os.getenv(
            "KNOWFLOW_ANALYTICS_RAGFLOW_BASE_URL", "http://127.0.0.1:9380"
        ),
        "service_token": _service_token(),
    }


def _service_token() -> str:
    configured = os.getenv("RAGFLOW_SECRET_KEY")
    if configured:
        return configured
    env_path = Path(__file__).parents[3] / "docker" / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "RAGFLOW_SECRET_KEY" and value.strip():
                return value.strip().strip('"').strip("'")
    pytest.skip("RAGFLOW_SECRET_KEY is not configured")
