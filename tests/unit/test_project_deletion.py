"""删项目要删干净。

漏一张表的后果不是报错，是"删完了还留着业务数据"——确认记忆里存着真实的维度取值，
诊断产物里存着物理 SQL。所以这里不逐表点名，而是照着 metadata 全量验。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select

from knowflow_analytics.catalog.store import CatalogStore, data_sources, metadata, projects


@pytest.fixture()
def catalog() -> CatalogStore:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    return store


def _scoped_tables():
    return [
        table
        for table in metadata.sorted_tables
        if table is not projects and "project_id" in table.c
    ]


def _seed_everything(catalog: CatalogStore, project_id: str) -> None:
    """往每张带 project_id 的表塞一行。

    照着 metadata 生成，所以将来新增的表会自动被这个测试覆盖——不必记得回来改。
    """

    now = datetime.now(UTC)
    with catalog._engine.begin() as connection:
        for table in _scoped_tables():
            # create_project 已经写过治理那一行，再塞会撞唯一键。
            existing = connection.execute(
                select(table).where(table.c.project_id == project_id)
            ).first()
            if existing is not None:
                continue
            values: dict[str, object] = {"project_id": project_id}
            for column in table.columns:
                if column.name in values:
                    continue
                if column.nullable or column.default is not None:
                    continue
                # 自增主键交给数据库，硬塞会在第二个项目上撞。
                if (
                    column.primary_key
                    and column.autoincrement is not False
                    and getattr(column.type, "python_type", str) is int
                ):
                    continue
                python_type = getattr(column.type, "python_type", str)
                if python_type is datetime:
                    values[column.name] = now
                elif python_type is int:
                    values[column.name] = 1
                elif python_type is bool:
                    values[column.name] = False
                elif python_type in (dict, list):
                    values[column.name] = {}
                else:
                    # 带上项目 id：两个项目一起塞时主键才不会撞。
                    values[column.name] = f"seed-{project_id}-{column.name}"
            connection.execute(insert(table).values(**values))


def _remaining(catalog: CatalogStore, project_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    with catalog._engine.connect() as connection:
        for table in _scoped_tables():
            rows = connection.execute(
                select(table).where(table.c.project_id == project_id)
            ).all()
            if rows:
                counts[table.name] = len(rows)
    return counts


class TestDeletesEverything:
    def test_every_project_scoped_table_is_cleared(self, catalog: CatalogStore):
        """照着 metadata 验，不逐表点名。

        手写清单必然会漏：这个目录 22 张表里 20 张带 project_id，而且还在长。
        """

        catalog.create_project(project_id="prj_1", name="要删的")
        _seed_everything(catalog, "prj_1")
        assert _remaining(catalog, "prj_1"), "夹具本身没塞进数据，测不出东西"

        assert catalog.delete_project("prj_1") is True

        assert _remaining(catalog, "prj_1") == {}

    def test_the_project_row_itself_is_gone(self, catalog: CatalogStore):
        catalog.create_project(project_id="prj_1", name="要删的")

        catalog.delete_project("prj_1")

        with catalog._engine.connect() as connection:
            assert connection.execute(
                select(projects).where(projects.c.id == "prj_1")
            ).all() == []

    def test_the_binding_goes_too(self, catalog: CatalogStore):
        # 留着悬空绑定的话，同名项目重建后会莫名继承上一个项目的数据源。
        record = catalog.create_data_source(name="仓库", engine="postgres", secret="x")
        catalog.create_project(project_id="prj_1", name="要删的")
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        catalog.delete_project("prj_1")

        assert catalog.get_project_data_source_id("prj_1") is None


class TestLeavesOthersAlone:
    def test_other_projects_survive(self, catalog: CatalogStore):
        for project_id in ("prj_1", "prj_2"):
            catalog.create_project(project_id=project_id, name=project_id)
            _seed_everything(catalog, project_id)

        catalog.delete_project("prj_1")

        assert _remaining(catalog, "prj_1") == {}
        assert _remaining(catalog, "prj_2")

    def test_data_sources_are_not_project_scoped(self, catalog: CatalogStore):
        """删项目不能带走数据源。

        数据源是连接，可能还有别的项目在用；一起删掉就是一次删除毁掉多个项目。
        """

        record = catalog.create_data_source(name="仓库", engine="postgres", secret="x")
        catalog.create_project(project_id="prj_1", name="要删的")
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        catalog.delete_project("prj_1")

        assert catalog.get_data_source(record.id) is not None

    def test_data_source_table_has_no_project_scope(self):
        # 上一条的结构性保证：它压根不在删除范围里，不是靠删除逻辑手下留情。
        assert "project_id" not in data_sources.c


class TestIdempotence:
    def test_deleting_a_missing_project_reports_it(self, catalog: CatalogStore):
        assert catalog.delete_project("prj_nope") is False

    def test_deleting_twice_is_safe(self, catalog: CatalogStore):
        # BFF 会重试（宿主清完、核心失败时），第二次不能炸。
        catalog.create_project(project_id="prj_1", name="要删的")

        assert catalog.delete_project("prj_1") is True
        assert catalog.delete_project("prj_1") is False
