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

import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import TimeGranularity
from knowflow_analytics.errors import TranslationError

__all__ = ["SqlDialect", "count_where_sql", "render_physical_sql"]


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

    def explain_sql(self, sql: str) -> str:
        """查询计划语句。两边语法不同，且 MySQL 不认 PostgreSQL 的括号写法。

        实测 MySQL 对 ``EXPLAIN (FORMAT JSON) ...`` 直接报 1064 语法错。返回值形状
        也不同：PostgreSQL 给结构化的 JSON，MySQL 给一段 JSON **字符串**。
        """

        if self is SqlDialect.POSTGRES:
            return f"EXPLAIN (FORMAT JSON) {sql}"
        return f"EXPLAIN FORMAT=JSON {sql}"

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


def render_physical_sql(expression: exp.Expression, dialect: SqlDialect) -> str:
    """把翻译器产出的 AST 渲染成目标引擎真正能跑的 SQL。

    **物理 SQL 的唯一收口。** 翻译链路里那几十处 ``read="postgres"`` 处理的是内部
    S2SQL——一种建立在受治理业务名之上的中间语言，不是任何数据库的方言。它必须
    保持固定：LLM 按它写、黄金集按它存、确认记忆按它绑定，跟着数据源变会让同一个
    问题在 MySQL 上产出不同的 S2SQL 文本，把这些全部作废。

    真正需要随数据源变的只有最后这一步：AST → 发给数据库的字符串。所以整条链路上
    只有这一个函数认识目标引擎。

    ``DATE_TRUNC`` 必须在这里换掉而不是交给 sqlglot：它给 MySQL 的渲染返回字符串
    而不是日期（详见模块注释第 1 条）。未受治理的粒度一律拒绝——按小时截断在
    PostgreSQL 上能跑、在 MySQL 上没有对应写法，静默出一列错的时间比报错糟得多。
    """

    if dialect is SqlDialect.POSTGRES:
        return expression.sql(dialect=dialect.value)

    rendered = expression.copy()
    for order in rendered.find_all(exp.Order):
        _rewrite_null_ordering(order)
    rendered = rendered.transform(_rewrite_aggregate_filter, copy=False)

    def replace(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.TimestampTrunc):
            return node
        unit = node.args.get("unit")
        raw_unit = (unit.name if isinstance(unit, exp.Expression) else str(unit or "")).lower()
        try:
            grain = TimeGranularity(raw_unit)
        except ValueError as exc:
            raise TranslationError(
                f"time granularity is not supported on {dialect.value}: {raw_unit or '(empty)'}",
                code="UNSUPPORTED_TIME_GRANULARITY",
            ) from exc
        inner_sql = node.this.sql(dialect=dialect.value)
        return sqlglot.parse_one(dialect.date_trunc_sql(inner_sql, grain), read=dialect.value)

    return rendered.transform(replace, copy=False).sql(dialect=dialect.value)


def _rewrite_null_ordering(order: exp.Order) -> None:
    """在**别名**上补偿 NULL 排序差异，而不是让 sqlglot 展开成表达式。

    两件事叠在一起才成为问题：

    1. 两个引擎对 NULL 的默认位置相反（实测 ``ORDER BY x ASC``：PostgreSQL 把 NULL
       排最后，MySQL 排最前）。所以补偿是**必需**的，不能省——省了就是静默换了一种
       排序。
    2. sqlglot 做这个补偿时，会把 ``ORDER BY "t"`` 里的 select 别名解析成它背后的
       完整表达式。而按时间粒度分组时那个表达式是 ``CAST(MAKEDATE(...) ...)``，
       于是 ORDER BY 里出现了 ``CASE WHEN CAST(...) IS NULL ...``——MySQL 的
       ``ONLY_FULL_GROUP_BY`` 认不出它就是 GROUP BY 的那个表达式，直接以 1055 拒绝。
       实测：按月/周/季/年聚合**全部跑不起来**。

    自己补偿就没有第 2 个问题：``CASE WHEN `t` IS NULL ...`` 用的是别名，MySQL 在
    ``ONLY_FULL_GROUP_BY`` 下接受（实测）。补完把 ``nulls_first`` 设成 MySQL 的原生
    默认值，sqlglot 看到「要的就是默认行为」便不再重复补偿。
    """

    terms: list[exp.Expression] = []
    for ordered in order.expressions:
        if not isinstance(ordered, exp.Ordered):
            terms.append(ordered)
            continue
        descending = bool(ordered.args.get("desc"))
        wanted = ordered.args.get("nulls_first")
        if wanted is None:
            # PostgreSQL 的默认：DESC 时 NULL 在前，ASC 时在后。
            wanted = descending
        # MySQL 的原生行为：ASC 时 NULL 在前，DESC 时在后。
        native = not descending
        if bool(wanted) != native:
            nulls_rank = 0 if wanted else 1
            terms.append(
                exp.Ordered(
                    this=exp.Case(
                        ifs=[
                            exp.If(
                                this=exp.Is(this=ordered.this.copy(), expression=exp.Null()),
                                true=exp.Literal.number(nulls_rank),
                            )
                        ],
                        default=exp.Literal.number(1 - nulls_rank),
                    ),
                    desc=False,
                    nulls_first=True,
                )
            )
        ordered.set("nulls_first", native)
        terms.append(ordered)
    order.set("expressions", terms)


def _rewrite_aggregate_filter(node: exp.Expression) -> exp.Expression:
    """``AGG(x) FILTER (WHERE p)`` → ``AGG(CASE WHEN p THEN x END)``。

    这条是三处例外里最要命的：子集占比（``RATIO_TO_TOTAL(指标, 维度, 值)``）生成的
    物理 SQL 里就带着 ``FILTER``，MySQL 不支持，而 **sqlglot 和 SQLAlchemy 都原样
    把它吐过去**（都实测过）——占比类问题在 MySQL 数据源上会直接语法错。

    改写是把条件推进聚合的参数里。九种聚合形态在真 PostgreSQL 上逐值验证过等价，
    包括两个容易想当然的边界：无匹配行时 ``SUM`` 返 NULL 而 ``COUNT`` 返 0，
    ``CASE`` 写法两边的这个区别也保持一致。

    ``COUNT(*)`` 单独处理：星号不能塞进 ``CASE``，改数 1。
    ``COUNT(DISTINCT x)`` 的 ``CASE`` 要推到 ``DISTINCT`` 里面而不是外面。
    """

    if not isinstance(node, exp.Filter):
        return node
    aggregate = node.this
    predicate = node.args.get("expression")
    if isinstance(predicate, exp.Where):
        predicate = predicate.this
    if not isinstance(aggregate, exp.AggFunc) or predicate is None:
        return node

    def guarded(value: exp.Expression) -> exp.Expression:
        return exp.Case(
            ifs=[exp.If(this=predicate.copy(), true=value)],
            default=None,
        )

    rewritten = aggregate.copy()
    target = rewritten.this
    if isinstance(target, exp.Distinct):
        target.set("expressions", [guarded(item) for item in target.expressions])
    elif isinstance(target, exp.Star):
        # COUNT(*)：星号进不了 CASE，改数 1。行为一致——满足条件的行各计一次。
        rewritten.set("this", guarded(exp.Literal.number(1)))
    else:
        rewritten.set("this", guarded(target))
    return rewritten
