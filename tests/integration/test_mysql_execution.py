"""同一个受治理语义查询，打到 MySQL 上要和 PostgreSQL 给出同样的答案。

前面几个文件验证的是「渲染出来的 SQL 对不对」；这里走完整条真实链路——翻译器 →
守卫 → 只读事务 → 驱动类型编解码 → QueryResult。少了这一层，方言层再对也只是
字符串正确。

需要两个库同时可用（同 test_dialect_parity_mysql）。
"""

from __future__ import annotations

import os

import pytest

from knowflow_analytics.contracts import (
    QueryFilter,
    QueryOrder,
    SemanticQuery,
    SemanticQueryType,
    SortDirection,
)
from knowflow_analytics.execution import SqlExecutor
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.semantic import SemanticTranslator
from tests.support import create_sales_fixture, create_sales_fixture_mysql


def _urls() -> tuple[str, str]:
    pg_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    my_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL")
    if not pg_url or not my_url:
        pytest.skip("需要同时配置 PostgreSQL 与 MySQL 测试库才能做跨引擎比对")
    return pg_url, my_url


@pytest.fixture(scope="module")
def seeded():
    pg_url, my_url = _urls()
    create_sales_fixture(pg_url)
    create_sales_fixture_mysql(my_url)
    return pg_url, my_url


def _run(url: str, dialect: SqlDialect, release, query: SemanticQuery):
    physical = SemanticTranslator().translate(release=release, query=query, dialect=dialect)
    executor = SqlExecutor(url, dialect=dialect)
    try:
        return physical, executor.execute(query=physical, release=release)
    finally:
        executor.close()


_QUERIES = {
    "指标按维度聚合": SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("region",),
        order_by=(QueryOrder(element_id="net_revenue", direction=SortDirection.DESC),),
        limit=10,
    ),
    "跨模型join": SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("customer_segment",),
        order_by=(QueryOrder(element_id="customer_segment", direction=SortDirection.ASC),),
        limit=10,
    ),
    "带过滤": SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        dimension_ids=("region",),
        filters=(QueryFilter(dimension_id="channel", operator="eq", value="直营"),),
        order_by=(QueryOrder(element_id="region", direction=SortDirection.ASC),),
        limit=10,
    ),
    "无维度总计": SemanticQuery(
        dataset_id="sales_dataset",
        metric_ids=("net_revenue",),
        limit=10,
    ),
}


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize("name", list(_QUERIES))
def test_same_query_gives_the_same_answer_on_both_engines(seeded, sales_release, name: str):
    """整条链路的最终判据：同一个语义查询，两个引擎的结果逐行相同。"""

    pg_url, my_url = seeded
    query = _QUERIES[name]

    _, expected = _run(pg_url, SqlDialect.POSTGRES, sales_release, query)
    _, actual = _run(my_url, SqlDialect.MYSQL, sales_release, query)

    assert actual.columns == expected.columns
    assert [tuple(map(str, row)) for row in actual.rows] == [
        tuple(map(str, row)) for row in expected.rows
    ]


@pytest.mark.mysql
def test_mysql_physical_sql_is_actually_mysql(seeded, sales_release):
    """物理 SQL 必须真的换了方言，不是碰巧 PostgreSQL 写法也能跑。

    ``analytics_v0.orders`` 在两边都合法（MySQL 把 schema 当库名），所以只看能不能
    跑是分辨不出方言有没有生效的——得看引号。
    """

    _, my_url = seeded
    physical = SemanticTranslator().translate(
        release=sales_release, query=_QUERIES["指标按维度聚合"], dialect=SqlDialect.MYSQL
    )

    assert "`" in physical.sql
    assert '"' not in physical.sql


@pytest.mark.mysql
def test_postgres_physical_sql_is_unchanged_by_the_dialect_work(sales_release):
    """PostgreSQL 侧必须逐字节不变。

    现存的黄金集、诊断产物、契约测试都绑在当前产出的 SQL 上；这条是整个方言改造
    唯一没有回归余地的地方。不需要数据库，纯比字符串。
    """

    physical = SemanticTranslator().translate(
        release=sales_release, query=_QUERIES["指标按维度聚合"]
    )
    explicit = SemanticTranslator().translate(
        release=sales_release,
        query=_QUERIES["指标按维度聚合"],
        dialect=SqlDialect.POSTGRES,
    )

    assert physical.sql == explicit.sql
    assert '"' in physical.sql
    assert "`" not in physical.sql
    # 参数占位符必须还是 :pN；重渲染会把它变成 %(pN)s（实测）。
    assert "%(" not in physical.sql


@pytest.mark.mysql
def test_read_only_transaction_blocks_writes_on_mysql(seeded, sales_release):
    """MySQL 上的只读事务真的拦得住写。

    只读是安全边界，语法两边完全不同，抄错了不会报错、只会静默不生效。
    """

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    _, my_url = seeded
    executor = SqlExecutor(my_url, dialect=SqlDialect.MYSQL)
    try:
        with executor._engine.connect() as connection, connection.begin():
            executor._configure_transaction(connection)
            with pytest.raises(SQLAlchemyError):
                connection.execute(text("INSERT INTO customers VALUES (99, 'x')"))
    finally:
        executor.close()


@pytest.mark.mysql
def test_explain_works_on_mysql(seeded, sales_release):
    """EXPLAIN 的语法两边不同：MySQL 不认 PostgreSQL 的括号写法（实测 1064）。"""

    _, my_url = seeded
    physical = SemanticTranslator().translate(
        release=sales_release, query=_QUERIES["指标按维度聚合"], dialect=SqlDialect.MYSQL
    )
    executor = SqlExecutor(my_url, dialect=SqlDialect.MYSQL)
    try:
        assert executor.explain(query=physical, release=sales_release)
    finally:
        executor.close()


@pytest.mark.mysql
def test_detail_query_round_trips_on_mysql(seeded, sales_release):
    """明细查询也要走通：它不聚合，命中的是另一条翻译分支。"""

    pg_url, my_url = seeded
    query = SemanticQuery(
        dataset_id="sales_dataset",
        query_type=SemanticQueryType.DETAIL,
        dimension_ids=("region", "channel"),
        order_by=(QueryOrder(element_id="region", direction=SortDirection.ASC),),
        limit=10,
    )

    _, expected = _run(pg_url, SqlDialect.POSTGRES, sales_release, query)
    _, actual = _run(my_url, SqlDialect.MYSQL, sales_release, query)

    assert actual.rows == expected.rows
