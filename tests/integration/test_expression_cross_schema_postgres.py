from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.contracts import SemanticQuery
from knowflow_analytics.execution.postgres import PostgresExecutor
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    ModelDefineType,
    SemanticCatalog,
)
from knowflow_analytics.semantic.translator import SemanticTranslator


def _catalog(schema_name: str) -> SemanticCatalog:
    """A non-sales cross-schema contract case built from catalog DTOs."""

    return SemanticCatalog.model_validate(
        {
            "projectId": "support-expression-contract",
            "revisionId": "revision-support-expression-contract",
            "models": [
                {
                    "id": "model_tickets",
                    "name": "服务工单",
                    "bizName": "tickets",
                    "description": "支持团队工单日快照",
                    "modelDetail": {
                        "queryType": "table_query",
                        "dbType": "postgresql",
                        "tableQuery": f"{schema_name}.tickets",
                        "filterSql": "state = 'active'",
                        "dimensions": [
                            {
                                "name": "周",
                                "type": "time",
                                "expr": "DATE_TRUNC('week', opened_at)",
                                "bizName": "opened_week",
                                "dataType": "TIMESTAMP",
                            },
                            {
                                "name": "队列",
                                "type": "categorical",
                                "expr": "queue",
                                "bizName": "queue",
                                "dataType": "TEXT",
                            },
                        ],
                        "fields": [
                            {"fieldName": "id", "dataType": "BIGINT"},
                            {"fieldName": "opened_at", "dataType": "TIMESTAMP"},
                            {"fieldName": "queue", "dataType": "TEXT"},
                            {"fieldName": "state", "dataType": "TEXT"},
                            {"fieldName": "opened_count", "dataType": "INTEGER"},
                            {"fieldName": "resolved_count", "dataType": "INTEGER"},
                        ],
                    },
                }
            ],
            "dimensions": [
                {
                    "id": "dimension_opened_week",
                    "name": "周",
                    "bizName": "opened_week",
                    "modelId": "model_tickets",
                    "type": "time",
                    "expr": "DATE_TRUNC('week', opened_at)",
                    "semanticType": "DATE",
                    "dataType": "TIMESTAMP",
                },
                {
                    "id": "dimension_queue",
                    "name": "队列",
                    "bizName": "queue",
                    "modelId": "model_tickets",
                    "type": "categorical",
                    "expr": "queue",
                    "semanticType": "CATEGORY",
                    "dataType": "TEXT",
                },
            ],
            "metrics": [
                {
                    "id": "metric_backlog_delta",
                    "name": "待办变化",
                    "bizName": "backlog_delta",
                    "description": "新开工单数减去解决工单数",
                    "modelId": "model_tickets",
                    "metricDefineType": "FIELD",
                    "metricDefineByFieldParams": {
                        "expr": "SUM(opened_count) - SUM(resolved_count)",
                        "fields": [
                            {"fieldName": "opened_count"},
                            {"fieldName": "resolved_count"},
                        ],
                    },
                }
            ],
            "dataSets": [
                {
                    "id": "dataset_support",
                    "name": "支持运营",
                    "bizName": "support_operations",
                    "dataSetDetail": {
                        "dataSetModelConfigs": [
                            {
                                "id": "model_tickets",
                                "includesAll": False,
                                "metrics": ["metric_backlog_delta"],
                                "dimensions": [
                                    "dimension_opened_week",
                                    "dimension_queue",
                                ],
                            }
                        ]
                    },
                }
            ],
        }
    )


@pytest.mark.postgres
def test_computed_dimensions_composite_metrics_and_filters_hold_across_schema_names():
    database_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL is not configured")
    schema_name = f"support_expression_{uuid.uuid4().hex}"
    engine = create_engine(database_url)
    executor = PostgresExecutor(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema_name}"')
            connection.exec_driver_sql(
                f"""
                CREATE TABLE "{schema_name}".tickets (
                    id BIGINT PRIMARY KEY,
                    opened_at TIMESTAMP NOT NULL,
                    queue TEXT NOT NULL,
                    state TEXT NOT NULL,
                    opened_count INTEGER NOT NULL,
                    resolved_count INTEGER NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                f"""
                INSERT INTO "{schema_name}".tickets VALUES
                  (1, TIMESTAMP '2026-08-03 08:00:00', 'A', 'active', 10, 2),
                  (2, TIMESTAMP '2026-08-04 08:00:00', 'A', 'active', 5, 1),
                  (3, TIMESTAMP '2026-08-04 09:00:00', 'B', 'active', 8, 8),
                  (4, TIMESTAMP '2026-08-05 08:00:00', 'A', 'closed', 100, 0)
                """
            )

        catalog = _catalog(schema_name)
        release = compile_semantic_catalog(catalog)
        query = SemanticQuery(
            dataset_id="dataset_support",
            metric_ids=("metric_backlog_delta",),
            dimension_ids=("dimension_queue",),
        )
        physical = SemanticTranslator().translate(release=release, query=query)
        result = executor.execute(query=physical, release=release)

        assert dict(result.rows) == {"A": 12, "B": 0}
        assert "opened_count" in physical.sql
        assert "resolved_count" in physical.sql
        assert "state" in physical.sql

        # Semantic labels and DTO list order are metadata, not runtime repair
        # inputs. Renaming/reordering must preserve the physical query.
        model = catalog.models[0]
        reordered_detail = model.model_detail.model_copy(
            update={"fields": tuple(reversed(model.model_detail.fields))}
        )
        metric = catalog.metrics[0]
        params = metric.metric_define_by_field_params.model_copy(
            update={"fields": tuple(reversed(metric.metric_define_by_field_params.fields))}
        )
        renamed = catalog.model_copy(
            update={
                "models": (
                    model.model_copy(
                        update={
                            "name": "服务请求",
                            "biz_name": "support_cases",
                            "model_detail": reordered_detail,
                        }
                    ),
                ),
                "dimensions": tuple(
                    item.model_copy(update={"name": f"显示名-{index}"})
                    for index, item in enumerate(catalog.dimensions)
                ),
                "metrics": (
                    metric.model_copy(
                        update={
                            "name": "未结变化",
                            "biz_name": "unresolved_delta",
                            "metric_define_by_field_params": params,
                        }
                    ),
                ),
                "data_sets": (catalog.data_sets[0].model_copy(update={"name": "客服运营"}),),
            }
        )
        renamed_release = compile_semantic_catalog(
            SemanticCatalog.model_validate(renamed.model_dump(mode="python"))
        )
        renamed_physical = SemanticTranslator().translate(
            release=renamed_release,
            query=query,
        )

        assert renamed_physical.sql == physical.sql
        assert renamed_physical.parameters == physical.parameters

        # DataModelNode parity: the same governed resources must execute when
        # their model source is a read-only SQL subquery instead of tableQuery.
        sql_detail = model.model_detail.model_copy(
            update={
                "query_type": ModelDefineType.SQL_QUERY,
                "table_query": None,
                "sql_query": f'SELECT * FROM "{schema_name}".tickets',
            }
        )
        sql_catalog = SemanticCatalog.model_validate(
            catalog.model_copy(
                update={"models": (model.model_copy(update={"model_detail": sql_detail}),)}
            ).model_dump(mode="python")
        )
        sql_release = compile_semantic_catalog(sql_catalog)
        sql_physical = SemanticTranslator().translate(
            release=sql_release,
            query=query,
        )
        sql_result = executor.execute(query=sql_physical, release=sql_release)

        assert dict(sql_result.rows) == {"A": 12, "B": 0}
        assert "FROM (SELECT * FROM" in sql_physical.sql

        week_query = SemanticTranslator().translate(
            release=release,
            query=SemanticQuery(
                dataset_id="dataset_support",
                metric_ids=("metric_backlog_delta",),
                dimension_ids=("dimension_opened_week",),
            ),
        )
        week_result = executor.execute(query=week_query, release=release)
        assert len(week_result.rows) == 1
        assert week_result.rows[0][1] == 12
    finally:
        executor.close()
        with engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        engine.dispose()
