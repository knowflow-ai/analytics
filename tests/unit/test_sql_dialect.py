"""方言层的合同。

这些断言全部来自对真 PostgreSQL 与真 MySQL 8.0 的比对实验，不是推理产物。
"""

from __future__ import annotations

import pytest
import sqlglot

from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.execution.dialect import SqlDialect, count_where_sql


class TestDateTrunc:
    def test_postgres_keeps_native_date_trunc(self):
        assert (
            SqlDialect.POSTGRES.date_trunc_sql('"d"', TimeGranularity.MONTH)
            == "DATE_TRUNC('month', \"d\")"
        )

    @pytest.mark.parametrize("grain", list(TimeGranularity))
    def test_every_grain_has_a_mysql_rendering(self, grain: TimeGranularity):
        # 漏一个粒度不会在这里报错，会在用户按季度提问时报错。
        rendered = SqlDialect.MYSQL.date_trunc_sql("`d`", grain)

        assert "`d`" in rendered

    @pytest.mark.parametrize("grain", list(TimeGranularity))
    def test_mysql_rendering_contains_no_percent(self, grain: TimeGranularity):
        """生成的 SQL 里不能出现 ``%``。

        pymysql 把 ``%`` 当参数占位符，而执行器是带参数执行的：
        ``DATE_FORMAT(c,'%Y-%m-01')`` 会在驱动层就抛
        ``unsupported format character 'Y'``，SQL 根本到不了数据库。
        所以粒度模板一律用 MAKEDATE/DATE_SUB 这类不含格式串的写法。
        """

        assert "%" not in SqlDialect.MYSQL.date_trunc_sql("`d`", grain)

    @pytest.mark.parametrize("grain", list(TimeGranularity))
    def test_mysql_rendering_casts_back_to_date(self, grain: TimeGranularity):
        """截断结果必须还是日期类型。

        交给 sqlglot 转译会得到 MySQL 的字符串
        （``DATE_ADD(...)`` 返回 ``'2026-08-01 00:00:00'``）；SuperSonic 的
        ``DATE_FORMAT(c,'%Y-%m')`` 同样是字符串。下游按粒度展示、按时间排序、
        同比自连接都依赖真实日期类型。
        """

        assert SqlDialect.MYSQL.date_trunc_sql("`d`", grain).startswith("CAST(")

    def test_mysql_week_starts_on_monday_like_postgres(self):
        # WEEKDAY() 周一为 0。用 DAYOFWEEK()（周日为 1）会整体差一天，
        # 而且只在跨周边界上错——测不出来就会长期错着。
        assert "WEEKDAY(" in SqlDialect.MYSQL.date_trunc_sql("`d`", TimeGranularity.WEEK)

    @pytest.mark.parametrize("grain", list(TimeGranularity))
    def test_mysql_rendering_parses_as_mysql(self, grain: TimeGranularity):
        rendered = SqlDialect.MYSQL.date_trunc_sql("`d`", grain)

        sqlglot.parse_one(f"SELECT {rendered} FROM t", read="mysql")


class TestRatioNumerator:
    def test_postgres_numerator_is_untouched(self):
        assert SqlDialect.POSTGRES.ratio_numerator_sql('SUM("v")') == 'SUM("v")'

    def test_mysql_numerator_casts_to_double(self):
        """MySQL 的 DECIMAL 除法只留 6 位小数。

        实测同一份数据：MySQL 给 ``0.862069``，PostgreSQL 给
        ``0.8620689655172413``；分子转 DOUBLE 后两边逐位一致。
        """

        assert SqlDialect.MYSQL.ratio_numerator_sql("SUM(`v`)") == "CAST(SUM(`v`) AS DOUBLE)"


class TestReadOnlySession:
    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_every_dialect_declares_read_only(self, dialect: SqlDialect):
        # 只读事务是安全边界：漏掉某个方言 = 那个方言上可以写库。
        statements = dialect.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=2_000
        )

        assert any("READ ONLY" in item for item in statements)

    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_every_dialect_bounds_statement_time(self, dialect: SqlDialect):
        statements = dialect.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=2_000
        )

        assert any("30000" in item for item in statements)

    def test_mysql_read_only_is_not_session_scoped(self):
        """MySQL 的只读一定不能加 SESSION。

        加了就会粘在连接上，而连接是池化复用的——下一个拿到这条连接的人（比如
        Excel 导入的写入）会莫名其妙地写不进去。实测 ``SET SESSION TRANSACTION
        READ ONLY`` 污染后续事务；这个错抄了不会报错，只会静默生效。
        """

        statements = SqlDialect.MYSQL.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=2_000
        )

        assert "SET TRANSACTION READ ONLY" in statements
        assert "SET SESSION TRANSACTION READ ONLY" not in statements

    def test_mysql_lock_timeout_rounds_up_to_one_second(self):
        """innodb_lock_wait_timeout 以秒计且最小为 1。

        毫秒配置整除会得到 0，MySQL 拒绝设置，于是**静默退回默认的 50 秒**——
        比配置值大 25 倍，且没有任何报错。向上取整宁可等久一点。
        """

        statements = SqlDialect.MYSQL.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=500
        )

        assert "SET SESSION innodb_lock_wait_timeout = 1" in statements

    def test_mysql_lock_timeout_rounds_up_not_down(self):
        statements = SqlDialect.MYSQL.read_only_session_sql(
            statement_timeout_ms=30_000, lock_timeout_ms=2_500
        )

        assert "SET SESSION innodb_lock_wait_timeout = 3" in statements


class TestCountWhere:
    def test_uses_portable_case_not_filter(self):
        """``COUNT(*) FILTER (WHERE ...)`` 是标准 SQL，MySQL 不支持。

        关键在于 **sqlglot 和 SQLAlchemy 都原样把它吐给 MySQL**（都实测过），
        没有库会替我们翻译，只会在数据库那头报语法错。
        """

        rendered = count_where_sql('"k" IS NULL')

        assert "FILTER" not in rendered.upper()
        assert rendered == 'SUM(CASE WHEN "k" IS NULL THEN 1 ELSE 0 END)'

    @pytest.mark.parametrize("write", ["postgres", "mysql", "duckdb"])
    def test_renders_identically_across_dialects(self, write: str):
        sql = f"SELECT {count_where_sql('k IS NULL')} AS n FROM t"

        transpiled = sqlglot.transpile(sql, read="postgres", write=write)[0]

        assert "CASE WHEN k IS NULL THEN 1 ELSE 0 END" in transpiled

    def test_sqlglot_really_does_leak_filter_to_mysql(self):
        """把这次实验本身钉住。

        如果哪天 sqlglot 学会了翻译 FILTER，这条会红——那时才可以考虑简化
        ``count_where_sql``。在那之前，任何"用库就行了"的说法都是错的。
        """

        leaked = sqlglot.transpile(
            "SELECT COUNT(*) FILTER (WHERE k IS NULL) FROM t", read="postgres", write="mysql"
        )[0]

        assert "FILTER" in leaked.upper()
