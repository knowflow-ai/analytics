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
import sqlglot
from sqlalchemy import create_engine, text

from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.execution.dialect import (
    SqlDialect,
    count_where_sql,
    render_physical_sql,
)

_TABLE = "dialect_parity_orders"

_ROWS = (
    (1, "华东", Decimal("100.00"), date(2026, 8, 1)),
    (2, "华东", Decimal("200.00"), date(2026, 8, 2)),
    (3, "华南", Decimal("80.00"), date(2026, 8, 3)),
    (4, "华东", Decimal("200.00"), date(2025, 8, 1)),
    # 跨周边界：用 DAYOFWEEK() 而不是 WEEKDAY() 会让这一行整体差一天。
    (5, "华南", Decimal("50.00"), date(2026, 8, 9)),
    # NULL 维度：两个引擎对 NULL 的默认排序位置相反，没有这一行就测不出补偿是否正确。
    (6, None, Decimal("30.00"), date(2026, 8, 5)),
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
                f"CREATE TABLE {_TABLE} (id BIGINT NOT NULL, region TEXT, "
                "net_amount NUMERIC NOT NULL, order_date DATE NOT NULL)"
            )
        )
        for row in _ROWS:
            connection.execute(
                text(f"INSERT INTO {_TABLE} VALUES (:id, :region, :amount, :order_date)"),
                {"id": row[0], "region": row[1], "amount": row[2], "order_date": row[3]},
            )
    with my.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS {_TABLE}"))
        connection.execute(
            text(
                f"CREATE TABLE {_TABLE} (id BIGINT NOT NULL, region VARCHAR(64), "
                "net_amount DECIMAL(18,2) NOT NULL, order_date DATE NOT NULL) "
                "DEFAULT CHARSET=utf8mb4"
            )
        )
        for row in _ROWS:
            connection.execute(
                text(f"INSERT INTO {_TABLE} VALUES (:id, :region, :amount, :order_date)"),
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

    这里刻意排除 NULL 分组：这条裸 SQL 不经过 ``render_physical_sql``，没有 NULL
    排序补偿，留着 NULL 会让两边的行序不同，把精度问题淹掉。NULL 排序本身由
    ``test_rendered_physical_sql_agrees_across_engines`` 覆盖。
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
                    f"/ {denominator} AS p FROM {_TABLE} WHERE region IS NOT NULL "
                    "GROUP BY region ORDER BY region"
                )
            )
        ]
    with my.connect() as connection:
        actual = [
            float(row[0])
            for row in connection.execute(
                text(
                    f"SELECT {SqlDialect.MYSQL.ratio_numerator_sql(numerator)} "
                    f"/ {denominator} AS p FROM {_TABLE} WHERE region IS NOT NULL "
                    "GROUP BY region ORDER BY region"
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


# 翻译器实际会产出的形状。每一条都要在两个真库上跑出**逐行相同**的结果——
# 这是 render_physical_sql 能不能用的唯一判据。
_PARITY_CASES = {
    "按日聚合": (
        'SELECT DATE_TRUNC(\'day\', "order_date") AS "t", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY DATE_TRUNC(\'day\', "order_date") ORDER BY "t" ASC LIMIT 101'
    ),
    "按周聚合": (
        'SELECT DATE_TRUNC(\'week\', "order_date") AS "t", COUNT(*) AS "n" '
        'FROM {tbl} GROUP BY DATE_TRUNC(\'week\', "order_date") ORDER BY "t" ASC LIMIT 101'
    ),
    "按月聚合": (
        'SELECT DATE_TRUNC(\'month\', "order_date") AS "t", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY DATE_TRUNC(\'month\', "order_date") ORDER BY "t" ASC LIMIT 101'
    ),
    "按季聚合": (
        'SELECT DATE_TRUNC(\'quarter\', "order_date") AS "t", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY DATE_TRUNC(\'quarter\', "order_date") ORDER BY "t" ASC LIMIT 101'
    ),
    "按年聚合": (
        'SELECT DATE_TRUNC(\'year\', "order_date") AS "t", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY DATE_TRUNC(\'year\', "order_date") ORDER BY "t" ASC LIMIT 101'
    ),
    "NULL维度升序": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY "region" ORDER BY "g" ASC LIMIT 101'
    ),
    "NULL维度降序": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY "region" ORDER BY "g" DESC LIMIT 101'
    ),
    "NULL显式FIRST": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY "region" ORDER BY "g" ASC NULLS FIRST LIMIT 101'
    ),
    "多列混合升降序": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY "region" ORDER BY "v" DESC, "g" ASC LIMIT 101'
    ),
    "带参数过滤": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" FROM {tbl} '
        'WHERE "net_amount" >= :p0 GROUP BY "region" ORDER BY "g" ASC LIMIT 101'
    ),
    "同比自连接CTE": (
        'WITH "b" AS (SELECT DATE_TRUNC(\'year\', "order_date") AS "t", '
        'SUM("net_amount") AS "v" FROM {tbl} '
        "GROUP BY DATE_TRUNC('year', \"order_date\")) "
        'SELECT "c"."t", "c"."v", ("c"."v" - "p"."v") / NULLIF("p"."v", 0) AS "r" '
        'FROM "b" AS "c" LEFT JOIN "b" AS "p" ON "p"."t" = "c"."t" - INTERVAL \'1 year\' '
        'ORDER BY "c"."t" ASC'
    ),
    "排序取TopN": (
        'SELECT "region" AS "g", SUM("net_amount") AS "v" '
        'FROM {tbl} GROUP BY "region" ORDER BY "v" DESC LIMIT 2'
    ),
}


def _normalize(rows):
    out = []
    for row in rows:
        out.append(
            tuple(
                value.date()
                if isinstance(value, datetime)
                else float(value)
                if isinstance(value, Decimal)
                else value
                for value in row
            )
        )
    return out


def _finish(sql: str) -> str:
    # 生产代码在渲染后做的参数占位符归一。
    import re

    return re.sub(r"%\((p\d+)\)s", r":\1", sql)


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize("name", list(_PARITY_CASES))
def test_rendered_physical_sql_agrees_across_engines(engines, name: str):
    """同一棵 AST，两个引擎跑出逐行相同的结果。

    覆盖五种时间粒度、NULL 双向排序、显式 NULLS FIRST、多列混合升降序、参数化过滤、
    同比自连接 CTE、以及排序取 TopN。少测哪一类，哪一类就会在用户那里静默出错。
    """

    pg, my = engines
    template = _PARITY_CASES[name]
    pg_sql = _finish(
        render_physical_sql(
            sqlglot.parse_one(template.format(tbl=f'"{_TABLE}"'), read="postgres"),
            SqlDialect.POSTGRES,
        )
    )
    my_sql = _finish(
        render_physical_sql(
            sqlglot.parse_one(template.format(tbl=f'"{_TABLE}"'), read="postgres"),
            SqlDialect.MYSQL,
        )
    )
    parameters = {"p0": 100} if ":p0" in template else {}

    with pg.connect() as connection:
        expected = _normalize(connection.execute(text(pg_sql), parameters).fetchall())
    with my.connect() as connection:
        actual = _normalize(connection.execute(text(my_sql), parameters).fetchall())

    assert actual == expected


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize("grain", list(TimeGranularity))
def test_grouped_time_query_runs_under_only_full_group_by(engines, grain: TimeGranularity):
    """按粒度分组 + 按该列排序，必须能在 ONLY_FULL_GROUP_BY 下跑起来。

    单独立一条是因为这个失败模式很特别：SQL 语法完全合法，只有开着
    ONLY_FULL_GROUP_BY（MySQL 8 的默认）的库才会以 1055 拒绝。用默认关掉该模式的
    环境测会全绿，上线才发现按月/周/季/年聚合全部打不开。
    """

    _, my = engines
    with my.connect() as connection:
        assert "ONLY_FULL_GROUP_BY" in connection.execute(text("SELECT @@sql_mode")).scalar_one()

    template = (
        f'SELECT DATE_TRUNC(\'{grain.value}\', "order_date") AS "t", '
        f'SUM("net_amount") AS "v" FROM "{_TABLE}" '
        f'GROUP BY DATE_TRUNC(\'{grain.value}\', "order_date") ORDER BY "t" ASC'
    )
    my_sql = render_physical_sql(sqlglot.parse_one(template, read="postgres"), SqlDialect.MYSQL)

    with my.connect() as connection:
        assert connection.execute(text(my_sql)).fetchall()


# AGG(x) FILTER (WHERE p) 的九种形态。每一条都要在两个真库上给出同一个值——
# 尤其是无匹配行时 SUM 返 NULL 而 COUNT 返 0 这个区别，改写不能把它抹平。
_FILTER_CASES = {
    "SUM": 'SUM("net_amount") FILTER (WHERE "region" = \'华东\')',
    "SUM无匹配": 'SUM("net_amount") FILTER (WHERE "region" = \'不存在\')',
    "COUNT星": "COUNT(*) FILTER (WHERE \"region\" = '华东')",
    "COUNT星无匹配": "COUNT(*) FILTER (WHERE \"region\" = '不存在')",
    "COUNT列": 'COUNT("region") FILTER (WHERE "net_amount" > 50)',
    "COUNT去重": 'COUNT(DISTINCT "region") FILTER (WHERE "net_amount" > 50)',
    "MIN": 'MIN("net_amount") FILTER (WHERE "region" = \'华东\')',
    "MAX": 'MAX("net_amount") FILTER (WHERE "region" = \'华东\')',
    "子集占比": (
        'CAST(SUM("net_amount") FILTER (WHERE "region" = \'华东\') AS DOUBLE PRECISION) '
        '/ NULLIF(SUM("net_amount"), 0)'
    ),
}


@pytest.mark.mysql
@pytest.mark.postgres
@pytest.mark.parametrize("name", list(_FILTER_CASES))
def test_aggregate_filter_rewrite_agrees_across_engines(engines, name: str):
    """改写后的 CASE 写法与 PostgreSQL 的 FILTER 给出同一个值。

    ``SUM无匹配`` 与 ``COUNT星无匹配`` 是这里最重要的两条：前者必须是 NULL、后者必须
    是 0。改写时给 CASE 加个 ``ELSE 0`` 就会把 NULL 变成 0——"没有数据"被悄悄换成
    "金额为零"，是典型的静默错答。
    """

    pg, my = engines
    template = f'SELECT {_FILTER_CASES[name]} AS "x" FROM "{_TABLE}" LIMIT 1'
    pg_sql = render_physical_sql(sqlglot.parse_one(template, read="postgres"), SqlDialect.POSTGRES)
    my_sql = render_physical_sql(sqlglot.parse_one(template, read="postgres"), SqlDialect.MYSQL)

    assert "FILTER" in pg_sql
    assert "FILTER" not in my_sql

    with pg.connect() as connection:
        expected = connection.execute(text(pg_sql)).scalar_one()
    with my.connect() as connection:
        actual = connection.execute(text(my_sql)).scalar_one()

    if expected is None:
        assert actual is None
    else:
        assert actual is not None
        assert float(actual) == pytest.approx(float(expected), rel=1e-9)


@pytest.mark.mysql
def test_filter_really_is_a_syntax_error_on_mysql(engines):
    """把「为什么必须改写」钉住。

    如果哪天 MySQL 支持了 FILTER，这条会红——那时才可以考虑去掉改写。在那之前，
    任何"交给 sqlglot 就行"的说法都是错的。
    """

    from sqlalchemy.exc import SQLAlchemyError

    _, my = engines
    with my.connect() as connection, pytest.raises(SQLAlchemyError):
        connection.execute(text(f"SELECT COUNT(*) FILTER (WHERE `region` IS NULL) FROM `{_TABLE}`"))
