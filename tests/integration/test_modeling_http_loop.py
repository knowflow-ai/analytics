from __future__ import annotations

import hashlib
import json
import os
import uuid
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
from knowflow_analytics.hashing import content_hash
from knowflow_analytics.modeling.coverage import (
    ProductChainEvidence,
    build_modeling_coverage_report,
)
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.modeling.profiler import PostgreSqlSemanticProfiler
from knowflow_analytics.semantic.index import EmbeddingBatch
from tests.support import create_sales_fixture

_SECRET = "modeling-http-contract-secret-v1"
_PROJECT_ID = "http-sales"


class _EmbeddingGateway:
    def for_tenant(self, _tenant_id: str):
        return self

    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        def vector(text: str) -> tuple[float, ...]:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            return tuple(1.0 if byte & 1 else -1.0 for byte in digest)

        return EmbeddingBatch(
            model_id="modeling-parity-embedding-v1",
            dimension=32,
            vectors=tuple(vector(item) for item in texts),
        )


class _Api:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.operations: list[str] = []
        self.headers = {
            "X-KnowFlow-Service-Token": _SECRET,
            "X-KnowFlow-Actor-Id": "model-owner",
            "X-KnowFlow-Project-Id": _PROJECT_ID,
            "X-KnowFlow-Permission-Scope-Hash": "modeling-parity-scope-v1",
        }

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        operation: str | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        response = self.client.request(method, path, headers=self.headers, json=body)
        assert response.status_code == 200, response.text
        if isinstance(operation, str):
            self.operations.append(operation)
        elif operation is not None:
            self.operations.extend(operation)
        result = response.json()
        assert isinstance(result, dict)
        return result


@pytest.mark.postgres
def test_real_postgres_is_modeled_published_and_reloaded_only_through_http(tmp_path):
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    create_sales_fixture(database_url)

    catalog_schema = f"modeling_parity_{uuid.uuid4().hex}"
    catalog_admin_engine = create_engine(database_url)
    with catalog_admin_engine.begin() as connection:
        connection.execute(CreateSchema(catalog_schema))
    catalog_engine = _catalog_engine(database_url, catalog_schema)
    restarted_catalog_engine = None
    datasource_engine = create_engine(database_url)
    catalog = CatalogStore(catalog_engine)
    catalog.create_schema()
    executor = PostgresExecutor(database_url)
    introspector = PostgreSqlIntrospector(datasource_engine)
    gateway = _EmbeddingGateway()
    application = AnalyticsApplication(
        catalog=catalog,
        introspector=introspector,
        semantic_profiler=PostgreSqlSemanticProfiler(datasource_engine),
        executor=executor,
        embedding_gateway=gateway,
        require_evaluation_for_publish=False,
        require_quality_report_for_publish=False,
    )
    try:
        with TestClient(
            create_api(
                application=application,
                service_secret=_SECRET,
                requests_per_minute=1_000,
                expensive_requests_per_minute=100,
            )
        ) as client:
            api = _Api(client)
            revision = _build_catalog_via_product_api(api)
            validated = api.request(
                "POST",
                _revision_path(revision, "validate"),
                operation="revision.validate",
            )
            assert validated["state"] == "validated"
            revision_catalog_hash = content_hash(validated["semantic_catalog"])
            published = api.request(
                "POST",
                _revision_path(revision, "publish"),
                {"confirmation": "publish"},
                operation="revision.publish",
            )

        # A new engine and CatalogStore simulate a process restart. Reusing the
        # original in-memory object would only prove Python object serialization.
        catalog_engine.dispose()
        restarted_catalog_engine = _catalog_engine(database_url, catalog_schema)
        restarted = AnalyticsApplication(
            catalog=CatalogStore(restarted_catalog_engine),
            introspector=introspector,
            executor=executor,
            embedding_gateway=gateway,
            require_evaluation_for_publish=False,
            require_quality_report_for_publish=False,
        )
        with TestClient(
            create_api(application=restarted, service_secret=_SECRET)
        ) as restarted_client:
            restarted_api = _Api(restarted_client)
            loaded = restarted_api.request(
                "GET",
                f"/v1/analytics/projects/{_PROJECT_ID}/releases/{published['release']['id']}",
                operation="release.reload",
            )
            executed = restarted_api.request(
                "POST",
                "/v1/analytics/query",
                {
                    "project_id": _PROJECT_ID,
                    "question": "按区域统计净收入",
                    "dataset_ids": ["dataset_sales"],
                },
                operation="query.execute",
            )
            assert executed["state"] == "COMPLETED", executed
            api.operations.extend(restarted_api.operations)

        release = loaded["release"]
        assert content_hash(release["modeling_catalog"]) == revision_catalog_hash
        assert release["spec_hash"] == published["release"]["spec_hash"]
        assert "upstreamCommit" not in release["modeling_catalog"]
        assert {item["metricDefineType"] for item in release["modeling_catalog"]["metrics"]} == {
            "FIELD",
            "MEASURE",
            "METRIC",
        }
        relations = release["modeling_catalog"]["modelRelations"]
        assert len(relations) == 1
        assert relations[0]["joinType"] == "left join"
        assert relations[0]["joinConditions"] == [
            {"leftField": "customer_id", "rightField": "id", "operator": "="}
        ]
        assert relations[0]["knowflowCardinality"] == "many_to_one"
        assert release["modeling_catalog"]["dataSets"][0]["dataSetDetail"]["dataSetModelConfigs"][
            1
        ]["dimensions"] == ["dimension_customer_segment"]
        parity_report = build_modeling_coverage_report(
            fixture_payload=json.loads(
                (Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json").read_text(
                    encoding="utf-8"
                )
            ),
            product_chain=ProductChainEvidence(
                api_sequence=tuple(api.operations),
                human_decisions=_m0_human_decisions(),
                revision_spec_hash=validated["semantic_spec"]["spec_hash"],
                revision_catalog_hash=revision_catalog_hash,
                release_spec_hash=release["spec_hash"],
                reloaded_catalog_hash=content_hash(release["modeling_catalog"]),
                authenticated_http_only=True,
                real_postgresql=True,
                restarted_and_reloaded=True,
                published_release_id=release["id"],
                executed_release_id=executed["release_id"],
                executed_spec_hash=executed["spec_hash"],
                query_state=executed["state"],
            ),
        )
        report_path = tmp_path / "modeling-parity-report.json"
        report_path.write_text(
            parity_report.model_dump_json(indent=2),
            encoding="utf-8",
        )
        reloaded_report = json.loads(report_path.read_text(encoding="utf-8"))
        assert reloaded_report["gate_passed"] is True
        assert reloaded_report["unreviewed_behaviors"] == []
    finally:
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()
        if restarted_catalog_engine is not None:
            restarted_catalog_engine.dispose()
        with catalog_admin_engine.begin() as connection:
            connection.execute(DropSchema(catalog_schema, cascade=True, if_exists=True))
        catalog_admin_engine.dispose()


def _catalog_engine(database_url: str, schema_name: str):
    return create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
    )


def _build_catalog_via_product_api(api: _Api) -> dict[str, Any]:
    api.request(
        "POST",
        "/v1/analytics/projects",
        {"name": "销售经营分析", "project_id": _PROJECT_ID},
    )
    snapshot = api.request(
        "POST",
        f"/v1/analytics/projects/{_PROJECT_ID}/schema-snapshots",
        {
            "schemas": ["analytics_v0"],
            "selected_tables": {"analytics_v0": ["customers"]},
        },
        operation="schema_snapshot.create",
    )
    revision = api.request(
        "POST",
        f"/v1/analytics/projects/{_PROJECT_ID}/revisions",
        {"schema_snapshot_id": snapshot["id"]},
        operation="revision.create_empty",
    )
    revision = api.request(
        "POST",
        _revision_path(revision, "models:from-table"),
        {
            **_version(revision),
            "schema_name": "analytics_v0",
            "table_name": "customers",
        },
        operation="model.create_from_table",
    )
    source_revision_id = revision["id"]
    revision = api.request(
        "POST",
        _revision_path(revision, "tables:extend"),
        {
            **_version(revision),
            "selected_tables": {"analytics_v0": ["orders"]},
        },
        operation="revision.extend_tables",
    )
    assert revision["parent_revision_id"] == source_revision_id
    assert revision["id"] != source_revision_id

    revision = api.request(
        "POST",
        _revision_path(revision, "decisions"),
        {
            **_version(revision),
            "decisions": [
                {
                    "suggestion_id": item["id"],
                    "accept": item["source"] == "database_constraint",
                }
                for item in revision["suggestions"]
            ],
        },
        operation="identifier.review",
    )
    models = {item["table"]: item["id"] for item in revision["semantic_spec"]["models"]}
    orders_id = models["orders"]
    customers_id = models["customers"]
    # Database FK metadata creates only a candidate. Match the
    # ModelRelationFormDrawer boundary by explicitly confirming cardinality
    # through the standalone relation resource before publication.
    relation_candidate = revision["semantic_catalog"]["modelRelations"][0]
    relation = {**relation_candidate, "knowflowCardinality": "many_to_one"}
    revision = api.request(
        "PUT",
        _revision_path(revision, f"catalog/relations/{relation['id']}"),
        {**_version(revision), "relation": relation},
        operation="relation.review",
    )

    for path, key, resource, operation in (
        (
            f"catalog/models/{orders_id}/dimensions/region",
            "dimension",
            _model_dimension("区域", "region", "categorical", "TEXT"),
            "model_dimension.upsert",
        ),
        (
            f"catalog/models/{orders_id}/dimensions/order_date",
            "dimension",
            {
                **_model_dimension("下单日期", "order_date", "partition_time", "DATE"),
                "typeParams": {"isPrimary": "true", "timeGranularity": "day"},
            },
            "model_dimension.upsert",
        ),
        (
            f"catalog/models/{customers_id}/dimensions/segment",
            "dimension",
            _model_dimension("客户分层", "segment", "categorical", "TEXT"),
            "model_dimension.upsert",
        ),
        (
            f"catalog/models/{orders_id}/measures/net_amount",
            "measure",
            _revenue_measure(),
            "measure.upsert",
        ),
        (
            "catalog/dimensions/dimension_region",
            "dimension",
            _dimension("dimension_region", "区域", "region", orders_id, "CATEGORY"),
            "dimension.upsert",
        ),
        (
            "catalog/dimensions/dimension_order_date",
            "dimension",
            {
                **_dimension(
                    "dimension_order_date",
                    "下单日期",
                    "order_date",
                    orders_id,
                    "DATE",
                ),
                "type": "partition_time",
                "typeParams": {"isPrimary": "true", "timeGranularity": "day"},
            },
            "dimension.upsert",
        ),
        (
            "catalog/dimensions/dimension_customer_segment",
            "dimension",
            _dimension(
                "dimension_customer_segment",
                "客户分层",
                "segment",
                customers_id,
                "CATEGORY",
            ),
            "dimension.upsert",
        ),
        (
            "catalog/metrics/metric_net_revenue",
            "metric",
            {
                "id": "metric_net_revenue",
                "name": "净收入",
                "bizName": "net_revenue",
                "description": "订单净金额合计",
                "modelId": orders_id,
                "alias": "收入,销售额",
                "metricDefineType": "MEASURE",
                "metricDefineByMeasureParams": {
                    "expr": "net_amount",
                    "measures": [_revenue_measure()],
                },
            },
            "metric.MEASURE.upsert",
        ),
        (
            "catalog/metrics/metric_order_count",
            "metric",
            {
                "id": "metric_order_count",
                "name": "订单数",
                "bizName": "order_count",
                "description": "去重订单数量",
                "modelId": orders_id,
                "metricDefineType": "FIELD",
                "metricDefineByFieldParams": {
                    "expr": "COUNT(DISTINCT id)",
                    "fields": [{"fieldName": "id"}],
                },
            },
            "metric.FIELD.upsert",
        ),
        (
            "catalog/metrics/metric_avg_order_value",
            "metric",
            {
                "id": "metric_avg_order_value",
                "name": "客单价",
                "bizName": "avg_order_value",
                "description": "净收入除以订单数",
                "modelId": orders_id,
                "metricDefineType": "METRIC",
                "metricDefineByMetricParams": {
                    "expr": "net_revenue / order_count",
                    "metrics": [
                        {"id": "metric_net_revenue", "bizName": "net_revenue"},
                        {"id": "metric_order_count", "bizName": "order_count"},
                    ],
                },
            },
            "metric.METRIC.upsert",
        ),
        (
            "catalog/datasets/dataset_sales",
            "data_set",
            {
                "id": "dataset_sales",
                "name": "销售分析",
                "bizName": "sales_analysis",
                "description": "订单与客户的受治理问数边界",
                "queryConfig": {
                    "detailTypeDefaultConfig": {
                        "timeDefaultConfig": {"unit": -1},
                        "limit": 500,
                    },
                    "aggregateTypeDefaultConfig": {
                        "timeDefaultConfig": {"unit": -1},
                        "limit": 200,
                    },
                },
                "dataSetDetail": {
                    "dataSetModelConfigs": [
                        {
                            "id": orders_id,
                            "includesAll": False,
                            "metrics": [
                                "metric_net_revenue",
                                "metric_order_count",
                                "metric_avg_order_value",
                            ],
                            "dimensions": ["dimension_region", "dimension_order_date"],
                        },
                        {
                            "id": customers_id,
                            "includesAll": False,
                            "metrics": [],
                            "dimensions": ["dimension_customer_segment"],
                        },
                    ]
                },
            },
            "dataset.upsert",
        ),
    ):
        revision = api.request(
            "PUT",
            _revision_path(revision, path),
            {**_version(revision), key: resource},
            operation=operation,
        )

    revision = api.request(
        "PUT",
        _revision_path(revision, "catalog/terms/revenue_term"),
        {
            **_version(revision),
            "term": {
                "id": "revenue_term",
                "name": "收入",
                "metric_ids": ["metric_net_revenue"],
            },
        },
        operation="term.upsert",
    )
    segment_value = next(
        item
        for item in revision["semantic_catalog"]["dimensionValues"]
        if item["dimension_id"] == "dimension_customer_segment" and item["value"] == "重点"
    )
    return api.request(
        "PUT",
        _revision_path(revision, f"catalog/dimension-values/{segment_value['id']}"),
        {
            **_version(revision),
            "dimension_value": {
                **segment_value,
                "display_name": "重点客户",
                "aliases": ["核心客户"],
            },
        },
        operation="dimension_value.upsert",
    )


def _revision_path(revision: dict[str, Any], suffix: str) -> str:
    return f"/v1/analytics/projects/{_PROJECT_ID}/revisions/{revision['id']}/{suffix}"


def _version(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_etag": revision["etag"],
        "schema_snapshot_hash": revision["schema_snapshot_hash"],
    }


def _model_dimension(name: str, field: str, kind: str, data_type: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "expr": field,
        "bizName": field,
        "dataType": data_type,
        "isCreateDimension": 0,
    }


def _dimension(
    dimension_id: str,
    name: str,
    field: str,
    model_id: str,
    semantic_type: str,
) -> dict[str, Any]:
    return {
        "id": dimension_id,
        "name": name,
        "bizName": field,
        "modelId": model_id,
        "type": "categorical",
        "expr": field,
        "semanticType": semantic_type,
        "dataType": "DATE" if semantic_type == "DATE" else "TEXT",
    }


def _revenue_measure() -> dict[str, Any]:
    return {
        "name": "净收入",
        "agg": "SUM",
        "expr": "net_amount",
        "bizName": "net_amount",
        "isCreateMetric": 0,
        "alias": "收入,销售额",
        "unit": "元",
    }


def _m0_human_decisions() -> tuple[str, ...]:
    return (
        "table_source",
        "field_classification",
        "identifier_subtype",
        "time_parameters",
        "measure_aggregation",
        "relation_join_and_cardinality",
        "metric_definitions",
        "dataset_scope",
        "business_dictionary",
        "publish_confirmation",
    )
