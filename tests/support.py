from __future__ import annotations

from sqlalchemy import create_engine

from knowflow_analytics.contracts import QueryFilter, SemanticQuery, SemanticRelease
from knowflow_analytics.evaluation.contracts import GoldenSuite
from knowflow_analytics.query.parser import serialize_s2sql


class GoldenS2SqlGateway:
    """Deterministic LLM boundary for the governed sales calibration suite.

    The fixture returns the suite's human-adjudicated semantic query. It must not
    copy RuleSqlParser output because selectedParseInfo carries only
    dataset/mapping scope into final LLM parsing, never a Rule S2SQL prompt seed.
    This is test-only adjudication data and is never imported by production code.
    """

    def __init__(self, suite: GoldenSuite, release: SemanticRelease) -> None:
        self._sql_by_question = {
            _normalized_question(case.question): serialize_s2sql(
                SemanticQuery(
                    dataset_id=case.expected_dataset_id or _single_dataset(case.dataset_ids),
                    query_type=case.expected_query_type,
                    metric_ids=case.expected_metric_ids,
                    aggregation_overrides=case.expected_aggregation_overrides,
                    dimension_ids=case.expected_dimension_ids,
                    filters=tuple(
                        QueryFilter(
                            dimension_id=item.dimension_id,
                            operator=item.operator,
                            value=item.value,
                        )
                        for item in case.expected_filters
                    ),
                    measure_filters=case.expected_measure_filters,
                    metric_filters=case.expected_metric_filters,
                    order_by=case.expected_order_by or (),
                    limit=case.expected_limit,
                ),
                release=release,
            )
            for case in suite.cases
            if case.expected_metric_ids or case.expected_dimension_ids
        }

    def generate_json(self, **kwargs):
        assert all("rule_seed" not in item["content"] for item in kwargs["messages"])
        user_message = kwargs["messages"][-1]["content"]
        question = user_message.splitlines()[0].removeprefix("question=")
        sql = self._sql_by_question.get(_normalized_question(question))
        if sql is None:
            raise AssertionError(f"question has no adjudicated S2SQL fixture: {question}")
        return {"thought": "固定黄金语义合同", "sql": sql}


def _normalized_question(value: str) -> str:
    return " ".join(value.split()).casefold()


def _single_dataset(dataset_ids: tuple[str, ...]) -> str:
    if len(dataset_ids) != 1:
        raise ValueError("adjudicated completed case requires one expected dataset")
    return dataset_ids[0]


def create_sales_fixture(database_url: str) -> None:
    engine = create_engine(database_url)
    statements = (
        "CREATE SCHEMA IF NOT EXISTS analytics_v0",
        "DROP TABLE IF EXISTS analytics_v0.order_items",
        "DROP TABLE IF EXISTS analytics_v0.orders",
        "DROP TABLE IF EXISTS analytics_v0.customers",
        "CREATE TABLE analytics_v0.customers (id bigint PRIMARY KEY, segment text NOT NULL)",
        """
        CREATE TABLE analytics_v0.orders (
            id bigint PRIMARY KEY,
            customer_id bigint NOT NULL REFERENCES analytics_v0.customers(id),
            region text NOT NULL,
            channel text NOT NULL,
            net_amount numeric NOT NULL,
            refund_amount numeric NOT NULL,
            order_date date NOT NULL
        )
        """,
        """
        CREATE TABLE analytics_v0.order_items (
            id bigint PRIMARY KEY,
            order_id bigint NOT NULL REFERENCES analytics_v0.orders(id),
            product text NOT NULL
        )
        """,
        "INSERT INTO analytics_v0.customers VALUES (1, '重点'), (2, '普通')",
        """
        INSERT INTO analytics_v0.orders VALUES
          (1, 1, '华东', '直营', 100, 5, DATE '2026-08-01'),
          (2, 2, '华东', '电商', 200, 0, DATE '2026-08-02'),
          (3, 2, '华南', '直营', 80, 10, DATE '2026-08-03')
        """,
        "INSERT INTO analytics_v0.order_items VALUES (1, 1, 'A'), (2, 1, 'B'), (3, 2, 'A')",
    )
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    finally:
        engine.dispose()


def create_sales_fixture_mysql(database_url: str) -> None:
    """把 PostgreSQL 的销售夹具镜像到 MySQL，用于跨引擎端到端比对。

    MySQL 没有 schema 这一层，``analytics_v0`` 就是库名——已发布模型上的
    ``schema_name`` 因此可以原样渲染成 ``analytics_v0.orders``，不需要为 MySQL
    另建一套模型。

    刻意与 PostgreSQL 版本用同样的行：两边跑出不同的数字时，才能确定差异来自
    方言而不是数据。
    """

    engine = create_engine(database_url)
    statements = (
        "DROP TABLE IF EXISTS order_items",
        "DROP TABLE IF EXISTS orders",
        "DROP TABLE IF EXISTS customers",
        "CREATE TABLE customers (id BIGINT PRIMARY KEY, segment VARCHAR(64) NOT NULL) "
        "DEFAULT CHARSET=utf8mb4",
        """
        CREATE TABLE orders (
            id BIGINT PRIMARY KEY,
            customer_id BIGINT NOT NULL,
            region VARCHAR(64) NOT NULL,
            channel VARCHAR(64) NOT NULL,
            net_amount DECIMAL(18,2) NOT NULL,
            refund_amount DECIMAL(18,2) NOT NULL,
            order_date DATE NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        ) DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE order_items (
            id BIGINT PRIMARY KEY,
            order_id BIGINT NOT NULL,
            product VARCHAR(64) NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        ) DEFAULT CHARSET=utf8mb4
        """,
        "INSERT INTO customers VALUES (1, '重点'), (2, '普通')",
        """
        INSERT INTO orders VALUES
          (1, 1, '华东', '直营', 100, 5, '2026-08-01'),
          (2, 2, '华东', '电商', 200, 0, '2026-08-02'),
          (3, 2, '华南', '直营', 80, 10, '2026-08-03')
        """,
        "INSERT INTO order_items VALUES (1, 1, 'A'), (2, 1, 'B'), (3, 2, 'A')",
    )
    try:
        with engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    finally:
        engine.dispose()
