"""建模链路打真 MySQL。

问数走通了不等于能用：用户得先能扫表、看画像、跑发布前质量报告。这几步是手写
SQL，绕过了翻译器，所以单独验。

需要 ``KNOWFLOW_ANALYTICS_TEST_MYSQL_URL``（会建表，指向一次性测试库）。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect

from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.modeling.introspector import SchemaIntrospector
from knowflow_analytics.modeling.profile import ColumnStatisticsProfiler
from knowflow_analytics.modeling.type_system import (
    is_numeric_type,
    is_temporal_type,
    is_text_type,
)

_TABLE = "modeling_probe"


def _engine():
    url = os.getenv("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL")
    if not url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL 未配置")
    return create_engine(url)


@pytest.fixture(scope="module")
def mysql():
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE}")
        connection.exec_driver_sql(
            f"""
            CREATE TABLE {_TABLE} (
                id BIGINT PRIMARY KEY,
                region VARCHAR(64),
                channel ENUM('直营','电商') NOT NULL,
                net_amount DOUBLE NOT NULL,
                qty TINYINT NOT NULL,
                note LONGTEXT,
                ordered_at DATETIME NOT NULL,
                order_date DATE NOT NULL
            ) DEFAULT CHARSET=utf8mb4
            """
        )
        for row in (
            "(1,'华东','直营',100.5,2,'a','2026-08-01 10:00:00','2026-08-01')",
            "(2,'华东','电商',200.25,3,NULL,'2026-08-02 11:00:00','2026-08-02')",
            "(3,'华南','直营',80.75,1,'c','2026-08-03 12:00:00','2026-08-03')",
            "(4,NULL,'直营',30.0,1,NULL,'2026-08-05 09:00:00','2026-08-05')",
        ):
            connection.exec_driver_sql(f"INSERT INTO {_TABLE} VALUES {row}")
    yield engine
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TABLE IF EXISTS {_TABLE}")
    engine.dispose()


@pytest.fixture(scope="module")
def snapshot(mysql):
    """用真内省器产出的表快照。

    手工拼快照会绕开"内省本身在 MySQL 上对不对"这个问题——而它正是链路的第一步。
    """

    return SchemaIntrospector(mysql, dialect=SqlDialect.MYSQL).describe_table(
        schema_name=inspect(mysql).default_schema_name, table_name=_TABLE
    )


@pytest.mark.mysql
def test_connection_test_answers_on_mysql(mysql):
    """``current_database()`` / ``current_setting()`` 是 PostgreSQL 专有的。

    MySQL 上直接以 1305 报"函数不存在"——漏了这一处，MySQL 数据源连"连上了吗"
    都答不出来。表结构读取走通用 inspect()，只有这一条是自己写的 SQL。
    """

    info = SchemaIntrospector(mysql, dialect=SqlDialect.MYSQL).connection_test()

    assert info["database"]
    assert info["server_version"].startswith("8.")


@pytest.mark.mysql
def test_introspection_reads_the_mysql_table(mysql):
    """内省走的是 SQLAlchemy 的通用 inspect()，本来就跨引擎。

    单独验一条，是因为"本来就跨引擎"这个判断如果错了，后面全塌。
    """

    introspector = SchemaIntrospector(mysql, dialect=SqlDialect.MYSQL)

    table = introspector.describe_table(
        schema_name=inspect(mysql).default_schema_name, table_name=_TABLE
    )

    assert {column.name for column in table.columns} >= {
        "id",
        "region",
        "net_amount",
        "ordered_at",
    }


@pytest.mark.mysql
def test_mysql_columns_get_the_right_kinds(mysql):
    """MySQL 特有的类型名要能被认出来。

    改造前这张表里 DOUBLE（金额）、TINYINT（数量）、DATETIME（下单时刻）、
    ENUM/LONGTEXT 全部判不出来——不报错，只是这些列悄悄没进模型。
    """

    columns = {item["name"]: str(item["type"]) for item in inspect(mysql).get_columns(_TABLE)}

    assert is_numeric_type(columns["net_amount"])  # DOUBLE
    assert is_numeric_type(columns["qty"])  # TINYINT
    assert is_temporal_type(columns["ordered_at"])  # DATETIME
    assert is_temporal_type(columns["order_date"])  # DATE
    assert is_text_type(columns["channel"])  # ENUM
    assert is_text_type(columns["note"])  # LONGTEXT


@pytest.mark.mysql
def test_column_profile_runs_on_mysql(mysql, snapshot):
    """列画像整条 SQL 里全是 ``::bigint``，MySQL 上是语法错。

    画像跑不出来，S3 的护栏就只能退回列名规则——建模质量直接下一个台阶，
    而且没有任何报错提示。
    """

    profile = ColumnStatisticsProfiler(mysql, dialect=SqlDialect.MYSQL).profile_table(snapshot)

    assert profile.row_count == 4
    by_name = {item.column: item for item in profile.columns}
    # region 有一行为 NULL：非空计数必须看得出来。
    # region 有一行为 NULL：4 行里只有 3 行非空。
    assert by_name["region"].non_null_count == 3
    assert by_name["channel"].distinct_count == 2


@pytest.mark.mysql
def test_column_profile_reports_ranges_for_numeric_and_temporal_columns(mysql, snapshot):
    """最小/最大值走 ``::text``，MySQL 上同样是语法错。

    没有值域，画像就阻止不了把 ``status_code`` 这类列判成 SUM 度量。
    """

    profile = ColumnStatisticsProfiler(mysql, dialect=SqlDialect.MYSQL).profile_table(snapshot)
    by_name = {item.column: item for item in profile.columns}

    assert by_name["net_amount"].min_value is not None
    assert by_name["order_date"].max_value is not None


@pytest.mark.mysql
def test_wrong_dialect_yields_an_empty_profile_that_says_why(mysql, snapshot):
    """方言传错时画像会**吞掉异常返回空 profile**——这是既有设计（画像是证据不是
    门禁），不是这次改出来的。

    钉住它是因为空画像和"这张表没什么可说的"长得一样。区别只在 ``error`` 字段：
    有值就说明是失败而不是没数据。任何读画像的地方都该看这个字段。
    """

    profile = ColumnStatisticsProfiler(mysql, dialect=SqlDialect.POSTGRES).profile_table(snapshot)

    assert profile.row_count == 0
    assert profile.error


@pytest.mark.mysql
def test_large_table_sampling_has_a_mysql_form(mysql, snapshot):
    """``TABLESAMPLE SYSTEM`` 是 PostgreSQL 专有的。

    不处理的话，超过采样上限的 MySQL 表整条画像语句失败，又被吞掉——表现为
    "这张大表没什么统计量"。这里把采样上限压到 1 行来逼出那条路径。
    """

    profiler = ColumnStatisticsProfiler(mysql, sample_rows=1, dialect=SqlDialect.MYSQL)

    profile = profiler.profile_table(snapshot)

    assert profile.error is None
    assert profile.truncated is True
