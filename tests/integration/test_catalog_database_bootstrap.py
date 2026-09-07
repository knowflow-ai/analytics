"""目录库不存在时，服务自己把它建出来。

这条只能对着真 PostgreSQL 验：`metadata.create_all` 建表、建不出库，
"库不存在"是连接层面的失败，替身证明不了服务能从这个状态里恢复。

需要 ``KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL``（指向一个可建库的 PostgreSQL），
与上传库那条集成测试复用同一个环境变量。
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

from knowflow_analytics.catalog.store import CatalogStore, ensure_catalog_database


def _url_with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _maintenance_url(url: str) -> str:
    return _url_with_database(url, "postgres")


@pytest.fixture
def base_url() -> str:
    url = os.getenv("KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL")
    if not url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL 未配置")
    return url


@pytest.fixture
def absent_database(base_url: str):
    """一个确定不存在的库名，用完删掉。"""

    name = f"analytics_bootstrap_{uuid.uuid4().hex[:12]}"
    yield name
    admin = create_engine(_maintenance_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _database_exists(base_url: str, name: str) -> bool:
    admin = create_engine(_maintenance_url(base_url), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            return bool(
                connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
                ).scalar()
            )
    finally:
        admin.dispose()


def test_the_catalog_database_is_created_when_it_does_not_exist(base_url, absent_database):
    url = _url_with_database(base_url, absent_database)
    assert not _database_exists(base_url, absent_database)

    ensure_catalog_database(url)

    assert _database_exists(base_url, absent_database)


def test_the_schema_lands_in_the_freshly_created_database(base_url, absent_database):
    """建库之后表也要能建起来——这两步合起来才是"服务能启动"。"""

    url = _url_with_database(base_url, absent_database)
    ensure_catalog_database(url)

    engine = create_engine(url)
    try:
        CatalogStore(engine).create_schema()
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT to_regclass('public.analytics_data_source')")
            ).scalar()
    finally:
        engine.dispose()


def test_calling_it_again_is_harmless(base_url, absent_database):
    """重启、多副本同时起、升级重跑，都会再走一遍这条路径。"""

    url = _url_with_database(base_url, absent_database)
    ensure_catalog_database(url)
    ensure_catalog_database(url)

    assert _database_exists(base_url, absent_database)


def test_an_existing_database_is_left_alone(base_url, absent_database):
    """已有数据的库不能被重建——那会静默清空线上语义模型。"""

    url = _url_with_database(base_url, absent_database)
    ensure_catalog_database(url)

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE canary (id int)"))
            connection.execute(text("INSERT INTO canary VALUES (1)"))
    finally:
        engine.dispose()

    ensure_catalog_database(url)

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id FROM canary")).scalar() == 1
    finally:
        engine.dispose()


def test_a_non_postgres_url_is_left_to_sqlalchemy(tmp_path):
    """SQLite 没有"库不存在"这回事，不该去连一个不存在的 maintenance 库。"""

    ensure_catalog_database(f"sqlite:///{tmp_path / 'catalog.db'}")
