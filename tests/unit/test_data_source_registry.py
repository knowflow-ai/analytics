"""按项目解析数据源。

这一层决定「这个问题去问哪个库」，错了不会报错、只会给出另一个库的数字。
所以每条分支都单独钉住。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.data_sources import DataSourceError, DataSourceRegistry
from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.dialect import SqlDialect

_SECRET = "local-dev-only-analytics-service-secret-change-me"
_DEFAULT_URL = "sqlite+pysqlite:///:memory:"
_MYSQL_DSN = "sqlite+pysqlite:///:memory:"  # 只验证装配，不真连库


@pytest.fixture()
def catalog() -> CatalogStore:
    store = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema()
    store.create_project(project_id="prj_1", name="p")
    return store


@pytest.fixture()
def registry(catalog: CatalogStore) -> DataSourceRegistry:
    return DataSourceRegistry(
        catalog=catalog,
        secret_box=DataSourceSecretBox(_SECRET),
        default_database_url=_DEFAULT_URL,
    )


@pytest.fixture()
def reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """把连通性探针短路。

    探针会真的连库并下发只读会话语句，SQLite 不认那些语法。探针本身由
    ``tests/integration/test_data_source_probe.py`` 对真 PostgreSQL / 真 MySQL
    验证；这里关心的是加密、缓存、绑定这些逻辑。
    """

    monkeypatch.setattr(
        DataSourceRegistry,
        "test",
        lambda self, *, engine, dsn: None,  # noqa: ARG005
    )


def _bind(catalog: CatalogStore, *, engine: str = "postgres", dsn: str = _MYSQL_DSN) -> str:
    secret = DataSourceSecretBox(_SECRET).encrypt(dsn)
    record = catalog.create_data_source(name="仓库", engine=engine, secret=secret)
    catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)
    return record.id


class TestNoFallback:
    """没有回落。

    曾经有过：存量项目一个绑定行都没有，回落让它们继续工作。但那是一次迁移的活，
    不该做成一条永久的代码路径——回落意味着项目说不出自己连的是哪个库，而这正是
    最容易出静默错答的地方（本该问 A 库的问题悄悄问了 B 库，数字看起来完全正常）。
    """

    def test_unbound_project_is_refused(self, registry: DataSourceRegistry):
        with pytest.raises(DataSourceError) as excinfo:
            registry.for_project("prj_1")

        assert excinfo.value.code == "DATA_SOURCE_NOT_BOUND"


class TestDeploymentDefaultMigration:
    """把部署配置的那个库变成真实数据源。

    不迁移的话，升级当天所有存量项目一起报"没绑数据源"。迁移之后 UI 里也不再需要
    「默认库（部署配置）」这个魔法选项——它就是列表里一个普通数据源。
    """

    def test_migration_creates_a_real_data_source(self, registry: DataSourceRegistry):
        data_source_id = registry.ensure_default_data_source()

        assert data_source_id is not None
        record = registry.get(data_source_id)
        assert record is not None
        assert record.name == "默认数据源"

    def test_migration_binds_every_unbound_project(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        catalog.create_project(project_id="prj_2", name="p2")

        data_source_id = registry.ensure_default_data_source()

        assert catalog.get_project_data_source_id("prj_1") == data_source_id
        assert catalog.get_project_data_source_id("prj_2") == data_source_id

    def test_migrated_projects_resolve_again(self, registry: DataSourceRegistry):
        # 迁移的意义就在这：升级前能问数的项目，升级后还能问。
        registry.ensure_default_data_source()

        assert registry.for_project("prj_1").dialect is SqlDialect.POSTGRES

    def test_migration_is_idempotent(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        # 每次启动都跑：不幂等的话每重启一次就多一个"默认数据源"。
        first = registry.ensure_default_data_source()
        second = registry.ensure_default_data_source()

        assert first == second
        assert len(catalog.list_data_sources()) == 1

    def test_migration_never_touches_already_bound_projects(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        """已经绑好的项目不能被迁移改掉。

        改掉的话，一次重启就把用户手工绑的 MySQL 换回了默认库——而且不报错。
        """

        chosen = _bind(catalog)

        registry.ensure_default_data_source()

        assert catalog.get_project_data_source_id("prj_1") == chosen

    def test_migration_stores_the_connection_string_encrypted(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        data_source_id = registry.ensure_default_data_source()

        stored = catalog.read_data_source_dsn(data_source_id)

        assert stored != _DEFAULT_URL
        assert DataSourceSecretBox(_SECRET).decrypt(stored) == _DEFAULT_URL

    def test_no_configured_database_means_no_migration(self, catalog: CatalogStore):
        # 没配默认库的部署（比如全靠用户自己建数据源）不该凭空造一个连不上的记录。
        registry = DataSourceRegistry(
            catalog=catalog,
            secret_box=DataSourceSecretBox(_SECRET),
            default_database_url="",
        )

        assert registry.ensure_default_data_source() is None
        assert catalog.list_data_sources() == ()


class TestBoundProjects:
    def test_bound_project_reports_its_source(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        data_source_id = _bind(catalog)

        assert registry.for_project("prj_1").data_source_id == data_source_id

    def test_engine_field_selects_the_dialect(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        _bind(catalog, engine="mysql")

        assert registry.for_project("prj_1").dialect is SqlDialect.MYSQL

    def test_the_executor_carries_the_same_dialect(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        """执行器的方言必须来自数据源。

        装配时漏传就会变成"MySQL 数据源发 PostgreSQL 语法"，而且第一条查询才会暴露。
        """

        _bind(catalog, engine="mysql")

        assert registry.for_project("prj_1").executor._dialect is SqlDialect.MYSQL

    def test_the_guard_carries_the_same_dialect(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        _bind(catalog, engine="mysql")

        assert registry.for_project("prj_1").executor._guard._dialect is SqlDialect.MYSQL


class TestFailClosed:
    def test_a_binding_pointing_at_a_deleted_source_fails(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        """**绝不回落到默认数据源。**

        回落意味着本该问 A 库的问题悄悄问了 B 库——数字看起来完全正常，是最坏的
        一种错。
        """

        catalog.bind_project_data_source(project_id="prj_1", data_source_id="ds_gone")

        with pytest.raises(DataSourceError) as excinfo:
            registry.for_project("prj_1")

        assert excinfo.value.code == "DATA_SOURCE_NOT_FOUND"

    def test_an_unknown_engine_is_refused(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        _bind(catalog, engine="oracle")

        with pytest.raises(DataSourceError) as excinfo:
            registry.for_project("prj_1")

        assert excinfo.value.code == "DATA_SOURCE_ENGINE_UNSUPPORTED"

    def test_an_undecryptable_secret_fails_rather_than_falling_back(self, catalog: CatalogStore):
        from knowflow_analytics.catalog.secrets import SecretDecryptionError

        record = catalog.create_data_source(
            name="坏的", engine="postgres", secret="not-a-valid-fernet-token"
        )
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)
        registry = DataSourceRegistry(
            catalog=catalog,
            secret_box=DataSourceSecretBox(_SECRET),
            default_database_url=_DEFAULT_URL,
        )

        with pytest.raises(SecretDecryptionError):
            registry.for_project("prj_1")


class TestCaching:
    def test_repeated_lookups_reuse_the_same_engine(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        """每次提问都新建连接池的话，几十次提问就把数据库连接耗光了。"""

        _bind(catalog)

        first = registry.for_project("prj_1")
        second = registry.for_project("prj_1")

        assert first.engine is second.engine

    def test_rotating_the_connection_string_yields_a_new_engine(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        """改了连接串不能还连旧地址。

        只按数据源 id 缓存就会：用户改完连接信息、测试通过、一提问还是连的旧库，
        且要到进程重启才恢复。缓存键里带密文就自然换。
        """

        data_source_id = _bind(catalog)
        before = registry.for_project("prj_1")

        catalog.update_data_source(
            data_source_id=data_source_id,
            secret=DataSourceSecretBox(_SECRET).encrypt("sqlite+pysqlite:///other.db"),
        )
        after = registry.for_project("prj_1")

        assert after.engine is not before.engine

    def test_invalidate_drops_the_cached_connections(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        data_source_id = _bind(catalog)
        before = registry.for_project("prj_1")

        registry.invalidate(data_source_id)
        after = registry.for_project("prj_1")

        assert after.engine is not before.engine

    def test_invalidate_leaves_other_sources_alone(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        _bind(catalog)
        kept = registry.for_project("prj_1")

        registry.invalidate("ds_someone_else")

        assert registry.for_project("prj_1").engine is kept.engine

    def test_two_projects_on_one_source_share_the_engine(
        self, catalog: CatalogStore, registry: DataSourceRegistry
    ):
        data_source_id = _bind(catalog)
        catalog.create_project(project_id="prj_2", name="p2")
        catalog.bind_project_data_source(project_id="prj_2", data_source_id=data_source_id)

        assert registry.for_project("prj_1").engine is registry.for_project("prj_2").engine


class TestManagement:
    def test_create_stores_the_connection_string_encrypted(
        self, catalog: CatalogStore, registry: DataSourceRegistry, reachable: None
    ):
        record = registry.create(name="仓库", engine="postgres", dsn=_MYSQL_DSN)

        stored = catalog.read_data_source_dsn(record.id)

        assert stored != _MYSQL_DSN
        assert DataSourceSecretBox(_SECRET).decrypt(stored) == _MYSQL_DSN

    def test_create_refuses_an_unknown_engine(self, registry: DataSourceRegistry):
        with pytest.raises(DataSourceError) as excinfo:
            registry.create(name="x", engine="oracle", dsn=_MYSQL_DSN)

        assert excinfo.value.code == "DATA_SOURCE_ENGINE_UNSUPPORTED"

    def test_create_refuses_a_connection_that_does_not_work(self, registry: DataSourceRegistry):
        """存下去之前先连一下。

        不试就把"填错了"变成"以后每次提问都失败"，而用户当时看到的是保存成功。
        """

        with pytest.raises(DataSourceError) as excinfo:
            registry.create(
                name="x", engine="postgres", dsn="postgresql+psycopg://x:y@127.0.0.1:1/none"
            )

        assert excinfo.value.code == "DATA_SOURCE_UNREACHABLE"

    def test_connection_errors_never_carry_the_connection_string(
        self, registry: DataSourceRegistry
    ):
        """报错信息里不能带连接串。

        SQLAlchemy 的异常默认会把 URL 带上，里面就有密码，而这条消息会一路走到
        浏览器。
        """

        dsn = "postgresql+psycopg://alice:hunter2@127.0.0.1:1/none"

        with pytest.raises(DataSourceError) as excinfo:
            registry.test(engine="postgres", dsn=dsn)

        message = str(excinfo.value)
        assert "hunter2" not in message
        assert "alice" not in message
        assert dsn not in message

    def test_update_rotates_the_secret_and_drops_the_old_engine(
        self, catalog: CatalogStore, registry: DataSourceRegistry, reachable: None
    ):
        record = registry.create(name="仓库", engine="postgres", dsn=_MYSQL_DSN)
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)
        before = registry.for_project("prj_1")

        registry.update(data_source_id=record.id, dsn="sqlite+pysqlite:///rotated.db")

        assert registry.for_project("prj_1").engine is not before.engine

    def test_update_of_an_unknown_source_returns_none(
        self, registry: DataSourceRegistry, reachable: None
    ):
        assert registry.update(data_source_id="ds_nope", name="x") is None

    def test_delete_refuses_while_projects_still_use_it(
        self, catalog: CatalogStore, registry: DataSourceRegistry, reachable: None
    ):
        """还有人在用就不能删。

        直接删掉的话，那些项目要到下一次提问时才发现——报错点离原因很远，看起来
        像是问数坏了。先让用户看到自己在影响谁。
        """

        record = registry.create(name="仓库", engine="postgres", dsn=_MYSQL_DSN)
        catalog.bind_project_data_source(project_id="prj_1", data_source_id=record.id)

        with pytest.raises(DataSourceError) as excinfo:
            registry.delete(record.id)

        assert excinfo.value.code == "DATA_SOURCE_IN_USE"

    def test_delete_works_when_nobody_uses_it(
        self, catalog: CatalogStore, registry: DataSourceRegistry, reachable: None
    ):
        record = registry.create(name="仓库", engine="postgres", dsn=_MYSQL_DSN)

        assert registry.delete(record.id) is True

    def test_binding_to_an_unknown_source_is_refused(self, registry: DataSourceRegistry):
        # 允许的话会存下一条悬空绑定，那个项目从此问不了数且原因很难看出来。
        with pytest.raises(DataSourceError) as excinfo:
            registry.bind(project_id="prj_1", data_source_id="ds_nope")

        assert excinfo.value.code == "DATA_SOURCE_NOT_FOUND"

    def test_listing_never_exposes_connection_strings(
        self, registry: DataSourceRegistry, reachable: None
    ):
        registry.create(name="仓库", engine="postgres", dsn=_MYSQL_DSN)

        assert all(not hasattr(item, "secret") for item in registry.list())
