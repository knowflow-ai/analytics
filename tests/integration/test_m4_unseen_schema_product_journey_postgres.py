from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.modeling.introspector import SchemaIntrospector
from knowflow_analytics.modeling.profiler import DimensionValueProfiler
from knowflow_analytics.semantic.index import EmbeddingBatch

_SECRET = "m4-unseen-schema-product-journey-secret"


class _EmbeddingGateway:
    def encode(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        return EmbeddingBatch(
            model_id="m4-product-journey-embedding-v1",
            dimension=2,
            vectors=tuple((1.0, float(index % 2)) for index, _item in enumerate(texts)),
        )


@dataclass(frozen=True)
class _JourneyCase:
    project_id: str
    project_name: str
    schema_name: str
    fact_table: str
    lookup_table: str
    fact_name: str
    lookup_name: str
    fact_primary_key: str
    fact_foreign_key: str
    lookup_primary_key: str
    local_dimension_column: str
    local_dimension_name: str
    lookup_dimension_column: str
    lookup_dimension_name: str
    measure_column: str
    metric_name: str
    question: str
    expected_rows: tuple[tuple[str, int], ...]
    has_database_foreign_key: bool


_FK_CASE = _JourneyCase(
    project_id="m4-procurement-holdout",
    project_name="采购分析",
    schema_name="analytics_m4_procurement_holdout",
    fact_table="purchase_orders",
    lookup_table="suppliers",
    fact_name="采购订单",
    lookup_name="供应商",
    fact_primary_key="purchase_id",
    fact_foreign_key="supplier_code",
    lookup_primary_key="supplier_code",
    local_dimension_column="department",
    local_dimension_name="采购部门",
    lookup_dimension_column="supplier_tier",
    lookup_dimension_name="供应商等级",
    measure_column="total_cost",
    metric_name="采购金额",
    question="按供应商等级统计采购金额",
    expected_rows=(("战略", 200), ("普通", 50)),
    has_database_foreign_key=True,
)

_NO_FK_CASE = _JourneyCase(
    project_id="m4-support-holdout",
    project_name="服务工单分析",
    schema_name="analytics_m4_support_holdout",
    fact_table="support_cases",
    lookup_table="customer_accounts",
    fact_name="服务工单",
    lookup_name="客户账户",
    fact_primary_key="case_id",
    fact_foreign_key="account_code",
    lookup_primary_key="account_code",
    local_dimension_column="case_status",
    local_dimension_name="工单状态",
    lookup_dimension_column="account_region",
    lookup_dimension_name="客户区域",
    measure_column="resolution_minutes",
    metric_name="解决时长",
    question="按客户区域统计解决时长",
    expected_rows=(("华东", 50), ("华南", 40)),
    has_database_foreign_key=False,
)


class _Api:
    def __init__(self, client: TestClient, *, project_id: str) -> None:
        self.client = client
        self.project_id = project_id
        self.headers = {
            "X-KnowFlow-Service-Token": _SECRET,
            "X-KnowFlow-Actor-Id": "model-owner",
            "X-KnowFlow-Project-Id": project_id,
            "X-KnowFlow-Permission-Scope-Hash": "m4-product-journey-scope-v1",
        }

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.client.request(method, path, headers=self.headers, json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert isinstance(payload, dict)
        return payload


@pytest.mark.postgres
def test_fk_schema_completes_the_versioned_product_journey_and_survives_restart():
    """A database FK is only a candidate; the reviewed relation drives the topic path."""

    result = _run_product_journey(_FK_CASE)

    assert result["relation_source"] == "database_fk_candidate"
    assert result["topic_model_count"] == 2
    assert result["preview_rows"] == _normalized_rows(_FK_CASE.expected_rows)
    assert result["online_rows"] == _normalized_rows(_FK_CASE.expected_rows)


@pytest.mark.postgres
def test_no_fk_schema_never_guesses_a_relation_and_uses_the_reviewed_manual_path():
    """A business relation without a DB constraint exists only after explicit review."""

    result = _run_product_journey(_NO_FK_CASE)

    assert result["relation_source"] == "manual_relation"
    assert result["topic_models_before_relation"] == (_NO_FK_CASE.fact_table,)
    assert result["topic_model_count"] == 2
    assert result["preview_rows"] == _normalized_rows(_NO_FK_CASE.expected_rows)
    assert result["online_rows"] == _normalized_rows(_NO_FK_CASE.expected_rows)


def _run_product_journey(case: _JourneyCase) -> dict[str, Any]:
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    datasource_engine = create_engine(database_url)
    _create_unseen_schema(datasource_engine, case)
    catalog_admin_engine = create_engine(database_url)
    catalog_schema = f"m4_product_{uuid.uuid4().hex}"
    with catalog_admin_engine.begin() as connection:
        connection.execute(CreateSchema(catalog_schema))
    catalog_engine = _catalog_engine(database_url, catalog_schema)
    restarted_catalog_engine = None
    executor = SqlExecutor(database_url)
    application = AnalyticsApplication(
        catalog=CatalogStore(catalog_engine),
        introspector=SchemaIntrospector(datasource_engine),
        semantic_profiler=DimensionValueProfiler(datasource_engine),
        executor=executor,
        embedding_gateway=_EmbeddingGateway(),
        minimum_evaluation_cases=1,
    )
    application.catalog.create_schema()
    try:
        with TestClient(
            create_api(
                application=application,
                service_secret=_SECRET,
                requests_per_minute=1_000,
                expensive_requests_per_minute=200,
            )
        ) as client:
            api = _Api(client, project_id=case.project_id)
            revision = _import_tables(api, case)
            relations = revision["semantic_catalog"]["modelRelations"]
            if case.has_database_foreign_key:
                assert len(relations) == 1
                relation_source = "database_fk_candidate"
            else:
                assert relations == []
                relation_source = "manual_relation"

            revision = _configure_models(api, revision, case)
            before_relation = api.request(
                "POST",
                _revision_path(case, revision, "analysis-topic-proposals:generate"),
                _version(revision),
            )
            fact_model_id = _model_id(revision, case.fact_table)
            topic_models_before_relation = tuple(
                _table_name(revision, model_id)
                for proposal in before_relation["proposals"]
                if proposal["route"]["root_model_id"] == fact_model_id
                for model_id in proposal["dataset"]["model_ids"]
            )

            revision = _confirm_relation(api, revision, case)
            proposals = api.request(
                "POST",
                _revision_path(case, revision, "analysis-topic-proposals:generate"),
                _version(revision),
            )
            proposal = next(
                item
                for item in proposals["proposals"]
                if item["route"]["root_model_id"] == fact_model_id
            )
            assert len(proposal["route"]["paths"]) == 1
            revision = api.request(
                "PUT",
                _revision_path(
                    case,
                    revision,
                    f"analysis-topics/{proposal['dataset']['id']}",
                ),
                {
                    **_version(revision),
                    "dataset": proposal["dataset"],
                    "route": proposal["route"],
                },
            )
            validated = api.request(
                "POST",
                _revision_path(case, revision, "validate"),
            )
            assert validated["state"] == "validated"
            metric = next(
                item
                for item in validated["semantic_spec"]["metrics"]
                if item["name"] == case.metric_name
            )
            dimension = next(
                item
                for item in validated["semantic_spec"]["dimensions"]
                if item["name"] == case.lookup_dimension_name
            )
            dataset_id = proposal["dataset"]["id"]
            structured = api.request(
                "POST",
                _revision_path(case, validated, "structured-query-preview"),
                {
                    **_version(validated),
                    "semantic_query": {
                        "dataset_id": dataset_id,
                        "metric_ids": [metric["id"]],
                        "dimension_ids": [dimension["id"]],
                    },
                },
            )
            assert structured["state"] == "COMPLETED", structured
            natural = api.request(
                "POST",
                _revision_path(case, validated, "query-preview"),
                {
                    **_version(validated),
                    "question": case.question,
                    "dataset_ids": [dataset_id],
                },
            )
            assert natural["state"] == "COMPLETED", natural
            expected_rows = [list(item) for item in case.expected_rows]
            report = api.request(
                "POST",
                _revision_path(case, validated, "evaluate"),
                {
                    **_version(validated),
                    "required_accuracy": 1.0,
                    "suite": {
                        "id": f"suite-{case.project_id}",
                        "name": f"{case.project_name}产品链验收",
                        "project_id": case.project_id,
                        "cases": [
                            {
                                "id": f"case-{case.project_id}",
                                "question": case.question,
                                "dataset_ids": [dataset_id],
                                "expected_state": "COMPLETED",
                                "expected_dataset_id": dataset_id,
                                "expected_metric_ids": [metric["id"]],
                                "expected_aggregation_overrides": [
                                    {
                                        "metric_id": metric["id"],
                                        "aggregation": "sum",
                                    }
                                ],
                                "expected_dimension_ids": [dimension["id"]],
                                "expected_rows": expected_rows,
                                "row_order_matters": False,
                            }
                        ],
                    },
                },
            )
            assert report["gate_passed"] is True, json.dumps(
                report["results"], ensure_ascii=False, indent=2
            )
            published = api.request(
                "POST",
                _revision_path(case, validated, "publish"),
                {**_version(validated), "confirmation": "publish"},
            )

        catalog_engine.dispose()
        restarted_catalog_engine = _catalog_engine(database_url, catalog_schema)
        restarted = AnalyticsApplication(
            catalog=CatalogStore(restarted_catalog_engine),
            introspector=SchemaIntrospector(datasource_engine),
            semantic_profiler=DimensionValueProfiler(datasource_engine),
            executor=executor,
            embedding_gateway=_EmbeddingGateway(),
            minimum_evaluation_cases=1,
        )
        with TestClient(create_api(application=restarted, service_secret=_SECRET)) as client:
            api = _Api(client, project_id=case.project_id)
            loaded = api.request(
                "GET",
                f"/v1/analytics/projects/{case.project_id}/releases/{published['release']['id']}",
            )
            online = api.request(
                "POST",
                "/v1/analytics/query",
                {
                    "project_id": case.project_id,
                    "question": case.question,
                    "dataset_ids": [dataset_id],
                },
            )
            assert online["state"] == "COMPLETED", online
            assert online["release_id"] == loaded["release"]["id"]
            assert online["spec_hash"] == loaded["release"]["spec_hash"]

        return {
            "relation_source": relation_source,
            "topic_models_before_relation": topic_models_before_relation,
            "topic_model_count": len(proposal["dataset"]["model_ids"]),
            "preview_rows": _response_rows(natural),
            "online_rows": _response_rows(online),
        }
    finally:
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()
        if restarted_catalog_engine is not None:
            restarted_catalog_engine.dispose()
        with catalog_admin_engine.begin() as connection:
            connection.execute(DropSchema(catalog_schema, cascade=True, if_exists=True))
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{case.schema_name}" CASCADE')
        catalog_admin_engine.dispose()


def _import_tables(api: _Api, case: _JourneyCase) -> dict[str, Any]:
    api.request(
        "POST",
        "/v1/analytics/projects",
        {"name": case.project_name, "project_id": case.project_id},
    )
    snapshot = api.request(
        "POST",
        f"/v1/analytics/projects/{case.project_id}/schema-snapshots",
        {
            "schemas": [case.schema_name],
            "selected_tables": {
                case.schema_name: [case.lookup_table, case.fact_table],
            },
        },
    )
    revision = api.request(
        "POST",
        f"/v1/analytics/projects/{case.project_id}/revisions",
        {"schema_snapshot_id": snapshot["id"]},
    )
    for table_name in (case.lookup_table, case.fact_table):
        revision = api.request(
            "POST",
            _revision_path(case, revision, "models:from-table"),
            {
                **_version(revision),
                "schema_name": case.schema_name,
                "table_name": table_name,
            },
        )
    return api.request(
        "POST",
        _revision_path(case, revision, "decisions"),
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
    )


def _configure_models(
    api: _Api,
    revision: dict[str, Any],
    case: _JourneyCase,
) -> dict[str, Any]:
    fact_model_id = _model_id(revision, case.fact_table)
    lookup_model_id = _model_id(revision, case.lookup_table)
    lookup = _catalog_model(revision, lookup_model_id)
    lookup["name"] = case.lookup_name
    lookup["bizName"] = case.lookup_table
    lookup["description"] = f"{case.lookup_name}业务实体"
    lookup["modelDetail"]["identifiers"] = [
        {
            "name": f"{case.lookup_name}标识",
            "type": "primary",
            "bizName": case.lookup_primary_key,
            "isCreateDimension": 0,
        }
    ]
    lookup["modelDetail"]["dimensions"] = [
        _model_dimension(
            name=case.lookup_dimension_name,
            column=case.lookup_dimension_column,
        )
    ]
    lookup["modelDetail"]["measures"] = []
    revision = _save_model(api, revision, case, lookup)

    fact = _catalog_model(revision, fact_model_id)
    fact["name"] = case.fact_name
    fact["bizName"] = case.fact_table
    fact["description"] = f"{case.fact_name}事实实体"
    fact["modelDetail"]["identifiers"] = [
        {
            "name": f"{case.fact_name}标识",
            "type": "primary",
            "bizName": case.fact_primary_key,
            "isCreateDimension": 0,
        },
        {
            "name": f"{case.lookup_name}外部标识",
            "type": "foreign",
            "bizName": case.fact_foreign_key,
            "isCreateDimension": 0,
        },
    ]
    fact["modelDetail"]["dimensions"] = [
        _model_dimension(
            name=case.local_dimension_name,
            column=case.local_dimension_column,
        )
    ]
    fact["modelDetail"]["measures"] = [
        {
            "name": case.metric_name,
            "agg": "SUM",
            "expr": case.measure_column,
            "bizName": case.measure_column,
            "isCreateMetric": 1,
        }
    ]
    return _save_model(api, revision, case, fact)


def _confirm_relation(
    api: _Api,
    revision: dict[str, Any],
    case: _JourneyCase,
) -> dict[str, Any]:
    if case.has_database_foreign_key:
        relation = revision["semantic_catalog"]["modelRelations"][0]
        relation = {**relation, "knowflowCardinality": "many_to_one"}
    else:
        relation = {
            "id": f"relation-{case.fact_table}-{case.lookup_table}",
            "domainId": None,
            "fromModelId": _model_id(revision, case.fact_table),
            "toModelId": _model_id(revision, case.lookup_table),
            "joinType": "left join",
            "joinConditions": [
                {
                    "leftField": case.fact_foreign_key,
                    "rightField": case.lookup_primary_key,
                    "operator": "=",
                }
            ],
            "knowflowCardinality": "many_to_one",
        }
    return api.request(
        "PUT",
        _revision_path(case, revision, f"catalog/relations/{relation['id']}"),
        {**_version(revision), "relation": relation},
    )


def _save_model(
    api: _Api,
    revision: dict[str, Any],
    case: _JourneyCase,
    model: dict[str, Any],
) -> dict[str, Any]:
    return api.request(
        "PUT",
        _revision_path(
            case,
            revision,
            f"catalog/models/{model['id']}",
        ),
        {**_version(revision), "model": model},
    )


def _catalog_model(revision: dict[str, Any], model_id: str) -> dict[str, Any]:
    return next(
        deepcopy(item) for item in revision["semantic_catalog"]["models"] if item["id"] == model_id
    )


def _model_id(revision: dict[str, Any], table_name: str) -> str:
    return next(
        item["id"] for item in revision["semantic_spec"]["models"] if item["table"] == table_name
    )


def _table_name(revision: dict[str, Any], model_id: str) -> str:
    return next(
        item["table"] for item in revision["semantic_spec"]["models"] if item["id"] == model_id
    )


def _model_dimension(*, name: str, column: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "categorical",
        "expr": column,
        "bizName": column,
        "dataType": "TEXT",
        "isCreateDimension": 1,
        "description": name,
    }


def _revision_path(
    case: _JourneyCase,
    revision: dict[str, Any],
    suffix: str,
) -> str:
    return f"/v1/analytics/projects/{case.project_id}/revisions/{revision['id']}/{suffix}"


def _version(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "expected_etag": revision["etag"],
        "schema_snapshot_hash": revision["schema_snapshot_hash"],
    }


def _response_rows(response: dict[str, Any]) -> frozenset[tuple[str, Decimal]]:
    return frozenset((str(row[0]), Decimal(str(row[1]))) for row in response["data"]["rows"])


def _normalized_rows(rows: tuple[tuple[str, int], ...]) -> frozenset[tuple[str, Decimal]]:
    return frozenset((name, Decimal(value)) for name, value in rows)


def _catalog_engine(database_url: str, schema_name: str):
    return create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
    )


def _create_unseen_schema(engine, case: _JourneyCase) -> None:
    if case.has_database_foreign_key:
        statements = (
            f'DROP SCHEMA IF EXISTS "{case.schema_name}" CASCADE',
            f'CREATE SCHEMA "{case.schema_name}"',
            f'''
            CREATE TABLE "{case.schema_name}"."{case.lookup_table}" (
                supplier_code text PRIMARY KEY,
                supplier_tier text NOT NULL
            )
            ''',
            f'''
            CREATE TABLE "{case.schema_name}"."{case.fact_table}" (
                purchase_id bigint PRIMARY KEY,
                supplier_code text NOT NULL
                    REFERENCES "{case.schema_name}"."{case.lookup_table}"(supplier_code),
                department text NOT NULL,
                total_cost numeric NOT NULL
            )
            ''',
            f'''
            INSERT INTO "{case.schema_name}"."{case.lookup_table}" VALUES
                ('S-1', '战略'), ('S-2', '普通')
            ''',
            f'''
            INSERT INTO "{case.schema_name}"."{case.fact_table}" VALUES
                (101, 'S-1', '生产', 120),
                (102, 'S-1', '研发', 80),
                (103, 'S-2', '行政', 50)
            ''',
        )
    else:
        statements = (
            f'DROP SCHEMA IF EXISTS "{case.schema_name}" CASCADE',
            f'CREATE SCHEMA "{case.schema_name}"',
            f'''
            CREATE TABLE "{case.schema_name}"."{case.lookup_table}" (
                account_code text PRIMARY KEY,
                account_region text NOT NULL
            )
            ''',
            f'''
            CREATE TABLE "{case.schema_name}"."{case.fact_table}" (
                case_id bigint PRIMARY KEY,
                account_code text NOT NULL,
                case_status text NOT NULL,
                resolution_minutes numeric NOT NULL
            )
            ''',
            f'''
            INSERT INTO "{case.schema_name}"."{case.lookup_table}" VALUES
                ('A-1', '华东'), ('A-2', '华南')
            ''',
            f'''
            INSERT INTO "{case.schema_name}"."{case.fact_table}" VALUES
                (201, 'A-1', '已解决', 20),
                (202, 'A-1', '已解决', 30),
                (203, 'A-2', '处理中', 40)
            ''',
        )
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
