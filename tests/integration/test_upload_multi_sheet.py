"""多 sheet 一次性导入，打真 PostgreSQL。

核心承诺只有一条：**失败不回滚已经成功的那几张**。这条只能对着真库验——建表和灌数
都发生在数据库里，替身证明不了"第三张失败时前两张还在"。

需要 ``KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL``（指向一个可建库的 PostgreSQL）。
"""

from __future__ import annotations

import io
import os

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, inspect, text

from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.ingest.uploads import ensure_upload_database


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    book = Workbook()
    book.remove(book.active)
    for name, rows in sheets.items():
        sheet = book.create_sheet(name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def application():
    url = os.getenv("KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL")
    if not url:
        pytest.skip("KNOWFLOW_ANALYTICS_TEST_UPLOAD_CATALOG_URL 未配置")
    app = AnalyticsApplication.__new__(AnalyticsApplication)
    app._catalog_database_url = url
    # 数据源登记不在这条测试的范围里：它要整套 catalog 装配，而这里验的是落库行为。
    app._ensure_upload_data_source = lambda _url: type("R", (), {"id": "ds_test"})()
    engine = create_engine(ensure_upload_database(url))
    with engine.begin() as connection:
        for name in ("多表_一", "多表_二", "多表_三"):
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    yield app, engine
    with engine.begin() as connection:
        for name in ("多表_一", "多表_二", "多表_三"):
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    engine.dispose()


@pytest.mark.postgres
def test_several_sheets_land_in_one_go(application) -> None:
    app, engine = application
    data = _xlsx({
        "一": [["门店", "金额"], ["A", 1], ["B", 2]],
        "二": [["编号", "门店"], ["001", "A"]],
    })

    result = app.commit_upload(data=data, plan=(("一", "多表_一"), ("二", "多表_二")))

    assert [item["row_count"] for item in result["results"]] == [2, 1]
    assert set(inspect(engine).get_table_names()) >= {"多表_一", "多表_二"}


@pytest.mark.postgres
def test_a_failure_keeps_what_already_landed(application) -> None:
    """第二张撞名失败，第一张和第三张照样进来。

    把前面成功的撤掉毫无道理——用户要的是"哪些进来了、哪些没有、为什么"。
    """

    app, engine = application
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE "多表_二" (占位 text)'))
    data = _xlsx({
        "一": [["门店"], ["A"]],
        "二": [["门店"], ["B"]],
        "三": [["门店"], ["C"]],
    })

    result = app.commit_upload(
        data=data, plan=(("一", "多表_一"), ("二", "多表_二"), ("三", "多表_三"))
    )

    outcomes = [(item["table"], item.get("row_count"), item.get("error", {}).get("code"))
                for item in result["results"]]
    assert outcomes == [
        ("多表_一", 1, None),
        ("多表_二", None, "UPLOAD_TABLE_EXISTS"),
        ("多表_三", 1, None),
    ]
    with engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM "多表_一"')).scalar() == 1
        assert connection.execute(text('SELECT count(*) FROM "多表_三"')).scalar() == 1


@pytest.mark.postgres
def test_a_duplicate_table_name_within_one_batch_is_refused_upfront(application) -> None:
    """同一批里两张 sheet 用同一个表名。

    不拦的话先建的会让后建的撞上"已存在"，报出来的原因和真正的错因对不上。
    """

    app, _engine = application
    data = _xlsx({"一": [["门店"], ["A"]], "二": [["门店"], ["B"]]})

    with pytest.raises(Exception) as excinfo:
        app.commit_upload(data=data, plan=(("一", "多表_一"), ("二", "多表_一")))

    assert getattr(excinfo.value, "code", "") == "UPLOAD_PLAN_DUPLICATE_TABLE"
