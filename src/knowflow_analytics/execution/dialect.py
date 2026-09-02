"""执行引擎的方言差异。

**这里只放 sqlglot 装不下的东西。** sqlglot 是本项目的方言权威（对标 SuperSonic
的 Calcite），实测 10 个 PostgreSQL 专有写法它翻对 9 个：``ILIKE`` → ``LOWER LIKE``、
``~`` → ``REGEXP_LIKE``、``STRING_AGG`` → ``GROUP_CONCAT``、``NULLS LAST`` → ``CASE``、
``::bigint`` → ``CAST AS SIGNED``。真实生成的物理 SQL、同比自连接 CTE、占比窗口在真
MySQL 上跑出来的行与 PostgreSQL 逐行相同。

所以这个模块**不是第二套转译器**，是三处经实测确认的例外。凡是 sqlglot 已经翻对的，
一律不要搬进来——多一条手写规则就多一处会漂移的真相。

三处例外（每条都有实测依据）：

1. **时间截断会丢类型。** sqlglot 把 ``DATE_TRUNC('month', d)`` 渲染成
   ``DATE_ADD('0000-01-01', INTERVAL TIMESTAMPDIFF(...) MONTH)``，MySQL 返回的是
   **字符串** ``'2026-08-01 00:00:00'`` 而不是日期。下游按粒度展示、按时间排序、
   同比自连接都依赖真实日期类型。

   SuperSonic 的 ``MysqlAdaptor.getDateFormat`` 用 ``DATE_FORMAT(c,'%Y-%m')``，
   **同样返回字符串**——照抄上游会把这个坑一起抄进来。这也正是上游 ``DbAdaptor``
   的第一个方法就是 ``getDateFormat`` 的原因：日期截断本来就不能交给通用转译器。

2. **生成的 SQL 里不能出现 ``%``。** pymysql 把 ``%`` 当参数占位符，而执行器是带参数
   执行的，于是 ``DATE_FORMAT(c,'%Y-%m-01')`` 会在驱动层就抛
   ``ValueError: unsupported format character 'Y'``——SQL 根本到不了数据库。因此下面
   五种粒度全部选用 ``MAKEDATE``/``DATE_SUB`` 这类**不含格式串**的写法（实测带参数
   执行通过）。改这些模板时必须守住这条：不要引入 ``%``。

3. **占比会被 DECIMAL 除法截断。** MySQL 的 ``DECIMAL/DECIMAL`` 只留 6 位小数
   （``0.862069``），PostgreSQL 给的是 ``0.8620689655172413``。分子转 ``DOUBLE`` 后
   两边逐位一致。

五种粒度的 MySQL 渲染都与 PostgreSQL 在真库上逐行比对过（day/week/month/quarter/year，
真实业务数据，带参数执行）。
"""

from __future__ import annotations

from enum import StrEnum

from knowflow_analytics.contracts import TimeGranularity

__all__ = ["SqlDialect", "count_where_sql"]


# 不含 ``%``（见模块注释第 2 条），且都 CAST 回 DATE 以保住类型契约（第 1 条）。
_MYSQL_DATE_TRUNC: dict[TimeGranularity, str] = {
    TimeGranularity.DAY: "CAST({column} AS DATE)",
    # WEEKDAY() 以周一为 0，与 PostgreSQL 的 DATE_TRUNC('week') 同为周一起始。
    TimeGranularity.WEEK: "CAST(DATE_SUB({column}, INTERVAL WEEKDAY({column}) DAY) AS DATE)",
    TimeGranularity.MONTH: (
        "CAST(MAKEDATE(YEAR({column}), 1) + INTERVAL (MONTH({column}) - 1) MONTH AS DATE)"
    ),
    TimeGranularity.QUARTER: (
        "CAST(MAKEDATE(YEAR({column}), 1) + INTERVAL (QUARTER({column}) - 1) QUARTER AS DATE)"
    ),
    TimeGranularity.YEAR: "CAST(MAKEDATE(YEAR({column}), 1) AS DATE)",
}


class SqlDialect(StrEnum):
    """一个数据源的执行方言。

    取值同时是 sqlglot 的方言名，省掉一张映射表——多一层映射就多一处会对不上的地方。
    """

    POSTGRES = "postgres"
    MYSQL = "mysql"

    def date_trunc_sql(self, column_sql: str, grain: TimeGranularity) -> str:
        """把一个已渲染的列表达式截断到指定粒度，结果保持日期类型。

        见模块注释第 1、2 条：交给 sqlglot 会得到字符串，用格式串会被 pymysql 拦下。
        """

        if self is SqlDialect.POSTGRES:
            return f"DATE_TRUNC('{grain.value}', {column_sql})"
        template = _MYSQL_DATE_TRUNC[grain]
        return template.format(column=column_sql)

    def ratio_numerator_sql(self, numerator_sql: str) -> str:
        """占比/同比的分子。

        见模块注释第 3 条：MySQL 的 DECIMAL 除法只留 6 位小数，转 DOUBLE 后与
        PostgreSQL 逐位一致。PostgreSQL 的 numeric 除法本身就够精度，不动。
        """

        if self is SqlDialect.POSTGRES:
            return numerator_sql
        return f"CAST({numerator_sql} AS DOUBLE)"

    def read_only_session_sql(
        self, *, statement_timeout_ms: int, lock_timeout_ms: int
    ) -> tuple[str, ...]:
        """只读事务与超时。语法两边完全不同，且这是**安全边界**，不能省。

        MySQL 的 ``READ ONLY`` **一定不能加 ``SESSION``**：那会把只读粘在连接上，而
        连接是池化复用的，于是下一个拿到这条连接的人（比如 Excel 导入的写入）会莫名
        其妙地写不进去。实测 ``SET SESSION TRANSACTION READ ONLY`` 污染后续事务，
        去掉 ``SESSION`` 后拦得住写、且不污染。这个错抄了不会报错，只会静默生效。

        超时用 ``SESSION`` 是可以的：每次执行前都会重设，且引擎按数据源独立。
        MySQL 没有 lock_timeout 的等价物；``innodb_lock_wait_timeout`` 以秒为单位且
        最小为 1，所以不足一秒的配置向上取整到 1 秒——宁可等久一点，也不要因为取整
        成 0 而被拒绝设置、退回到默认的 50 秒。
        """

        if self is SqlDialect.POSTGRES:
            return (
                "SET TRANSACTION READ ONLY",
                f"SET LOCAL statement_timeout = {statement_timeout_ms}",
                f"SET LOCAL lock_timeout = {lock_timeout_ms}",
            )
        lock_seconds = max(1, -(-lock_timeout_ms // 1000))
        return (
            "SET TRANSACTION READ ONLY",
            f"SET SESSION max_execution_time = {statement_timeout_ms}",
            f"SET SESSION innodb_lock_wait_timeout = {lock_seconds}",
        )


def count_where_sql(predicate_sql: str) -> str:
    """满足条件的行数。

    **不是方言方法**——因为正确写法两边通用，这里只是把它放到一处，防止再写回
    ``COUNT(*) FILTER (WHERE ...)``。

    那个写法是标准 SQL，PostgreSQL 支持而 MySQL 不支持；关键是 **sqlglot 和
    SQLAlchemy 都原样把它吐给 MySQL**（都实测过），没有任何库会替我们翻译，只会在
    数据库那头报语法错。而 ``SUM(CASE WHEN ...)`` 三种方言渲染完全一致。
    """

    return f"SUM(CASE WHEN {predicate_sql} THEN 1 ELSE 0 END)"
