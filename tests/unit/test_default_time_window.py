"""默认时间窗合同（2026-09-04 用户评审，确定性优先）。

两条底线：
- 用户问题里明确要求的时间一定不能错——S2SQL 只要在谓词里碰了时间维，一个字都不改。
- 没明确的，补了必须显示出来且一键可撤——窗单独投影成 ``default_time_window``，
  不混进用户自己的 filters；「不限时间」下钻必须真的把窗拿掉，不能被发布配置补回来。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from knowflow_analytics.catalog.store import PublishedRelease
from knowflow_analytics.contracts import (
    DatasetTimeDefaultConfig,
    QueryFilter,
    QueryResult,
    SemanticQuery,
    effective_time_default,
    time_window_label,
)
from knowflow_analytics.query.contracts import (
    QueryOptions,
    QueryRequest,
    QueryState,
    StructuredQueryRequest,
)
from knowflow_analytics.query.default_time_window import (
    inject_default_time_window,
    parse_time_window_marker,
)
from knowflow_analytics.query.mapper import SemanticMapper
from knowflow_analytics.query.orchestrator import CandidateOrchestrator
from knowflow_analytics.query.parser import LlmS2SqlParser, _default_time_range
from knowflow_analytics.query.service import AnalyticsQueryService
from knowflow_analytics.semantic import SemanticTranslator

RECENT_7 = DatasetTimeDefaultConfig(unit=7, period="DAY", time_mode="RECENT")
NOW = datetime(2026, 3, 31, 16, 30, tzinfo=UTC)


def _with_default_time(sales_release, **dataset_updates):
    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date", **dataset_updates}
    )
    return sales_release.model_copy(update={"datasets": (dataset,)})


class TestLabelsAndOverrides:
    def test_labels_read_like_a_person_would_say_them(self) -> None:
        assert time_window_label(RECENT_7) == "最近 7 天"
        assert (
            time_window_label(DatasetTimeDefaultConfig(unit=1, period="DAY", time_mode="CURRENT"))
            == "今天"
        )
        assert (
            time_window_label(DatasetTimeDefaultConfig(unit=1, period="MONTH", time_mode="LAST"))
            == "上个月"
        )
        assert (
            time_window_label(DatasetTimeDefaultConfig(unit=3, period="DAY", time_mode="LAST"))
            == "3 天前那一天"
        )

    def test_override_none_disables_and_days_win_over_the_published_config(
        self, sales_release
    ) -> None:
        dataset = _with_default_time(sales_release, aggregate_time_default=RECENT_7).datasets[0]
        assert effective_time_default(dataset, detail=False, override="none") is None
        assert effective_time_default(dataset, detail=False, override="dataset") == RECENT_7
        assert effective_time_default(
            dataset, detail=False, override=30
        ) == DatasetTimeDefaultConfig(unit=30, period="DAY", time_mode="RECENT")

    def test_day_count_is_bounded_and_never_a_bool(self) -> None:
        assert QueryOptions(default_time_window=7).default_time_window == 7
        assert QueryOptions(default_time_window="dataset").default_time_window == "dataset"
        for bad in (0, 3_651, True):
            with pytest.raises(ValueError):
                QueryOptions(default_time_window=bad)


class TestInjection:
    """只在最简单的形状上补窗；任何拿不准的情况都原样不动。"""

    def test_a_simple_aggregate_gets_the_window_and_a_visible_marker(self, sales_release) -> None:
        release = _with_default_time(sales_release)
        injected = inject_default_time_window(
            s2sql='SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"',
            release=release,
            dataset=release.datasets[0],
            config=RECENT_7,
            now=NOW,
        )

        assert injected is not None
        start, end = _default_time_range(RECENT_7, date(2026, 3, 31))
        assert f"\"下单日期\" >= '{start.isoformat()}'" in injected.s2sql
        assert f"\"下单日期\" < '{end.isoformat()}'" in injected.s2sql
        assert 'GROUP BY "区域"' in injected.s2sql
        assert injected.marker == f"time:order_date:{start.isoformat()}:{end.isoformat()}:最近 7 天"
        assert injected.window.dimension == "下单日期"
        assert injected.window.label == "最近 7 天"

    def test_the_window_is_anded_onto_an_existing_non_time_predicate(self, sales_release) -> None:
        release = _with_default_time(sales_release)
        injected = inject_default_time_window(
            s2sql='SELECT SUM("净收入") FROM "销售经营" WHERE "区域" = \'华东\'',
            release=release,
            dataset=release.datasets[0],
            config=RECENT_7,
            now=NOW,
        )

        assert injected is not None
        assert "\"区域\" = '华东'" in injected.s2sql
        assert " AND " in injected.s2sql

    def test_business_day_follows_the_dataset_timezone(self, sales_release) -> None:
        release = _with_default_time(sales_release, timezone="Asia/Shanghai")
        injected = inject_default_time_window(
            s2sql='SELECT SUM("净收入") FROM "销售经营"',
            release=release,
            dataset=release.datasets[0],
            config=RECENT_7,
            now=NOW,
        )

        assert injected is not None
        # UTC 16:30 在上海已是次日；窗按业务日历算，不按服务器时钟。
        start, end = _default_time_range(RECENT_7, date(2026, 4, 1))
        assert injected.window.start == start.isoformat()
        assert injected.window.end == end.isoformat()

    @pytest.mark.parametrize(
        "s2sql",
        [
            'SELECT SUM("净收入") FROM "销售经营" WHERE "下单日期" >= \'2025-01-01\'',
            'SELECT SUM("净收入") FROM "销售经营" '
            "WHERE \"下单日期\" BETWEEN '2025-01-01' AND '2025-12-31'",
            'SELECT "下单日期", SUM("净收入") FROM "销售经营" GROUP BY "下单日期" '
            "HAVING MIN(\"下单日期\") > '2025-01-01'",
        ],
    )
    def test_an_explicit_time_predicate_is_never_touched(self, sales_release, s2sql) -> None:
        release = _with_default_time(sales_release)
        assert (
            inject_default_time_window(
                s2sql=s2sql, release=release, dataset=release.datasets[0], config=RECENT_7, now=NOW
            )
            is None
        )

    def test_time_in_projection_or_grouping_alone_is_not_a_user_range(self, sales_release) -> None:
        """按天看趋势 ≠ 给了时间范围：这种问题最需要默认窗，否则拉全表。"""

        release = _with_default_time(sales_release)
        injected = inject_default_time_window(
            s2sql='SELECT "下单日期", SUM("净收入") FROM "销售经营" GROUP BY "下单日期"',
            release=release,
            dataset=release.datasets[0],
            config=RECENT_7,
            now=NOW,
        )
        assert injected is not None

    @pytest.mark.parametrize(
        "s2sql",
        [
            'WITH t AS (SELECT SUM("净收入") AS s FROM "销售经营") SELECT s FROM t',
            'SELECT "区域" FROM "销售经营" UNION ALL SELECT "区域" FROM "销售经营"',
            'SELECT "区域" FROM "销售经营" WHERE "区域" IN (SELECT "区域" FROM "销售经营")',
            'SELECT RATIO_OVER(SUM("净收入"), \'DAY\') FROM "销售经营"',
            'SELECT SUM("净收入") FROM "销售经营" WHERE "不存在的列" = 1',
        ],
    )
    def test_uncertain_shapes_are_skipped_not_guessed(self, sales_release, s2sql) -> None:
        release = _with_default_time(sales_release)
        assert (
            inject_default_time_window(
                s2sql=s2sql, release=release, dataset=release.datasets[0], config=RECENT_7, now=NOW
            )
            is None
        )

    def test_no_governed_time_dimension_means_no_window(self, sales_release) -> None:
        assert (
            inject_default_time_window(
                s2sql='SELECT SUM("净收入") FROM "销售经营"',
                release=sales_release,
                dataset=sales_release.datasets[0],
                config=RECENT_7,
                now=NOW,
            )
            is None
        )


class TestMarkerAndInterpretation:
    def test_marker_round_trips_and_legacy_markers_still_parse(self) -> None:
        parsed = parse_time_window_marker("time:order_date:2026-03-25:2026-04-01:最近 7 天")
        assert parsed is not None
        assert (parsed.dimension_id, parsed.start, parsed.end, parsed.label) == (
            "order_date",
            "2026-03-25",
            "2026-04-01",
            "最近 7 天",
        )
        legacy = parse_time_window_marker("time:order_date:2026-03-25:2026-04-01")
        assert legacy is not None and legacy.label == "2026-03-25 起"
        assert parse_time_window_marker("query_rule:recent") is None
        assert parse_time_window_marker("time:order_date") is None

    def test_the_window_is_split_out_of_the_user_filters(self, sales_release) -> None:
        query = SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
            filters=(
                QueryFilter(dimension_id="region", operator="eq", value="华东"),
                QueryFilter(dimension_id="order_date", operator="gte", value="2026-03-25"),
                QueryFilter(dimension_id="order_date", operator="lt", value="2026-04-01"),
            ),
        )
        interpretation = AnalyticsQueryService._interpretation(
            sales_release, query, ("time:order_date:2026-03-25:2026-04-01:最近 7 天",)
        )

        assert interpretation.filters == ("区域 = 华东",)
        window = interpretation.default_time_window
        assert window is not None
        assert (window.dimension, window.start, window.end, window.label) == (
            "下单日期",
            "2026-03-25",
            "2026-04-01",
            "最近 7 天",
        )

    def test_without_a_marker_time_filters_stay_user_filters(self, sales_release) -> None:
        query = SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            filters=(QueryFilter(dimension_id="order_date", operator="gte", value="2026-03-25"),),
        )
        interpretation = AnalyticsQueryService._interpretation(sales_release, query, ())
        assert interpretation.default_time_window is None
        assert interpretation.filters == ("下单日期 ≥ 2026-03-25",)


class _ReleaseProvider:
    def __init__(self, release, index) -> None:
        self.published = PublishedRelease(
            release=release.model_copy(update={"index_snapshot_id": index.id}),
            index_snapshot=index,
            status="active",
        )

    def get_active_release(self, _project_id):
        return self.published


class _Executor:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, *, query, release):
        self.sql.append(query.sql)
        return QueryResult(
            columns=tuple(item.element_id for item in query.columns), rows=(), row_count=0
        )


class _Gateway:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    def generate_json(self, **_kwargs):
        return {"thought": "按业务语义生成 S2SQL", "sql": self._sql}


def _service(release, index, sql: str | None = None) -> tuple[AnalyticsQueryService, _Executor]:
    executor = _Executor()
    orchestrator = CandidateOrchestrator(
        mapper=SemanticMapper(),
        llm_parser=LlmS2SqlParser(_Gateway(sql)) if sql is not None else None,
    )
    service = AnalyticsQueryService(
        releases=_ReleaseProvider(release, index),
        orchestrator=orchestrator,
        translator=SemanticTranslator(),
        executor=executor,
        selection_secret="drilldown-secret-of-at-least-32-bytes",
    )
    return service, executor


class TestNaturalLanguagePath:
    SQL = 'SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'

    def test_assistant_days_override_adds_a_visible_window(
        self, sales_release, sales_index
    ) -> None:
        release = _with_default_time(sales_release)
        service, executor = _service(release, sales_index, self.SQL)

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="各区域的净收入",
                dataset_ids=("sales_dataset",),
                options=QueryOptions(default_time_window=7),
            ),
            now=NOW,
        )

        assert response.state is QueryState.COMPLETED
        window = response.interpretation.default_time_window
        assert window is not None and window.label == "最近 7 天"
        assert window.dimension == "下单日期"
        # 权威 S2SQL 和真正执行的 SQL 都带着窗；回答里的用户过滤 chip 不带——那不是用户说的。
        assert '"下单日期" >=' in response.corrected_s2sql
        assert "order_date" in executor.sql[-1]
        assert response.interpretation.filters == ()
        assert any(
            item.startswith("time:order_date:") for item in response.interpretation.applied_defaults
        )

    def test_no_override_keeps_todays_behaviour(self, sales_release, sales_index) -> None:
        release = _with_default_time(sales_release, aggregate_time_default=RECENT_7)
        service, executor = _service(release, sales_index, self.SQL)

        response = service.query(
            QueryRequest(
                project_id="sales", question="各区域的净收入", dataset_ids=("sales_dataset",)
            ),
            now=NOW,
        )

        assert response.state is QueryState.COMPLETED
        assert response.interpretation.default_time_window is None
        assert "order_date" not in executor.sql[-1]

    def test_an_explicit_time_in_the_question_is_never_narrowed(
        self, sales_release, sales_index
    ) -> None:
        release = _with_default_time(sales_release)
        service, executor = _service(
            release,
            sales_index,
            'SELECT SUM("净收入") FROM "销售经营" WHERE "下单日期" >= \'2025-01-01\'',
        )

        response = service.query(
            QueryRequest(
                project_id="sales",
                question="2025 年以来的净收入",
                dataset_ids=("sales_dataset",),
                options=QueryOptions(default_time_window=7),
            ),
            now=NOW,
        )

        assert response.state is QueryState.COMPLETED
        assert response.interpretation.default_time_window is None
        # 权威文本原样：用户的下限还在，没有再被 AND 上一个默认窗。
        assert "2025-01-01" in response.corrected_s2sql
        assert response.corrected_s2sql.count('"下单日期"') == 1
        assert executor.sql


class TestStructuredPathAndDrilldown:
    def test_all_time_drilldown_really_drops_a_published_default(
        self, sales_release, sales_index
    ) -> None:
        """发布配置给了默认窗时，「不限时间」不能被结构化路径原样补回来。"""

        release = _with_default_time(sales_release, aggregate_time_default=RECENT_7)
        service, executor = _service(release, sales_index)
        base = SemanticQuery(
            dataset_id="sales_dataset",
            metric_ids=("net_revenue",),
            dimension_ids=("region",),
            limit=10,
        )
        response = service.query_structured(
            StructuredQueryRequest(project_id="sales", semantic_query=base), actor_id="user-1"
        )
        assert response.state is QueryState.COMPLETED
        window = response.interpretation.default_time_window
        assert window is not None and window.label == "最近 7 天"
        assert "order_date" in executor.sql[-1]

        all_time = next(
            item
            for item in response.drilldown
            if item.action == "retime" and item.label == "不限时间"
        )
        cleared = service.query_drilldown(
            project_id="sales",
            query_id=response.query_id,
            token=all_time.token,
            base_s2sql=response.corrected_s2sql,
            base_dataset_id=response.semantic_query.dataset_id,
            base_applied_defaults=response.interpretation.applied_defaults,
            base_release_id=response.release_id,
            base_spec_hash=response.spec_hash,
            actor_id="user-1",
        )

        assert cleared.state is QueryState.COMPLETED
        assert cleared.interpretation.default_time_window is None
        assert "order_date" not in executor.sql[-1]
