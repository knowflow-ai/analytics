from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.contracts import ModelingRevision, RevisionState


def _store() -> CatalogStore:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    store = CatalogStore(engine)
    store.create_schema()
    return store


def _revision(sales_catalog, *, revision_id: str, project_id: str) -> ModelingRevision:
    catalog = sales_catalog.model_copy(
        update={"revision_id": revision_id, "project_id": project_id}
    )
    return ModelingRevision(
        id=revision_id,
        project_id=project_id,
        schema_snapshot_hash="sha256:schema",
        etag=1,
        state=RevisionState.DRAFT,
        semantic_catalog=catalog,
        semantic_spec=compile_semantic_catalog(catalog),
    )


def test_projects_are_listed_by_owner_prefix_newest_first(sales_catalog):
    """退出项目后此前从 UI 上不可达：模块内没有列表，只靠 sessionStorage。

    归属编码在 id 前缀里（prj_u{owner}_{nonce}），本服务只按前缀过滤，不认识 actor。
    """

    store = _store()
    store.create_project(project_id="prj_uaaaaaaaaaaaaaaaaaaaa_" + "1" * 32, name="旧的")
    store.create_project(project_id="prj_uaaaaaaaaaaaaaaaaaaaa_" + "2" * 32, name="新的")
    store.create_project(project_id="prj_ubbbbbbbbbbbbbbbbbbbb_" + "3" * 32, name="别人的")

    listed = store.list_projects(id_prefix="prj_uaaaaaaaaaaaaaaaaaaaa_")

    assert [item.name for item in listed] == ["新的", "旧的"]


def test_listing_carries_the_latest_revision_so_the_ui_can_open_it(sales_catalog):
    """打开项目要从最近更新的 revision 进入；没有单独的 revision 列表接口，
    所以列表自己带上它，前端一次调用就能打开。"""

    store = _store()
    project_id = "prj_uaaaaaaaaaaaaaaaaaaaa_" + "1" * 32
    store.create_project(project_id=project_id, name="销售")
    store.create_project(project_id="prj_uaaaaaaaaaaaaaaaaaaaa_" + "2" * 32, name="空的")
    store.save_revision(_revision(sales_catalog, revision_id="rev-old", project_id=project_id))
    newer = _revision(sales_catalog, revision_id="rev-new", project_id=project_id)
    store.save_revision(newer)

    by_id = {item.id: item for item in store.list_projects(id_prefix="prj_uaaaaaaaaaaaaaaaaaaaa_")}

    assert by_id[project_id].latest_revision_id == "rev-new"
    assert by_id["prj_uaaaaaaaaaaaaaaaaaaaa_" + "2" * 32].latest_revision_id is None
