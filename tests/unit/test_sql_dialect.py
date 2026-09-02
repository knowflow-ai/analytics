"""方言层的合同。

这些断言全部来自对真 PostgreSQL 与真 MySQL 8.0 的比对实验，不是推理产物。
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.execution.dialect import (
    SqlDialect,
    count_where_sql,
    render_physical_sql,
)


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


class TestRenderPhysicalSql:
    """物理 SQL 的唯一收口。"""

    @staticmethod
    def _tree(sql: str) -> exp.Expression:
        return sqlglot.parse_one(sql, read="postgres")

    def test_postgres_output_is_byte_identical_to_plain_rendering(self):
        """PostgreSQL 路径必须一个字节都不变。

        这是整条链路唯一没有回归余地的地方：现存的黄金集、确认记忆、诊断产物
        全都绑在当前产出的 SQL 上。
        """

        tree = self._tree(
            'SELECT DATE_TRUNC(\'month\', "d") AS "t", SUM("v") AS "s" '
            'FROM "o" GROUP BY DATE_TRUNC(\'month\', "d") ORDER BY "t" ASC LIMIT 101'
        )

        assert render_physical_sql(tree, SqlDialect.POSTGRES) == tree.sql(dialect="postgres")

    def test_mysql_replaces_date_trunc_with_the_typed_rendering(self):
        tree = self._tree('SELECT DATE_TRUNC(\'month\', "d") AS "t" FROM "o"')

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "MAKEDATE" in rendered
        # sqlglot 自己的渲染，返回字符串而不是日期。
        assert "TIMESTAMPDIFF" not in rendered

    @pytest.mark.parametrize("grain", list(TimeGranularity))
    def test_mysql_handles_every_governed_grain(self, grain: TimeGranularity):
        tree = self._tree(f'SELECT DATE_TRUNC(\'{grain.value}\', "d") AS "t" FROM "o"')

        assert render_physical_sql(tree, SqlDialect.MYSQL)

    def test_ungoverned_grain_is_rejected_rather_than_silently_wrong(self):
        """按小时截断在 PostgreSQL 上能跑，MySQL 上没有对应写法。

        静默出一列错的时间比报错糟得多——问数的底线是 0 静默错答。
        """

        tree = self._tree('SELECT DATE_TRUNC(\'hour\', "d") AS "t" FROM "o"')

        with pytest.raises(TranslationError) as excinfo:
            render_physical_sql(tree, SqlDialect.MYSQL)

        assert excinfo.value.code == "UNSUPPORTED_TIME_GRANULARITY"

    def test_mysql_order_by_keeps_the_select_alias(self):
        """ORDER BY 必须保住别名，不能展开成表达式。

        sqlglot 在补偿 NULL 排序时会把别名解析成背后的完整表达式；按时间粒度分组时
        那是 ``CAST(MAKEDATE(...))``，MySQL 的 ONLY_FULL_GROUP_BY 认不出它就是 GROUP
        BY 的那个表达式，以 1055 拒绝——实测按月/周/季/年聚合全部跑不起来。
        """

        tree = self._tree(
            'SELECT DATE_TRUNC(\'month\', "d") AS "t", SUM("v") AS "s" FROM "o" '
            'GROUP BY DATE_TRUNC(\'month\', "d") ORDER BY "t" ASC'
        )

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "ORDER BY CASE WHEN `t` IS NULL" in rendered
        assert "CASE WHEN CAST(MAKEDATE" not in rendered

    def test_mysql_ascending_puts_nulls_last_like_postgres(self):
        # 两边默认相反（实测 ASC：PostgreSQL NULL 在后，MySQL 在前），必须补偿。
        tree = self._tree('SELECT "g" FROM "o" ORDER BY "g" ASC')

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "CASE WHEN `g` IS NULL THEN 1 ELSE 0 END" in rendered

    def test_mysql_descending_puts_nulls_first_like_postgres(self):
        tree = self._tree('SELECT "g" FROM "o" ORDER BY "g" DESC')

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "CASE WHEN `g` IS NULL THEN 0 ELSE 1 END" in rendered

    def test_mysql_skips_compensation_when_it_matches_the_native_default(self):
        # ASC + NULLS FIRST 正是 MySQL 的原生行为，不该再加 CASE。
        tree = self._tree('SELECT "g" FROM "o" ORDER BY "g" ASC NULLS FIRST')

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "CASE WHEN" not in rendered

    def test_mysql_compensates_each_term_of_a_multi_column_order(self):
        tree = self._tree('SELECT "a", "b" FROM "o" ORDER BY "a" ASC, "b" DESC')

        rendered = render_physical_sql(tree, SqlDialect.MYSQL)

        assert "CASE WHEN `a` IS NULL THEN 1 ELSE 0 END" in rendered
        assert "CASE WHEN `b` IS NULL THEN 0 ELSE 1 END" in rendered

    def test_rendering_does_not_mutate_the_source_tree(self):
        """渲染不能改坏入参。

        同一棵树在诊断产物里还要按 PostgreSQL 再渲染一次；就地改会让诊断显示的 SQL
        和真正执行的对不上。
        """

        tree = self._tree('SELECT DATE_TRUNC(\'month\', "d") AS "t" FROM "o" ORDER BY "t" ASC')
        before = tree.sql(dialect="postgres")

        render_physical_sql(tree, SqlDialect.MYSQL)

        assert tree.sql(dialect="postgres") == before

    def test_parameter_placeholders_survive_both_dialects(self):
        """参数占位符经归一后两边都是 ``:pN``。

        sqlglot 给 PostgreSQL 渲染成 ``%(p0)s``、给 MySQL 渲染成 ``:p0``；生产代码在
        渲染后统一用同一条正则归一，这里确认那条正则对两边都成立。
        """

        import re

        tree = self._tree('SELECT "a" FROM "o" WHERE "a" >= :p0')

        for dialect in SqlDialect:
            normalized = re.sub(r"%\((p\d+)\)s", r":\1", render_physical_sql(tree, dialect))
            assert ":p0" in normalized
            assert "%(p0)s" not in normalized


class TestExplain:
    def test_postgres_uses_the_parenthesised_form(self):
        assert SqlDialect.POSTGRES.explain_sql("SELECT 1") == "EXPLAIN (FORMAT JSON) SELECT 1"

    def test_mysql_uses_the_equals_form(self):
        """MySQL 不认 PostgreSQL 的括号写法。

        实测 ``EXPLAIN (FORMAT JSON) SELECT 1`` 在 MySQL 8 上直接报 1064 语法错。
        """

        assert SqlDialect.MYSQL.explain_sql("SELECT 1") == "EXPLAIN FORMAT=JSON SELECT 1"

    @pytest.mark.parametrize("dialect", list(SqlDialect))
    def test_every_dialect_can_explain(self, dialect: SqlDialect):
        assert "SELECT 1" in dialect.explain_sql("SELECT 1")


class TestAggregateFilterRewrite:
    """``AGG(x) FILTER (WHERE p)`` 在 MySQL 上是语法错，必须改写。

    这条是最要命的一处：子集占比 ``RATIO_TO_TOTAL(指标, 维度, 值)`` 生成的物理 SQL
    里就带着 FILTER，而 sqlglot 和 SQLAlchemy 都原样吐给 MySQL——占比类问题在
    MySQL 数据源上会直接语法错。
    """

    @staticmethod
    def _mysql(sql: str) -> str:
        return render_physical_sql(sqlglot.parse_one(sql, read="postgres"), SqlDialect.MYSQL)

    @staticmethod
    def _postgres(sql: str) -> str:
        return render_physical_sql(sqlglot.parse_one(sql, read="postgres"), SqlDialect.POSTGRES)

    def test_postgres_keeps_filter_untouched(self):
        # PostgreSQL 原生支持，重写它只会平添一处与既有产物的差异。
        sql = 'SELECT SUM("v") FILTER (WHERE "g" = \'x\') FROM "o"'

        assert "FILTER" in self._postgres(sql)

    def test_sum_pushes_the_condition_into_the_argument(self):
        rendered = self._mysql('SELECT SUM("v") FILTER (WHERE "g" = \'x\') FROM "o"')

        assert "FILTER" not in rendered
        assert "SUM(CASE WHEN `g` = 'x' THEN `v` END)" in rendered

    def test_count_star_counts_ones_because_star_cannot_go_in_a_case(self):
        rendered = self._mysql('SELECT COUNT(*) FILTER (WHERE "g" IS NULL) FROM "o"')

        assert "COUNT(CASE WHEN `g` IS NULL THEN 1 END)" in rendered

    def test_count_of_a_column_keeps_that_column(self):
        rendered = self._mysql('SELECT COUNT("g") FILTER (WHERE "v" > 1) FROM "o"')

        assert "COUNT(CASE WHEN `v` > 1 THEN `g` END)" in rendered

    def test_count_distinct_pushes_inside_the_distinct(self):
        """CASE 要推到 DISTINCT 里面。

        推到外面（``DISTINCT CASE`` 变成 ``CASE ... DISTINCT``）会得到不同的去重口径。
        """

        rendered = self._mysql('SELECT COUNT(DISTINCT "g") FILTER (WHERE "v" > 1) FROM "o"')

        assert "COUNT(DISTINCT CASE WHEN `v` > 1 THEN `g` END)" in rendered

    @pytest.mark.parametrize("aggregate", ["AVG", "MIN", "MAX"])
    def test_other_aggregates_are_rewritten_too(self, aggregate: str):
        rendered = self._mysql(f'SELECT {aggregate}("v") FILTER (WHERE "g" = \'x\') FROM "o"')

        assert "FILTER" not in rendered
        assert f"{aggregate}(CASE WHEN `g` = 'x' THEN `v` END)" in rendered

    def test_no_case_default_so_sum_still_returns_null_when_nothing_matches(self):
        """``CASE`` 不能带 ``ELSE 0``。

        带了的话 ``SUM`` 在无匹配行时会从 NULL 变成 0——"没有数据"被悄悄换成
        "金额为零"。实测 PostgreSQL 上 FILTER 与不带 ELSE 的 CASE 两者都返回 NULL。
        """

        rendered = self._mysql('SELECT SUM("v") FILTER (WHERE "g" = \'x\') FROM "o"')

        assert "ELSE" not in rendered

    def test_subset_share_expression_is_fully_rewritten(self):
        # RATIO_TO_TOTAL 子集占比的完整形状。
        rendered = self._mysql(
            'SELECT CAST(SUM("v") FILTER (WHERE "g" = \'x\') AS DOUBLE PRECISION) '
            '/ NULLIF(SUM("v"), 0) AS "p" FROM "o"'
        )

        assert "FILTER" not in rendered
        assert "NULLIF(SUM(`v`), 0)" in rendered

    def test_rewritten_sql_parses_as_mysql(self):
        rendered = self._mysql('SELECT SUM("v") FILTER (WHERE "g" = \'x\') FROM "o"')

        sqlglot.parse_one(rendered, read="mysql")
