"""问数反馈要能收口。

这张表原本只是追加日志，但用户是当**工作队列**在用：一条条看、处理、消掉。三个症状
同一个根因——没有状态、没有分页、补了词典记录也不动：

- 「一堆问题怎么删」→ 没有状态列，消不掉
- 「补到词典后仍然还在」→ 词典和这些记录之间没有任何联系
- 「没有分页」→ 只能取最近 N 条，第 N+1 条以后永远看不到
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.query.contracts import QueryFailureRecord


@pytest.fixture
def store():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    catalog = CatalogStore(engine)
    catalog.create_schema()
    return catalog


def _record(question: str, *, phrases=(), terms=()) -> QueryFailureRecord:
    return QueryFailureRecord(
        kind="inferred",
        question=question,
        stage="CANDIDATE_DISCOVERY",
        code="SEMANTIC_INFERRED",
        release_id="rel",
        spec_hash="hash",
        index_snapshot_id="idx",
        unmatched_phrases=tuple(phrases),
        inferred_terms=tuple(terms),
    )


class TestHandledOnesGoAway:
    def test_a_resolved_record_leaves_the_default_list(self, store) -> None:
        store.save_failure(_record("各门店的业绩"), actor_id="a", project_id="p")
        listed, _total = store.list_failures(project_id="p")

        store.update_failure_status(
            project_id="p",
            failure_ids=(listed[0].id,),
            status="resolved",
            actor_id="u1",
            now=datetime.now(UTC),
        )

        remaining, total = store.list_failures(project_id="p")
        assert remaining == () and total == 0
        # 但没有被删掉：这份数据同时是补词典的依据和"这一版比上一版好"的素材。
        archived, archived_total = store.list_failures(project_id="p", status="resolved")
        assert archived_total == 1 and archived[0].question == "各门店的业绩"

    def test_another_project_is_not_touched(self, store) -> None:
        """id 是全局自增的，只按 id 更新会改到别的项目的记录。"""

        store.save_failure(_record("我的"), actor_id="a", project_id="mine")
        store.save_failure(_record("别人的"), actor_id="a", project_id="theirs")
        theirs, _ = store.list_failures(project_id="theirs")

        changed = store.update_failure_status(
            project_id="mine",
            failure_ids=(theirs[0].id,),
            status="resolved",
            actor_id="u1",
            now=datetime.now(UTC),
        )

        assert changed == 0
        assert store.list_failures(project_id="theirs")[1] == 1


class TestAddingTheTermClosesTheLoop:
    def test_records_naming_the_same_phrase_are_resolved(self, store) -> None:
        """用户补了「业绩」，说的是「业绩」的那些记录就该消失。"""

        store.save_failure(_record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p")
        store.save_failure(
            _record("门店业绩排名", terms=(("业绩", "销售金额"),)), actor_id="a", project_id="p"
        )
        store.save_failure(
            _record("各门店的坪效", phrases=("坪效",)), actor_id="a", project_id="p"
        )

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("业绩",), actor_id="u1", now=datetime.now(UTC)
        )

        assert changed == 2, "两个来源记下的说法都要能消掉，只看一个的话另一个消不了"
        remaining, total = store.list_failures(project_id="p")
        assert total == 1 and remaining[0].question == "各门店的坪效"

    def test_a_similar_phrase_is_not_treated_as_the_same(self, store) -> None:
        """不做模糊匹配——把「业绩」和「营业额」当成一回事，用户会以为自己补过了。"""

        store.save_failure(
            _record("各门店的营业额", phrases=("营业额",)), actor_id="a", project_id="p"
        )

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("业绩",), actor_id="u1", now=datetime.now(UTC)
        )

        assert changed == 0
        assert store.list_failures(project_id="p")[1] == 1

    def test_already_handled_records_are_left_alone(self, store) -> None:
        store.save_failure(_record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p")
        listed, _ = store.list_failures(project_id="p")
        store.update_failure_status(
            project_id="p", failure_ids=(listed[0].id,), status="ignored",
            actor_id="u1", now=datetime.now(UTC),
        )

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("业绩",), actor_id="u2", now=datetime.now(UTC)
        )

        assert changed == 0, "用户明确忽略过的不该被自动改成已解决"


class TestPagination:
    def test_the_total_counts_everything_not_just_this_page(self, store) -> None:
        """没有总数就说不出"还剩多少条待处理"——而那正是这个页面存在的意义。"""

        for index in range(7):
            store.save_failure(_record(f"问题{index}"), actor_id="a", project_id="p")

        page, total = store.list_failures(project_id="p", limit=3)

        assert len(page) == 3 and total == 7

    def test_offset_reaches_the_older_ones(self, store) -> None:
        """此前只能取最近 N 条，第 N+1 条以后永远看不到。"""

        for index in range(5):
            store.save_failure(_record(f"问题{index}"), actor_id="a", project_id="p")

        first, _ = store.list_failures(project_id="p", limit=2, offset=0)
        second, _ = store.list_failures(project_id="p", limit=2, offset=2)

        assert [item.question for item in first] == ["问题4", "问题3"]
        assert [item.question for item in second] == ["问题2", "问题1"]
