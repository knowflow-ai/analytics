from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, update
from sqlalchemy.pool import StaticPool

from knowflow_analytics.catalog.store import (
    CatalogError,
    CatalogStore,
    projects,
    releases,
)


def _store() -> CatalogStore:
    store = CatalogStore(
        create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    )
    store.create_schema()
    return store


def _seed_release(store: CatalogStore, *, project_id: str, release_id: str, minute: int):
    """直接写入一条已发布记录并把它设为线上版本。

    真实发布链路要构造语义索引快照，对回滚这一行为的测试是不必要的负担。
    """

    with store._engine.begin() as connection:
        connection.execute(
            insert(releases).values(
                id=release_id,
                project_id=project_id,
                revision_id=f"rev_{release_id}",
                spec_hash=f"sha256:{release_id}",
                index_snapshot_id=f"idx_{release_id}",
                status="active",
                payload={"id": release_id},
                created_at=datetime(2026, 8, 22, 10, minute, tzinfo=UTC),
            )
        )
        # 复刻真实 publish 的不变量：先把旧的 active 置为 retired，
        # 再让新记录成为唯一 active（store.py 的 publish 就是这么做的）。
        connection.execute(
            update(releases)
            .where(releases.c.project_id == project_id)
            .where(releases.c.id != release_id)
            .where(releases.c.status == "active")
            .values(status="retired")
        )
        connection.execute(
            update(projects).where(projects.c.id == project_id).values(active_release_id=release_id)
        )
    return release_id


def test_activating_an_earlier_release_switches_production(sales_release) -> None:
    """发布后发现口径算错，必须能一键切回上一版。

    此前唯一出路是重建候选版本、重跑体检、重备黄金问题、再发布，期间线上持续
    输出错误数据。
    """

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    first = _seed_release(store, project_id="sales", release_id="rel_1", minute=1)
    second = _seed_release(store, project_id="sales", release_id="rel_2", minute=2)

    assert store.get_project("sales").active_release_id == second

    assert store.activate_release(project_id="sales", release_id=first) == first
    assert store.get_project("sales").active_release_id == first


def test_switching_back_to_a_newer_release_is_possible(sales_release) -> None:
    """切换必须是双向的。

    原先的 `rollback_active_release` 只往更早走一步，没有回头路：切过一次之后
    线上停在最早那版，更新的那版仍列在发布历史里却再也切不回去——用户看着"第 2
    版 · 历史"，没有任何入口能回到它。
    """

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    old = _seed_release(store, project_id="sales", release_id="rel_old", minute=1)
    new = _seed_release(store, project_id="sales", release_id="rel_new", minute=2)

    store.activate_release(project_id="sales", release_id=old)
    assert store.get_project("sales").active_release_id == old

    store.activate_release(project_id="sales", release_id=new)
    assert store.get_project("sales").active_release_id == new


def test_activating_the_live_release_changes_nothing(sales_release) -> None:
    """重复点已经在线上的那一版是无操作，不该报错也不该翻搅 status。"""

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    _seed_release(store, project_id="sales", release_id="rel_1", minute=1)
    live = _seed_release(store, project_id="sales", release_id="rel_2", minute=2)

    assert store.activate_release(project_id="sales", release_id=live) == live
    assert store.get_project("sales").active_release_id == live


def test_a_release_from_another_project_is_refused(sales_release) -> None:
    """归属校验不能只靠路由。

    release id 是可猜的；拿别的项目的快照当自己的线上版本，会让问数用上另一个
    项目的语义模型和索引。
    """

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    _seed_release(store, project_id="sales", release_id="rel_mine", minute=1)
    store.create_project(project_id="other", name="别的")
    theirs = _seed_release(store, project_id="other", release_id="rel_theirs", minute=2)

    with pytest.raises(CatalogError) as raised:
        store.activate_release(project_id="sales", release_id=theirs)
    assert raised.value.code == "RELEASE_NOT_FOUND"
    assert store.get_project("sales").active_release_id == "rel_mine"
    # 对方项目的线上版本也不能被顺手改掉。
    assert store.get_project("other").active_release_id == theirs


def test_an_unknown_release_is_refused(sales_release) -> None:
    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    _seed_release(store, project_id="sales", release_id="rel_1", minute=1)

    with pytest.raises(CatalogError) as raised:
        store.activate_release(project_id="sales", release_id="rel_nope")
    # code 是界面能把这句话翻成中文的唯一依据，笼统的 CATALOG_ERROR 翻不了。
    assert raised.value.code == "RELEASE_NOT_FOUND"


def test_activation_keeps_exactly_one_active_release(sales_release) -> None:
    """publish 维护「同一项目下只有一条 status=active」的不变量。

    切换只改 projects.active_release_id 而不碰 releases.status，会让指针指向
    一条 status='retired' 的记录，同时旧版本仍标着 active —— 两条记录自相矛盾，
    get_active_release() 会把 'retired' 当成线上状态返回给上层。
    """

    from sqlalchemy import select

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    first = _seed_release(store, project_id="sales", release_id="rel_1", minute=1)
    _seed_release(store, project_id="sales", release_id="rel_2", minute=2)

    store.activate_release(project_id="sales", release_id=first)

    with store._engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(releases.c.id, releases.c.status).where(releases.c.project_id == "sales")
            ).all()
        )
    assert statuses[first] == "active"
    assert [key for key, value in statuses.items() if value == "active"] == [first]


def test_release_history_is_listed_newest_first_with_the_live_one_marked(sales_release) -> None:
    """能发布却看不到发过什么，就无从判断该切到哪一版。"""

    store = _store()
    store.create_project(project_id="sales", name="销售")
    _seed_release(store, project_id="sales", release_id="rel-1", minute=1)
    _seed_release(store, project_id="sales", release_id="rel-2", minute=2)
    store.create_project(project_id="other", name="别的")
    _seed_release(store, project_id="other", release_id="rel-x", minute=3)

    listed = store.list_releases(project_id="sales")

    assert [item.id for item in listed] == ["rel-2", "rel-1"]
    assert [item.status for item in listed] == ["active", "retired"]
    # 序号按发布先后编号，跟列表顺序（最新在前）相反。
    assert [item.sequence for item in listed] == [2, 1]

    store.activate_release(project_id="sales", release_id="rel-1")
    after = {item.id: item.status for item in store.list_releases(project_id="sales")}
    assert after == {"rel-2": "retired", "rel-1": "active"}
