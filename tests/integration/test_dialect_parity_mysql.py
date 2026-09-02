"""同一份数据，PostgreSQL 与 MySQL 必须给出同样的答案。

单元测试只能证明生成的字符串长什么样；能不能被数据库接受、跑出来的行和类型对不对，
只有真库能回答。方言层的三条例外全部来自这里的比对，所以这些断言是它们的依据本身。

需要两个库同时可用：

    KNOWFLOW_ANALYTICS_TEST_DATABASE_URL=postgresql+psycopg://...
    KNOWFLOW_ANALYTICS_TEST_MYSQL_URL=mysql+pymysql://...?charset=utf8mb4
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.execution.dialect import SqlDialect, count_where_sql

_TABLE = "dialect_parity_orders"

_ROWS = (
    (1, "华东", Decimal("100.00"), date(2026, 8, 1)),
    (2, "华东", Decimal("200.00"), date(2026, 8, 2)),
    (3, "华南", Decimal("80.00"), date(2026, 8, 3)),
    (4, "华东", Decimal("200.00"), date(2025, 8, 1)),
    # 跨周边界：用 DAYOFWEEK() 而不是 WEEKDAY() 会让这一行整体差一天。
    (5, "华南", Decimal("50.00"), date(2026, 8, 9)),
)


def _engines():
    pg_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL")
    my_url = os.getenv("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL")
    if not pg_url or not my_url:
        pytest.skip("需要同时配置 PostgreSQL 与 MySQL 测试库才能做跨引擎比对")
    return create_engine(pg_url), create_engine(my_url)


def _seed(pg, my):
    with pg.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        connection.execute(
            text(
                f"CREATE TABLE {_TABLE} (id BIGINT NOT NULL, region TEXT NOT NULL, "
                "net_amount NUMERIC NOT NULL, order_date DATE NOT NULL)"
            )
        )
        for row in _ROWS:
            connection.execute(
                text(
                    f"INSERT INTO {_TABLE} VALUES (:id, :region, :amount, :order_date)"
                ),
                {"id": row[0], "region": row[1], "amount": row[2], "order_date": row[3]},
            )
    with my.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        connection.execute(
            text(
                f"CREATE TABLE {_TABLE} (id BIGINT NOT NULL, region VARCHAR(64) NOT NULL, "
                "net_amount DECIMAL(18,2) NOT NULL, order_date DATE NOT NULL) "
                "DEFAULT CHARSET=utf8mb4"
            )
        )
        for row in _ROWS:
            connection.execute(
                text(
                    f"INSERT INTO {_TABLE} VALUES (:id, :region, :amount, :order_date)"
                ),
                {"id": row[0], "region": row[1], "amount": row[2], "order_date": row[3]},
            )


def _as_date(value):
    return value.date() if isinstance(value, datetime) else value


@pytest.fixture(scope="module")
def engines():
    pg, my = _engines()
    _seed(pg, my)
    yield pg, my
    for engine in (pg, my):
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        engine.dispose()


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize("grain", list(TimeGranularity))
def test_time_grain_matches_postgres(engines, grain: TimeGranularity):
    """五种粒度截出来的日期必须逐行相同。"""

    pg, my = engines
    pg_sql = SqlDialect.POSTGRES.date_trunc_sql("order_date", grain)
    my_sql = SqlDialect.MYSQL.date_trunc_sql("order_date", grain)

    # 带参数执行：pymysql 只在有参数时才对 SQL 做 % 插值，
    # 不带参数跑会漏掉「生成的 SQL 不能含 %」这条约束。
    with pg.connect() as connection:
        expected = [
            _as_date(row[0])
            for row in connection.execute(
                text(f"SELECT {pg_sql} AS t FROM {_TABLE} WHERE id >= :lo ORDER BY id"),
                {"lo": 1},
            )
        ]
    with my.connect() as connection:
        actual = [
            _as_date(row[0])
            for row in connection.execute(
                text(f"SELECT {my_sql} AS t FROM {_TABLE} WHERE id >= :lo ORDER BY id"),
                {"lo": 1},
            )
        ]

    assert actual == expected


@pytest.mark.mysql
@pytest.mark.parametrize("grain", list(TimeGranularity))
def test_mysql_time_grain_keeps_the_date_type(engines, grain: TimeGranularity):
    """值对了还不够，类型也得对。

    交给 sqlglot 转译会得到字符串，下游按粒度展示、按时间排序、同比自连接全都依赖
    真实日期类型——而字符串排序在跨年时才出错，测不出来就会长期错着。
    """

    _, my = engines
    my_sql = SqlDialect.MYSQL.date_trunc_sql("order_date", grain)

    with my.connect() as connection:
        value = connection.execute(
            text(f"SELECT {my_sql} AS t FROM {_TABLE} WHERE id = :id"), {"id": 1}
        ).scalar_one()

    assert isinstance(value, date)


@pytest.mark.mysql
@pytest.mark.postgres
def test_ratio_precision_matches_postgres(engines):
    """占比要算到底。

    不转 DOUBLE 的话 MySQL 的 DECIMAL 除法只留 6 位小数（0.862069），
    PostgreSQL 给的是 0.8620689655172413。
    """

    pg, my = engines
    numerator = "SUM(net_amount)"
    denominator = "NULLIF(SUM(SUM(net_amount)) OVER (), 0)"

    with pg.connect() as connection:
        expected = [
            float(row[0])
            for row in connection.execute(
                text(
                    f"SELECT {SqlDialect.POSTGRES.ratio_numerator_sql(numerator)} "
                    f"/ {denominator} AS p FROM {_TABLE} GROUP BY region ORDER BY region"
                )
            )
        ]
    with my.connect() as connection:
        actual = [
            float(row[0])
            for row in connection.execute(
                text(
                    f"SELECT {SqlDialect.MYSQL.ratio_numerator_sql(numerator)} "
                    f"/ {denominator} AS p FROM {_TABLE} GROUP BY region ORDER BY region"
                )
            )
        ]

    assert actual == pytest.approx(expected, rel=1e-12)


@pytest.mark.mysql
@pytest.mark.postgres
def test_count_where_matches_postgres(engines):
    """FILTER 的替代写法两边算出同一个数。"""

    pg, my = engines
    sql = f"SELECT {count_where_sql('region = :region')} AS n FROM {_TABLE}"

    with pg.connect() as connection:
        expected = connection.execute(text(sql), {"region": "华东"}).scalar_one()
    with my.connect() as connection:
        actual = connection.execute(text(sql), {"region": "华东"}).scalar_one()

    assert int(actual) == int(expected) == 3


@pytest.mark.mysql
def test_read_only_session_actually_blocks_writes(engines):
    """只读事务是安全边界，得证明它真的拦得住写。

    MySQL 的只读语法与 PostgreSQL 完全不同，抄错了不会报错，只会静默地不生效。
    这里刻意复刻执行器的形状：``begin()`` 之后立刻发只读语句，再执行业务 SQL。
    SQLAlchemy 的 ``begin()`` 是惰性的，所以只读语句先于真正的事务落地。
    """

    from sqlalchemy.exc import SQLAlchemyError

    _, my = engines
    with my.connect() as connection, connection.begin():
        for statement in SqlDialect.MYSQL.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=2_000
        ):
            connection.exec_driver_sql(statement)
        with pytest.raises(SQLAlchemyError):
            connection.execute(
                text(f"INSERT INTO {_TABLE} VALUES (:id, 'x', 1, '2026-01-01')"), {"id": 99}
            )


@pytest.mark.mysql
def test_read_only_does_not_leak_to_later_transactions(engines):
    """只读不能粘在连接上。

    连接是池化复用的：加了 ``SESSION`` 的话，下一个拿到这条连接的人（比如 Excel
    导入的写入）会莫名其妙地写不进去。实测那正是
    ``SET SESSION TRANSACTION READ ONLY`` 的行为——它连清理用的 DROP TABLE 都拦下了。
    """

    _, my = engines
    with my.connect() as connection:
        with connection.begin():
            for statement in SqlDialect.MYSQL.read_only_session_sql(
                statement_timeout_ms=30_000, lock_timeout_ms=2_000
            ):
                connection.exec_driver_sql(statement)
            connection.execute(text(f"SELECT COUNT(*) FROM {_TABLE}"))

        # 同一条连接上的下一个事务必须还能写。
        with connection.begin():
            connection.execute(
                text(f"INSERT INTO {_TABLE} VALUES (:id, 'x', 1, '2026-01-01')"), {"id": 99}
            )
            connection.execute(text(f"DELETE FROM {_TABLE} WHERE id = :id"), {"id": 99})
