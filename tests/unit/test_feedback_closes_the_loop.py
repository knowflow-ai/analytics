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


def _archive(store, *, question: str, phrase: str = "", status: str = "resolved") -> int:
    """按列表上那一行的聚合口径归档。

    界面点的是"这一行"（一个说法），不是"这几条 id"——同一个说法的记录散在多页。
    """

    return store.update_failures_by_group(
        project_id="p",
        kind="inferred",
        phrase=phrase,
        resolution="",
        question=question,
        status=status,
        actor_id="u1",
        now=datetime.now(UTC),
    )


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

        assert _archive(store, question="各门店的业绩") == 1

        remaining, total = store.list_failure_groups(project_id="p")
        assert remaining == () and total == 0
        # 但没有被删掉：这份数据同时是补词典的依据和"这一版比上一版好"的素材。
        archived, archived_total = store.list_failure_groups(project_id="p", status="resolved")
        assert archived_total == 1 and archived[0].question == "各门店的业绩"

    def test_another_project_is_not_touched(self, store) -> None:
        """说法在项目之间会重名，归档不限定项目就会改到别人的记录。"""

        store.save_failure(_record("各门店的业绩"), actor_id="a", project_id="p")
        store.save_failure(_record("各门店的业绩"), actor_id="a", project_id="theirs")

        assert _archive(store, question="各门店的业绩") == 1

        assert store.list_failure_groups(project_id="p")[1] == 0
        assert store.list_failure_groups(project_id="theirs")[1] == 1


class TestGroupingHappensBeforePaging:
    """聚合必须发生在分页之前，否则次数、排序、总数三样全是错的。

    此前是「取最新 50 行 → 前端 group by」：实测一个被问过 21 次的说法散在三页，
    第一页显示 2 次、第二页 10 次、再往后 9 次——三行谁都不等于 21。
    """

    def test_a_saying_asked_across_pages_counts_every_occurrence(self, store) -> None:
        for _ in range(21):
            store.save_failure(
                _record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p"
            )
        # 足够多的杂音，把那 21 条挤到分页边界之外。
        for index in range(30):
            store.save_failure(_record(f"杂音{index}"), actor_id="a", project_id="p")

        page, total = store.list_failure_groups(project_id="p", limit=5)

        assert total == 31, "31 种说法，不是 51 行"
        top = page[0]
        assert top.phrase == "业绩"
        assert top.count == 21, "次数是全库的，不是这一页里数出来的"

    def test_archiving_one_row_covers_the_records_on_other_pages(self, store) -> None:
        """点一下归档，那个说法就该整个消失，而不是只消掉看得见的几条。"""

        for _ in range(21):
            store.save_failure(
                _record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p"
            )

        assert _archive(store, question="各门店的业绩", phrase="业绩") == 21
        assert store.list_failure_groups(project_id="p")[1] == 0

    def test_the_same_question_splits_by_saying(self, store) -> None:
        """同一句问话按不同说法聚合成不同行——说法才是这一页的主语。"""

        store.save_failure(_record("各城市有多少门店"), actor_id="a", project_id="p")
        store.save_failure(
            _record("各城市有多少门店", phrases=("多少",)), actor_id="a", project_id="p"
        )

        page, total = store.list_failure_groups(project_id="p")

        assert total == 2
        assert sorted(item.phrase for item in page) == ["", "多少"]


class TestAddingTheTermClosesTheLoop:
    def test_records_naming_the_same_phrase_are_resolved(self, store) -> None:
        store.save_failure(
            _record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p"
        )
        store.save_failure(
            _record("门店业绩排名", terms=(("业绩", "销售金额"),)), actor_id="a", project_id="p"
        )
        store.save_failure(_record("各门店的毛利"), actor_id="a", project_id="p")

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("业绩",), actor_id="u1", now=datetime.now(UTC)
        )

        assert changed == 2
        remaining, total = store.list_failure_groups(project_id="p")
        assert total == 1 and remaining[0].question == "各门店的毛利"

    def test_a_similar_phrase_is_not_treated_as_the_same(self, store) -> None:
        """只认字面相等：把「毛利」当成「毛利率」处理会静默改掉不该动的记录。"""

        store.save_failure(
            _record("各门店的毛利率", phrases=("毛利率",)), actor_id="a", project_id="p"
        )

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("毛利",), actor_id="u1", now=datetime.now(UTC)
        )

        assert changed == 0
        assert store.list_failure_groups(project_id="p")[1] == 1

    def test_already_handled_records_are_left_alone(self, store) -> None:
        store.save_failure(
            _record("各门店的业绩", phrases=("业绩",)), actor_id="a", project_id="p"
        )
        _archive(store, question="各门店的业绩", phrase="业绩", status="ignored")

        changed = store.resolve_failures_by_phrase(
            project_id="p", phrases=("业绩",), actor_id="u2", now=datetime.now(UTC)
        )

        assert changed == 0, "用户明确忽略过的不该被自动改成已解决"


class TestPagination:
    def test_the_total_counts_everything_not_just_this_page(self, store) -> None:
        """没有总数就说不出"还剩多少条待处理"——而那正是这个页面存在的意义。"""

        for index in range(7):
            store.save_failure(_record(f"问题{index}"), actor_id="a", project_id="p")

        page, total = store.list_failure_groups(project_id="p", limit=3)

        assert len(page) == 3 and total == 7

    def test_offset_reaches_the_older_ones(self, store) -> None:
        """此前只能取最近 N 条，第 N+1 条以后永远看不到。"""

        for index in range(5):
            store.save_failure(_record(f"问题{index}"), actor_id="a", project_id="p")

        first, _ = store.list_failure_groups(project_id="p", limit=2, offset=0)
        second, _ = store.list_failure_groups(project_id="p", limit=2, offset=2)

        assert [item.question for item in first] == ["问题0", "问题1"]
        assert [item.question for item in second] == ["问题2", "问题3"]


class TestArchivedIsOneThingToTheUser:
    """已处理和已忽略对建模者是同一件事：我不用再看它了。

    界面上给这两者各开一个页签，等于把系统内部的记账变成用户要理解的两个概念——
    实测它们合计只占 6% 的数据，进去之后还没有任何可执行的操作。合并成「已归档」，
    取数就必须能一次拿到两者。
    """

    def test_archived_covers_both_resolved_and_ignored(self, store) -> None:
        for question in ("问一", "问二", "问三"):
            store.save_failure(_record(question), actor_id="a", project_id="p")

        _archive(store, question="问一", status="resolved")
        _archive(store, question="问二", status="ignored")

        archived, total = store.list_failure_groups(project_id="p", status="archived")

        assert total == 2
        assert sorted(item.question for item in archived) == ["问一", "问二"]
        # 待办里只剩没动过的那条。
        assert store.list_failure_groups(project_id="p")[1] == 1
