"""数据源 API 的合同。

这一层是凭据离开服务端的最后一道门，所以「响应里有没有连接串」逐个端点验。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from knowflow_analytics.api import create_api
from knowflow_analytics.catalog.data_sources import DataSourceError
from knowflow_analytics.catalog.store import DataSourceRecord

_SECRET = "unit-test-analytics-service-secret-value-32+"
_DSN = "postgresql+psycopg://alice:hunter2@10.0.0.7:5432/warehouse"

_HEADERS = {
    "X-KnowFlow-Service-Token": _SECRET,
    "X-KnowFlow-Actor-Id": "actor-1",
    "X-KnowFlow-Permission-Scope-Hash": "sha256:probe",
}


def _record(name: str = "生产库", engine: str = "postgres") -> DataSourceRecord:
    now = datetime.now(UTC)
    return DataSourceRecord(id="ds_1", name=name, engine=engine, created_at=now, updated_at=now)


class _FakeApplication:
    """只记录调用并返回记录，不碰数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.sources = [_record()]
        self.bound: DataSourceRecord | None = None
        self.create_error: Exception | None = None

    def list_data_sources(self):
        return tuple(self.sources)

    def create_data_source(self, *, name, engine, dsn):
        self.calls.append(("create", {"name": name, "engine": engine, "dsn": dsn}))
        if self.create_error is not None:
            raise self.create_error
        return _record(name=name, engine=engine)

    def update_data_source(self, *, data_source_id, name=None, dsn=None):
        self.calls.append(("update", {"id": data_source_id, "name": name, "dsn": dsn}))
        if data_source_id != "ds_1":
            return None
        return _record(name=name or "生产库")

    def delete_data_source(self, data_source_id):
        self.calls.append(("delete", {"id": data_source_id}))
        return data_source_id == "ds_1"

    def test_data_source(self, *, engine, dsn):
        self.calls.append(("test", {"engine": engine, "dsn": dsn}))

    def bind_project_data_source(self, *, project_id, data_source_id):
        self.calls.append(("bind", {"project": project_id, "id": data_source_id}))

    def unbind_project_data_source(self, project_id):
        self.calls.append(("unbind", {"project": project_id}))
        return True

    def get_project_data_source(self, project_id):
        self.calls.append(("get_bound", {"project": project_id}))
        return self.bound


@pytest.fixture()
def application() -> _FakeApplication:
    return _FakeApplication()


@pytest.fixture()
def client(application: _FakeApplication) -> TestClient:
    return TestClient(
        create_api(application=application, service_secret=_SECRET),
        raise_server_exceptions=False,
    )


def _project_headers(project_id: str = "prj_1") -> dict[str, str]:
    return {**_HEADERS, "X-KnowFlow-Project-Id": project_id}


class TestCredentialsNeverComeBack:
    def test_create_response_has_no_connection_string(
        self, client: TestClient, application: _FakeApplication
    ):
        """建完之后返回的记录里不能有连接串。

        这是凭据最容易漏出去的一处：请求里刚带过它，顺手回显就成了泄漏。
        """

        response = client.post(
            "/v1/analytics/data-sources",
            headers=_HEADERS,
            json={"name": "生产库", "engine": "postgres", "dsn": _DSN},
        )

        assert response.status_code == 200
        assert "hunter2" not in response.text
        assert "dsn" not in response.json()

    def test_listing_has_no_connection_string(self, client: TestClient):
        response = client.get("/v1/analytics/data-sources", headers=_HEADERS)

        assert "hunter2" not in response.text
        assert all("dsn" not in item for item in response.json()["items"])

    def test_update_response_has_no_connection_string(self, client: TestClient):
        response = client.put(
            "/v1/analytics/data-sources/ds_1",
            headers=_HEADERS,
            json={"dsn": _DSN},
        )

        assert response.status_code == 200
        assert "hunter2" not in response.text

    def test_probe_response_has_no_connection_string(self, client: TestClient):
        response = client.post(
            "/v1/analytics/data-sources:test",
            headers=_HEADERS,
            json={"engine": "postgres", "dsn": _DSN},
        )

        assert response.status_code == 200
        assert "hunter2" not in response.text

    def test_bound_source_response_has_no_connection_string(
        self, client: TestClient, application: _FakeApplication
    ):
        application.bound = _record()

        response = client.get(
            "/v1/analytics/projects/prj_1/data-source", headers=_project_headers()
        )

        assert "hunter2" not in response.text


class TestRouting:
    def test_create_passes_the_fields_through(
        self, client: TestClient, application: _FakeApplication
    ):
        client.post(
            "/v1/analytics/data-sources",
            headers=_HEADERS,
            json={"name": "MySQL 仓库", "engine": "mysql", "dsn": _DSN},
        )

        assert application.calls[0] == (
            "create",
            {"name": "MySQL 仓库", "engine": "mysql", "dsn": _DSN},
        )

    def test_unknown_source_update_is_a_404(self, client: TestClient):
        response = client.put(
            "/v1/analytics/data-sources/ds_nope", headers=_HEADERS, json={"name": "x"}
        )

        assert response.status_code == 404

    def test_unknown_source_delete_is_a_404(self, client: TestClient):
        response = client.delete("/v1/analytics/data-sources/ds_nope", headers=_HEADERS)

        assert response.status_code == 404

    def test_project_with_no_source_reports_null(self, client: TestClient):
        response = client.get(
            "/v1/analytics/projects/prj_1/data-source", headers=_project_headers()
        )

        assert response.json() == {"data_source": None}

    def test_bind_reaches_the_application(self, client: TestClient, application: _FakeApplication):
        client.put(
            "/v1/analytics/projects/prj_1/data-source",
            headers=_project_headers(),
            json={"data_source_id": "ds_1"},
        )

        assert ("bind", {"project": "prj_1", "id": "ds_1"}) in application.calls

    def test_unbind_reaches_the_application(
        self, client: TestClient, application: _FakeApplication
    ):
        client.delete("/v1/analytics/projects/prj_1/data-source", headers=_project_headers())

        assert ("unbind", {"project": "prj_1"}) in application.calls


class TestScopeAndAuth:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("post", "/v1/analytics/data-sources", {"name": "x", "engine": "postgres", "dsn": "d"}),
            ("post", "/v1/analytics/data-sources:test", {"engine": "postgres", "dsn": "d"}),
            ("put", "/v1/analytics/data-sources/ds_1", {"name": "x"}),
            ("delete", "/v1/analytics/data-sources/ds_1", None),
            ("get", "/v1/analytics/data-sources", None),
        ],
    )
    def test_every_endpoint_requires_the_service_token(
        self, client: TestClient, method: str, path: str, body
    ):
        response = (
            getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
        )

        assert response.status_code == 401

    def test_binding_a_project_outside_the_request_scope_is_refused(self, client: TestClient):
        """带着 A 项目的上下文去改 B 项目的数据源，必须拒。

        放过就等于让一个人把别人项目的数据源换掉。
        """

        response = client.put(
            "/v1/analytics/projects/prj_other/data-source",
            headers=_project_headers("prj_1"),
            json={"data_source_id": "ds_1"},
        )

        assert response.status_code == 403

    def test_reading_another_projects_data_source_is_refused(self, client: TestClient):
        response = client.get(
            "/v1/analytics/projects/prj_other/data-source",
            headers=_project_headers("prj_1"),
        )

        assert response.status_code == 403


class TestErrors:
    def test_unreachable_source_surfaces_its_code(
        self, client: TestClient, application: _FakeApplication
    ):
        application.create_error = DataSourceError(
            "could not connect to the data source: OperationalError",
            code="DATA_SOURCE_UNREACHABLE",
        )

        response = client.post(
            "/v1/analytics/data-sources",
            headers=_HEADERS,
            json={"name": "x", "engine": "postgres", "dsn": _DSN},
        )

        assert response.status_code >= 400
        assert "DATA_SOURCE_UNREACHABLE" in response.text

    def test_failure_messages_never_carry_the_connection_string(
        self, client: TestClient, application: _FakeApplication
    ):
        application.create_error = DataSourceError(
            "could not connect to the data source: OperationalError",
            code="DATA_SOURCE_UNREACHABLE",
        )

        response = client.post(
            "/v1/analytics/data-sources",
            headers=_HEADERS,
            json={"name": "x", "engine": "postgres", "dsn": _DSN},
        )

        assert "hunter2" not in response.text
