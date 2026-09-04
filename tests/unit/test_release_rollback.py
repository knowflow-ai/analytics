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


def test_rollback_points_the_project_at_the_previous_release(sales_release) -> None:
    """发布后发现口径算错，必须能一键切回上一版。

    此前唯一出路是重建候选版本、重跑体检、重备 30 条黄金问题、再发布，
    期间线上持续输出错误数据。
    """

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    first = _seed_release(store, project_id="sales", release_id="rel_1", minute=1)
    second = _seed_release(store, project_id="sales", release_id="rel_2", minute=2)

    assert store.get_project("sales").active_release_id == second

    rolled = store.rollback_active_release(project_id="sales")

    assert rolled == first
    assert store.get_project("sales").active_release_id == first


def test_rollback_requires_an_earlier_release(sales_release) -> None:
    """只有一个发布版本时无处可退，必须显式报错而不是静默无操作。"""

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    _seed_release(store, project_id="sales", release_id="rel_only", minute=1)

    with pytest.raises(CatalogError) as raised:
        store.rollback_active_release(project_id="sales")
    # code 是界面能把这句话翻成中文的唯一依据，笼统的 CATALOG_ERROR 翻不了。
    assert raised.value.code == "NO_EARLIER_RELEASE"


def test_rolling_back_twice_stops_at_the_earliest_release(sales_release) -> None:
    """回滚之后线上停在更早的那一版，此时"上一版"已经不存在了。

    界面上曾按"一共发过几版"决定要不要显示回滚入口——回滚过一次后仍然有两版，
    入口照常显示，点下去必然撞上这里。判据是"有没有比线上更早的发布"。
    """

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    _seed_release(store, project_id="sales", release_id="rel_old", minute=1)
    _seed_release(store, project_id="sales", release_id="rel_new", minute=2)

    assert store.rollback_active_release(project_id="sales") == "rel_old"

    with pytest.raises(CatalogError) as raised:
        store.rollback_active_release(project_id="sales")
    assert raised.value.code == "NO_EARLIER_RELEASE"


def test_rollback_on_a_project_that_never_published(sales_release) -> None:
    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    with pytest.raises(CatalogError):
        store.rollback_active_release(project_id="sales")


def test_rollback_keeps_exactly_one_active_release(sales_release) -> None:
    """publish 维护「同一项目下只有一条 status=active」的不变量。

    回滚只改 projects.active_release_id 而不碰 releases.status，会让指针指向
    一条 status='retired' 的记录，同时旧版本仍标着 active —— 两条记录自相矛盾，
    get_active_release() 会把 'retired' 当成线上状态返回给上层。
    """

    from sqlalchemy import select

    store = _store()
    store.create_project(project_id="sales", name="销售分析")
    first = _seed_release(store, project_id="sales", release_id="rel_1", minute=1)
    _seed_release(store, project_id="sales", release_id="rel_2", minute=2)

    store.rollback_active_release(project_id="sales")

    with store._engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(releases.c.id, releases.c.status).where(releases.c.project_id == "sales")
            ).all()
        )
    assert statuses[first] == "active"
    assert [key for key, value in statuses.items() if value == "active"] == [first]


def test_release_history_is_listed_newest_first_with_the_live_one_marked(sales_release) -> None:
    """能发布却看不到发过什么，回滚按钮也无从判断有没有上一版可退。"""

    store = _store()
    store.create_project(project_id="sales", name="销售")
    _seed_release(store, project_id="sales", release_id="rel-1", minute=1)
    _seed_release(store, project_id="sales", release_id="rel-2", minute=2)
    store.create_project(project_id="other", name="别的")
    _seed_release(store, project_id="other", release_id="rel-x", minute=3)

    listed = store.list_releases(project_id="sales")

    assert [item.id for item in listed] == ["rel-2", "rel-1"]
    assert [item.status for item in listed] == ["active", "retired"]

    store.rollback_active_release(project_id="sales")
    after = {item.id: item.status for item in store.list_releases(project_id="sales")}
    assert after == {"rel-2": "retired", "rel-1": "active"}
