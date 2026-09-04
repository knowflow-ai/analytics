"""上传数据落到哪里，以及怎么落。

**独立数据库，不放目录库的 schema 里。** 上传目标必须成为一条数据源记录，问数才连得上；
而目录库里躺着 `analytics_data_source`（加密连接串）、发布版本、查询历史。一条指向目录库
的数据源 DSN 等于把元数据库整个纳入查询连接的可达范围——语义层只暴露建模过的表，但连接
本身的面大得多。同一个 PostgreSQL 实例、同样的凭据、换一个 database，成本几乎为零。

建不出库就**明确报错**让管理员去建。这里不做静默回落：回落到目录库意味着上面那条边界
在没人知道的情况下消失了。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.ingest.workbook import SheetPreview

UPLOAD_DATABASE = "analytics_uploads"
UPLOAD_DATA_SOURCE_NAME = "上传的表格"
_INSERT_BATCH = 1_000


class UploadError(AnalyticsError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message, code=code, stage="INGEST")


def upload_database_url(catalog_database_url: str) -> str:
    """把目录库的连接串换个 database——同实例、同凭据、不同库。"""

    parts = urlsplit(catalog_database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{UPLOAD_DATABASE}", "", ""))


def ensure_upload_database(catalog_database_url: str) -> str:
    """确保上传库存在，返回它的连接串。"""

    url = upload_database_url(catalog_database_url)
    engine = create_engine(url)
    try:
        with engine.connect():
            return url
    except Exception:  # noqa: BLE001 连不上有很多种原因，先当成"还没建"去建一次
        pass
    finally:
        engine.dispose()

    parts = urlsplit(catalog_database_url)
    maintenance = urlunsplit((parts.scheme, parts.netloc, "/postgres", "", ""))
    admin = create_engine(maintenance, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{UPLOAD_DATABASE}"'))
    except Exception as exc:  # noqa: BLE001
        raise UploadError(
            f"上传库「{UPLOAD_DATABASE}」不存在，且当前数据库账号建不出来。"
            f"请让管理员在同一个 PostgreSQL 实例上创建它并授权。",
            code="UPLOAD_DATABASE_UNAVAILABLE",
        ) from exc
    finally:
        admin.dispose()
    return url


def table_exists(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def create_table(engine: Engine, *, table: str, preview: SheetPreview) -> None:
    """建表。同名表不覆盖——覆盖会让一张已经建好模型、已发布的表在用户不知情时换掉结构。"""

    if table_exists(engine, table):
        raise UploadError(
            f"已经有一张叫「{table}」的表了。请换一个名字，或先删掉那张表。",
            code="UPLOAD_TABLE_EXISTS",
        )
    columns = ", ".join(f'"{item.name}" {item.sql_type}' for item in preview.columns)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE "{table}" ({columns})'))


def insert_rows(engine: Engine, *, table: str, preview: SheetPreview, rows: Iterable[tuple]) -> int:
    names = ", ".join(f'"{item.name}"' for item in preview.columns)
    placeholders = ", ".join(f":c{index}" for index in range(len(preview.columns)))
    statement = text(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})')
    written = 0
    with engine.begin() as connection:
        for batch in _batched(rows, _INSERT_BATCH):
            connection.execute(
                statement,
                [{f"c{index}": value for index, value in enumerate(row)} for row in batch],
            )
            written += len(batch)
    return written


def _batched(rows: Iterable[tuple], size: int) -> Iterator[list[tuple]]:
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
