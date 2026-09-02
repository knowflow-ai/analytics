"""数据源的存储层合同。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from knowflow_analytics.catalog.store import CatalogStore, data_sources


@pytest.fixture()
def catalog() -> CatalogStore:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    return store


_DSN_CIPHERTEXT = "gAAAAA-pretend-this-is-encrypted"


def _create(catalog: CatalogStore, *, name: str = "生产库", engine: str = "postgres"):
    return catalog.create_data_source(name=name, engine=engine, secret=_DSN_CIPHERTEXT)


class TestCreateAndList:
    def test_created_source_appears_in_the_list(self, catalog: CatalogStore):
        record = _create(catalog)

        assert [item.id for item in catalog.list_data_sources()] == [record.id]

    def test_identifier_is_prefixed_so_it_reads_as_a_data_source(self, catalog: CatalogStore):
        assert _create(catalog).id.startswith("ds_")

    def test_engine_is_recorded(self, catalog: CatalogStore):
        record = _create(catalog, engine="mysql")

        assert catalog.get_data_source(record.id).engine == "mysql"

    def test_get_returns_none_for_unknown_ids(self, catalog: CatalogStore):
        assert catalog.get_data_source("ds_nope") is None


class TestSecretsNeverLeak:
    def test_the_record_type_has_no_connection_string_field(self, catalog: CatalogStore):
        """返回值里根本没有装连接串的地方。

        比"记得脱敏"强：脱敏是每处都要记得做的事，没有字段是一次性的。
        """

        record = _create(catalog)

        assert not hasattr(record, "secret")
        assert _DSN_CIPHERTEXT not in record.model_dump_json()

    def test_listing_never_carries_the_secret(self, catalog: CatalogStore):
        _create(catalog)

        dumped = str([item.model_dump() for item in catalog.list_data_sources()])

        assert _DSN_CIPHERTEXT not in dumped

    def test_the_secret_is_reachable_only_through_the_dedicated_reader(self, catalog: CatalogStore):
        record = _create(catalog)

        assert catalog.read_data_source_dsn(record.id) == _DSN_CIPHERTEXT

    def test_reading_an_unknown_source_gives_none_not_an_error(self, catalog: CatalogStore):
        assert catalog.read_data_source_dsn("ds_nope") is None

    def test_what_lands_in_the_column_is_exactly_what_was_handed_over(self, catalog: CatalogStore):
        """存储层不做加密，也不该偷偷改写。

        加密在 DataSourceSecretBox；存储层再动一次手就会出现"两处都以为对方做了"。
        """

        record = _create(catalog)

        with catalog._engine.connect() as connection:
            stored = connection.execute(
                select(data_sources.c.secret).where(data_sources.c.id == record.id)
            ).scalar_one()

        assert stored == _DSN_CIPHERTEXT


class TestUpdate:
    def test_rename_keeps_the_identifier_and_engine(self, catalog: CatalogStore):
        record = _create(catalog, name="旧名")

        updated = catalog.update_data_source(data_source_id=record.id, name="新名")

        assert updated.id == record.id
        assert updated.name == "新名"
        assert updated.engine == record.engine

    def test_rotating_the_secret_replaces_it(self, catalog: CatalogStore):
        record = _create(catalog)

        catalog.update_data_source(data_source_id=record.id, secret="gAAAAA-rotated")

        assert catalog.read_data_source_dsn(record.id) == "gAAAAA-rotated"

    def test_renaming_does_not_disturb_the_secret(self, catalog: CatalogStore):
        record = _create(catalog)

        catalog.update_data_source(data_source_id=record.id, name="改个名")

        assert catalog.read_data_source_dsn(record.id) == _DSN_CIPHERTEXT

    def test_updating_an_unknown_source_returns_none(self, catalog: CatalogStore):
        assert catalog.update_data_source(data_source_id="ds_nope", name="x") is None

    def test_updated_at_moves_forward(self, catalog: CatalogStore):
        record = _create(catalog)

        updated = catalog.update_data_source(data_source_id=record.id, name="改名")

        # 去掉时区再比：SQLite 读回来是 naive，生产的 PostgreSQL 是 aware。
        # 与既有各表的处理一致，存储层不额外归一。
        assert updated.updated_at.replace(tzinfo=None) >= record.updated_at.replace(tzinfo=None)


class TestBinding:
    def test_a_fresh_project_has_no_data_source(self, catalog: CatalogStore):
        catalog.create_project(project_id="prj_1", name="p")

        assert catalog.get_project_data_source_id("prj_1") is None

    def test_binding_then_reading_returns_the_source(self, catalog: CatalogStore):
        record = _create(catalog)
        catalog.create_project(project_id="prj_1", name="p")

        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        assert catalog.get_project_data_source_id("prj_1") == record.id

    def test_rebinding_replaces_rather_than_duplicates(self, catalog: CatalogStore):
        """重复绑定必须是覆盖。

        插成两行的话 ``get`` 会看心情返回其中一个——同一个项目今天连生产库、
        明天连测试库，而且没有任何报错。
        """

        first = _create(catalog, name="一号")
        second = _create(catalog, name="二号")
        catalog.create_project(project_id="prj_1", name="p")

        catalog.bind_project_data_source(project_id="prj_1", data_source_id=first.id)
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=second.id)

        assert catalog.get_project_data_source_id("prj_1") == second.id

    def test_one_source_can_serve_several_projects(self, catalog: CatalogStore):
        record = _create(catalog)
        for project_id in ("prj_1", "prj_2"):
            catalog.create_project(project_id=project_id, name=project_id)
            catalog.bind_project_data_source(project_id=project_id, data_source_id=record.id)

        assert catalog.list_projects_using_data_source(record.id) == ("prj_1", "prj_2")

    def test_unbinding_leaves_the_project_without_a_source(self, catalog: CatalogStore):
        record = _create(catalog)
        catalog.create_project(project_id="prj_1", name="p")
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        assert catalog.unbind_project_data_source("prj_1") is True
        assert catalog.get_project_data_source_id("prj_1") is None

    def test_unbinding_something_unbound_reports_it(self, catalog: CatalogStore):
        assert catalog.unbind_project_data_source("prj_1") is False


class TestDelete:
    def test_deleting_removes_the_source(self, catalog: CatalogStore):
        record = _create(catalog)

        assert catalog.delete_data_source(record.id) is True
        assert catalog.get_data_source(record.id) is None

    def test_deleting_takes_its_bindings_with_it(self, catalog: CatalogStore):
        """不能留下悬空绑定。

        留着的话，那些项目要到下一次提问时才发现数据源没了——报错点离原因很远，
        而且看起来像是问数坏了。
        """

        record = _create(catalog)
        catalog.create_project(project_id="prj_1", name="p")
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        catalog.delete_data_source(record.id)

        assert catalog.get_project_data_source_id("prj_1") is None

    def test_deleting_does_not_touch_other_sources_bindings(self, catalog: CatalogStore):
        doomed = _create(catalog, name="要删的")
        kept = _create(catalog, name="要留的")
        for project_id, source in (("prj_1", doomed), ("prj_2", kept)):
            catalog.create_project(project_id=project_id, name=project_id)
            catalog.bind_project_data_source(project_id=project_id, data_source_id=source.id)

        catalog.delete_data_source(doomed.id)

        assert catalog.get_project_data_source_id("prj_2") == kept.id

    def test_deleting_an_unknown_source_reports_it(self, catalog: CatalogStore):
        assert catalog.delete_data_source("ds_nope") is False

    def test_the_secret_is_gone_after_deletion(self, catalog: CatalogStore):
        record = _create(catalog)

        catalog.delete_data_source(record.id)

        assert catalog.read_data_source_dsn(record.id) is None
