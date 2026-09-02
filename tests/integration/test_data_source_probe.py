"""连通性探针打真库。

保存数据源之前只有这一道关。它放过一条连不上的配置，用户看到的是"保存成功"，
然后每一次提问都失败——而失败信息在问数那头，离原因很远。

需要 PostgreSQL 与 MySQL 测试库（同 test_dialect_parity_mysql）。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from knowflow_analytics.catalog.data_sources import DataSourceError, DataSourceRegistry
from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.dialect import SqlDialect

_SECRET = "local-dev-only-analytics-service-secret-change-me"


@pytest.fixture()
def registry() -> DataSourceRegistry:
    catalog = CatalogStore(create_engine("sqlite+pysqlite:///:memory:"))
    catalog.create_schema()
    catalog.create_project(project_id="prj_1", name="p")
    return DataSourceRegistry(
        catalog=catalog,
        secret_box=DataSourceSecretBox(_SECRET),
        default_database_url="sqlite+pysqlite:///:memory:",
    )


def _url(name: str) -> str:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} 未配置")
    return value


@pytest.mark.postgres
def test_probe_accepts_a_working_postgres(registry: DataSourceRegistry):
    registry.test(engine=SqlDialect.POSTGRES, dsn=_url("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL"))


@pytest.mark.mysql
def test_probe_accepts_a_working_mysql(registry: DataSourceRegistry):
    """MySQL 的只读会话语法与 PostgreSQL 完全不同。

    探针下发的是方言自己的那组语句；用错方言这里就会红，而不是等到第一次提问。
    """

    registry.test(engine=SqlDialect.MYSQL, dsn=_url("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL"))


@pytest.mark.postgres
def test_probe_rejects_a_wrong_password(registry: DataSourceRegistry):
    url = _url("KNOWFLOW_ANALYTICS_TEST_DATABASE_URL").replace(
        "infini_rag_flow", "definitely-not-the-password"
    )

    with pytest.raises(DataSourceError) as excinfo:
        registry.test(engine=SqlDialect.POSTGRES, dsn=url)

    assert excinfo.value.code == "DATA_SOURCE_UNREACHABLE"


@pytest.mark.postgres
def test_probe_rejects_a_closed_port(registry: DataSourceRegistry):
    with pytest.raises(DataSourceError) as excinfo:
        registry.test(
            engine=SqlDialect.POSTGRES,
            dsn="postgresql+psycopg://u:p@127.0.0.1:1/none",
        )

    assert excinfo.value.code == "DATA_SOURCE_UNREACHABLE"


@pytest.mark.postgres
def test_probe_failures_never_carry_the_credentials(registry: DataSourceRegistry):
    """报错里不能有密码。

    SQLAlchemy 的异常默认把 URL 带上，而这条消息会一路走到浏览器。
    """

    dsn = "postgresql+psycopg://alice:hunter2@127.0.0.1:1/none"

    with pytest.raises(DataSourceError) as excinfo:
        registry.test(engine=SqlDialect.POSTGRES, dsn=dsn)

    text = f"{excinfo.value} {excinfo.value.__dict__}"
    assert "hunter2" not in text
    assert "alice" not in text


@pytest.mark.mysql
def test_probe_rejects_mysql_url_declared_as_postgres(registry: DataSourceRegistry):
    """引擎选错了要在这里被挡住。

    选错的话建模能扫出表、发布也能过，直到第一次提问才炸——那时用户已经建完
    整套语义模型了。
    """

    with pytest.raises(DataSourceError):
        registry.test(engine=SqlDialect.POSTGRES, dsn=_url("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL"))


@pytest.mark.mysql
def test_create_round_trips_a_real_mysql_source(registry: DataSourceRegistry):
    """建一个真 MySQL 数据源，绑给项目，取回来的组件方言必须是 MySQL。"""

    url = _url("KNOWFLOW_ANALYTICS_TEST_MYSQL_URL")

    record = registry.create(name="MySQL 仓库", engine="mysql", dsn=url)
    registry.bind(project_id="prj_1", data_source_id=record.id)
    binding = registry.for_project("prj_1")

    assert binding.dialect is SqlDialect.MYSQL
    assert binding.executor._dialect is SqlDialect.MYSQL
    assert binding.data_source_id == record.id
