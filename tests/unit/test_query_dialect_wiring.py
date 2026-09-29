"""翻译器的每个调用点都必须带上数据源的方言。

``translate()`` 的 ``dialect`` 默认是 PostgreSQL。漏传不会报错，只会渲染出双引号
标识符，PostgreSQL 数据源上一切正常（默认值恰好相同），MySQL 数据源上以 1064 失败，
用户只看到一句 "mysql query failed"。这类错在 PostgreSQL 的测试里永远是绿的，所以
这里用 MySQL 执行目标逐个钉住：真正送到执行器的 SQL 不得含 PostgreSQL 记法。

2026-09-28 客户实机：MySQL 数据源发布检查的全部指标样本阻断（PR #3 修了样本那一处）；
问数里「命中查询规则后重译」「补默认时间窗后重译」两处同样漏传。
"""

from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.contracts import (
    QueryResult,
    QueryRuleMode,
    QueryRuleSpec,
    QueryRuleType,
)
from knowflow_analytics.errors import QueryExecutionError
from knowflow_analytics.execution.dialect import SqlDialect
from knowflow_analytics.modeling.quality import ModelingQualityProfiler, QualityStatus
from knowflow_analytics.query.contracts import QueryOptions, QueryRequest, QueryState
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser
from knowflow_analytics.query.service import AnalyticsQueryService, _FixedExecutionTarget
from knowflow_analytics.semantic import SemanticTranslator
from tests.unit.test_default_time_window import _Gateway, _ReleaseProvider, _with_default_time

NOW = datetime(2026, 3, 31, 16, 30, tzinfo=UTC)
SQL = 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'


class _RecordingExecutor:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, *, query, release):  # noqa: ARG002
        self.sql.append(query.sql)
        return QueryResult(
            columns=tuple(item.element_id for item in query.columns), rows=(), row_count=0
        )


def _assert_mysql_sql(sql: str) -> None:
    assert "`" in sql, sql
    assert '"' not in sql, sql


def _mysql_service(release, index) -> tuple[AnalyticsQueryService, _RecordingExecutor]:
    executor = _RecordingExecutor()
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=CandidateOrchestrator(
            mapper=SemanticMapper(), llm_parser=LlmS2SqlParser(_Gateway(SQL))
        ),
        translator=SemanticTranslator(),
        executor=executor,
        execution_targets=_FixedExecutionTarget(executor, dialect=SqlDialect.MYSQL),
        selection_secret="dialect-wiring-secret-of-at-least-32-bytes",
    )
    return service, executor


def _ask(service: AnalyticsQueryService, **options):
    return service.query(
        QueryRequest(
            project_id="sales",
            question="各区域的净收入",
            dataset_ids=("sales_dataset",),
            options=QueryOptions(**options),
        ),
        now=NOW,
    )


def test_plain_natural_language_query_renders_mysql(sales_release, sales_index) -> None:
    """基线：没有规则、没有默认窗时这条路本来就对。"""

    service, executor = _mysql_service(sales_release, sales_index)

    assert _ask(service).state is QueryState.COMPLETED
    _assert_mysql_sql(executor.sql[-1])


def test_retranslation_after_a_query_rule_keeps_the_mysql_dialect(
    sales_release, sales_index
) -> None:
    release = sales_release.model_copy(
        update={
            "query_rules": (
                QueryRuleSpec(
                    id="region-add-channel",
                    dataset_id="sales_dataset",
                    priority=2,
                    rule_type=QueryRuleType.ADD_SELECT,
                    mode=QueryRuleMode.EXIST,
                    parameters=("region",),
                    outputs=("channel",),
                ),
            )
        }
    )
    service, executor = _mysql_service(release, sales_index)

    response = _ask(service)

    assert response.state is QueryState.COMPLETED
    # 规则确实生效了（加了渠道），走到的是规则之后那次重译。
    assert '"渠道"' in response.corrected_s2sql
    assert "`channel`" in executor.sql[-1]
    _assert_mysql_sql(executor.sql[-1])


def test_retranslation_after_the_default_time_window_keeps_the_mysql_dialect(
    sales_release, sales_index
) -> None:
    service, executor = _mysql_service(_with_default_time(sales_release), sales_index)

    response = _ask(service, default_time_window=7)

    assert response.state is QueryState.COMPLETED
    assert response.interpretation.default_time_window is not None
    _assert_mysql_sql(executor.sql[-1])


class TestMetricPreview:
    @staticmethod
    def _profiler(executor) -> ModelingQualityProfiler:
        return ModelingQualityProfiler(object(), executor, dialect=SqlDialect.MYSQL)

    def test_metric_preview_renders_the_datasource_dialect(self, sales_release) -> None:
        executor = _RecordingExecutor()

        preview = self._profiler(executor).preview_metric(
            "sales_dataset", "net_revenue", sales_release
        )

        assert preview.status is QualityStatus.PENDING_REVIEW
        _assert_mysql_sql(executor.sql[-1])

    def test_a_failed_preview_says_what_the_database_said(self, sales_release) -> None:
        """只写 "mysql query failed" 等于没说：客户截图 8 条阻断一模一样，看不出原因。

        数据库原话在执行器里已经按安全规则截取进 ``details``（不含 SQL 原文），
        样本卡上把它带出来。
        """

        class _Failing:
            def execute(self, *, query, release):  # noqa: ARG002
                raise QueryExecutionError(
                    "mysql query failed",
                    details={
                        "sqlstate": "42S22",
                        "database_message": "Unknown column 'm0.amount' in 'field list'",
                    },
                )

        preview = self._profiler(_Failing()).preview_metric(
            "sales_dataset", "net_revenue", sales_release
        )

        assert preview.status is QualityStatus.BLOCKING
        assert preview.message == (
            "mysql query failed: Unknown column 'm0.amount' in 'field list' [42S22]"
        )
