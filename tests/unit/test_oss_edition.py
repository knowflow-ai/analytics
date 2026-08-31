from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import SecretStr

from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.oss import server as oss_server
from knowflow_analytics.oss.config import ConfigStore, ModelEndpoint, OssConfig
from knowflow_analytics.oss.gateways import (
    OpenAiCompatibleEmbeddingGateway,
    OpenAiCompatibleModelGateway,
)
from knowflow_analytics.oss.runtime import OSS_ACTOR_ID, OSS_SCOPE_HASH, OssSettings

# --- config -----------------------------------------------------------------


def _full_config() -> OssConfig:
    return OssConfig(
        datasource_database_url=SecretStr("postgresql://u:pw@db/app"),
        chat_model=ModelEndpoint(base_url="http://llm/v1/", api_key=SecretStr("k1"), model="m"),
        embedding_model=ModelEndpoint(base_url="http://emb/v1", api_key=SecretStr("k2"), model="e"),
    )


def test_public_view_masks_secrets_and_reports_completion() -> None:
    view = _full_config().public_view()
    assert view["datasource_database_url"] == "postgresql+psycopg://u:********@db/app"
    assert view["chat_model"] == {
        "base_url": "http://llm/v1",
        "api_key": "********",
        "model": "m",
        # The browser edits the output budget and the thinking mode, so both
        # have to survive the round trip that masks the key.
        "max_output_tokens": None,
        "thinking": "auto",
    }
    assert view["configured"] == {"datasource": True, "chat_model": True, "embedding_model": True}
    assert OssConfig().public_view()["configured"]["datasource"] is False


def test_merge_keeps_stored_secrets_when_mask_is_echoed_back() -> None:
    current = _full_config()
    incoming = OssConfig.model_validate(
        {
            # Host edited, password left masked: keep only the password.
            "datasource_database_url": "postgresql://u:********@db2:5433/app2",
            "chat_model": {"base_url": "http://llm/v1", "api_key": "********", "model": "m2"},
            "embedding_model": {"base_url": "http://emb/v1", "api_key": "fresh", "model": "e"},
        }
    )
    merged = current.merged_with(incoming)
    assert (
        merged.datasource_database_url.get_secret_value()
        == "postgresql+psycopg://u:pw@db2:5433/app2"
    )
    assert merged.chat_model.api_key.get_secret_value() == "k1"
    assert merged.chat_model.model == "m2"
    assert merged.embedding_model.api_key.get_secret_value() == "fresh"


def test_masked_api_key_never_follows_a_new_base_url() -> None:
    current = _full_config()
    incoming = OssConfig.model_validate(
        {
            "datasource_database_url": "postgresql://u:********@db/app",
            "chat_model": {"base_url": "http://attacker/v1", "api_key": "********", "model": "m"},
            "embedding_model": {"base_url": "http://emb/v1", "api_key": "********", "model": "e"},
        }
    )
    with pytest.raises(ValueError, match="API Key"):
        current.merged_with(incoming)


def test_query_string_secrets_are_masked() -> None:
    view = OssConfig(
        datasource_database_url=SecretStr("postgresql://u@db/app?password=s3cret&sslmode=require")
    ).public_view()
    assert "s3cret" not in view["datasource_database_url"]
    assert "sslmode=require" in view["datasource_database_url"]


def test_config_store_round_trips_and_restricts_permissions(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "data")
    assert not store.load().is_complete()
    store.save(_full_config())
    assert store.load().is_complete()
    assert json.loads(store.path.read_text())["chat_model"]["api_key"] == "k1"
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_endpoint_rejects_non_http_base_url() -> None:
    with pytest.raises(ValueError):
        ModelEndpoint(base_url="ftp://x", model="m")


# --- gateways ---------------------------------------------------------------


def _chat_client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://llm/v1", transport=httpx.MockTransport(handler))


def test_model_gateway_downgrades_response_format_once_and_parses_fenced_json() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        mode = body.get("response_format", {}).get("type", "text")
        seen.append(mode)
        if mode == "json_schema":
            return httpx.Response(400, json={"error": "response_format json_schema unsupported"})
        assert request.headers["authorization"] == "Bearer k1"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]},
        )

    endpoint = ModelEndpoint(base_url="http://llm/v1", api_key=SecretStr("k1"), model="m")
    gateway = OpenAiCompatibleModelGateway(endpoint, client=_chat_client(handler))
    assert gateway.probe() == "m"
    assert gateway.probe() == "m"
    # The rejected json_schema mode is not retried on the second call.
    assert seen == ["json_schema", "json_object", "json_object"]


def test_model_gateway_rejects_non_object_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "[1, 2]"}}]})

    gateway = OpenAiCompatibleModelGateway(
        ModelEndpoint(base_url="http://llm/v1", model="m"), client=_chat_client(handler)
    )
    with pytest.raises(ModelGatewayError) as exc:
        gateway.probe()
    assert exc.value.code == "MODEL_OUTPUT_INVALID"


def test_endpoint_output_ceiling_replaces_the_built_in_one() -> None:
    """Only the operator knows how much a deployment may emit.

    ``/v1/models`` declares no capability on the providers we have measured, so
    a deployment that can write 64k stays clamped to the built-in ceiling unless
    the setting overrides it.
    """

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

    def ask(endpoint: ModelEndpoint, hint: str | None) -> None:
        OpenAiCompatibleModelGateway(endpoint, client=_chat_client(handler)).generate_json(
            purpose="p",
            messages=[{"role": "user", "content": "x"}],
            response_schema=schema,
            trace={"max_tokens_hint": hint} if hint else {},
        )

    base = {"base_url": "http://llm/v1", "model": "m"}
    ask(ModelEndpoint(**base), None)
    ask(ModelEndpoint(**base), "65536")
    ask(ModelEndpoint(**base, max_output_tokens=65_536), "65536")

    assert seen == [4096, 16_384, 65_536]


def test_thinking_off_is_sent_in_every_spelling_providers_actually_read() -> None:
    """Reasoning spends the answer's budget, so the toggle has to reach the model.

    No provider agrees on the spelling - SiliconFlow and DashScope read the
    top-level flag, vLLM reads chat_template_kwargs - and OpenAI-compatible
    servers ignore fields they do not know, so both are sent.
    """

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})

    base = {"base_url": "http://llm/v1", "model": "m"}
    OpenAiCompatibleModelGateway(ModelEndpoint(**base), client=_chat_client(handler)).probe()
    OpenAiCompatibleModelGateway(
        ModelEndpoint(**base, thinking="off"), client=_chat_client(handler)
    ).probe()

    assert "enable_thinking" not in bodies[0]
    assert "chat_template_kwargs" not in bodies[0]
    assert bodies[1]["enable_thinking"] is False
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_reasoning_truncation_is_not_reported_as_malformed_output() -> None:
    """An empty content beside a populated reasoning trace means "never finished".

    Calling that malformed output hides the only two things that fix it, so it
    gets its own code and a message naming both settings.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "thinking..."},
                    }
                ]
            },
        )

    gateway = OpenAiCompatibleModelGateway(
        ModelEndpoint(base_url="http://llm/v1", model="m"), client=_chat_client(handler)
    )
    with pytest.raises(ModelGatewayError) as exc:
        gateway.probe()

    assert exc.value.code == "MODEL_OUTPUT_TRUNCATED_IN_REASONING"


def test_an_empty_answer_without_any_reasoning_stays_malformed_output() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    gateway = OpenAiCompatibleModelGateway(
        ModelEndpoint(base_url="http://llm/v1", model="m"), client=_chat_client(handler)
    )
    with pytest.raises(ModelGatewayError) as exc:
        gateway.probe()

    assert exc.value.code == "MODEL_OUTPUT_INVALID"


def test_embedding_gateway_orders_vectors_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "e" and body["input"] == ["a", "b"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    gateway = OpenAiCompatibleEmbeddingGateway(
        ModelEndpoint(base_url="http://emb/v1", model="e"),
        client=httpx.Client(base_url="http://emb/v1", transport=httpx.MockTransport(handler)),
    )
    batch = gateway.encode(("a", "b"))
    assert batch.model_id == "e" and batch.dimension == 2
    assert batch.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert gateway.for_tenant("anyone") is gateway


# --- shell ------------------------------------------------------------------


class _FakeRuntime:
    """Stands in for OssRuntime: a tiny core app that echoes the injected headers."""

    def __init__(self, settings: OssSettings) -> None:
        self.settings = settings
        self.service_secret = "s" * 48
        self.config = OssConfig()
        self.core_error = None
        core = FastAPI()

        @core.get("/v1/analytics/projects")
        def projects(
            x_knowflow_service_token: str = Header(),
            x_knowflow_actor_id: str = Header(),
            x_knowflow_permission_scope_hash: str = Header(),
            x_knowflow_project_id: str | None = Header(default=None),
        ):
            return {
                "token": x_knowflow_service_token,
                "actor": x_knowflow_actor_id,
                "scope": x_knowflow_permission_scope_hash,
                "project": x_knowflow_project_id,
            }

        self.core = SimpleNamespace(api=core)

    def close(self) -> None:
        return None


def _shell(monkeypatch, tmp_path: Path, **overrides) -> TestClient:
    settings = OssSettings(
        catalog_database_url="postgresql://x", data_dir=tmp_path, web_dist=tmp_path, **overrides
    )
    monkeypatch.setattr(oss_server, "OssRuntime", _FakeRuntime)
    return TestClient(oss_server.create_app(settings))


def test_forwarder_injects_signed_context_and_drops_spoofed_headers(monkeypatch, tmp_path) -> None:
    client = _shell(monkeypatch, tmp_path)
    response = client.get(
        "/v1/analytics/projects",
        headers={
            "X-KnowFlow-Actor-Id": "attacker",
            "X-KnowFlow-Service-Token": "guess",
            "X-KnowFlow-Project-Id": "prj_oss_1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "token": "s" * 48,
        "actor": OSS_ACTOR_ID,
        "scope": OSS_SCOPE_HASH,
        "project": "prj_oss_1",
    }


def test_password_guard_protects_core_and_settings_but_not_status(monkeypatch, tmp_path) -> None:
    client = _shell(monkeypatch, tmp_path, access_password="open-sesame")
    assert client.get("/api/oss/status").status_code == 200
    assert client.get("/api/oss/status").json()["login_required"] is True
    assert client.get("/v1/analytics/projects").status_code == 401
    assert client.get("/api/oss/settings").status_code == 401
    ok = client.get("/v1/analytics/projects", headers={"Authorization": "Bearer open-sesame"})
    assert ok.status_code == 200


def test_core_unavailable_returns_503_with_reason(monkeypatch, tmp_path) -> None:
    client = _shell(monkeypatch, tmp_path)
    runtime = client.app.state.runtime
    runtime.core = None
    runtime.core_error = "not_configured"
    response = client.get("/v1/analytics/projects")
    assert response.status_code == 503
    assert response.json() == {"detail": "not_configured"}


def test_shell_serves_spa_bundle_with_fallback(monkeypatch, tmp_path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    settings = OssSettings(catalog_database_url="postgresql://x", data_dir=tmp_path, web_dist=dist)
    monkeypatch.setattr(oss_server, "OssRuntime", _FakeRuntime)
    client = TestClient(oss_server.create_app(settings))
    assert client.get("/").text.startswith("<!doctype html>")
    assert client.get("/projects/prj_oss_1/ask").text.startswith("<!doctype html>")
    assert client.get("/assets/app.js").text == "console.log(1)"
    # Paths outside dist never resolve to files.
    assert client.get("/../pyproject.toml").text.startswith("<!doctype html>")


def test_column_profiler_flag_does_not_shadow_sampling_method() -> None:
    """Regression: the ctor stored the flag under the method's name, so every
    profile_table call raised ``'bool' object is not callable``."""
    from sqlalchemy import create_engine

    from knowflow_analytics.modeling.profile import PostgreSqlColumnProfiler

    profiler = PostgreSqlColumnProfiler(
        create_engine("postgresql+psycopg://u:p@h/d"), sample_values=True
    )
    assert callable(profiler._sample_values)
    assert profiler._sample_values_enabled is True


def test_datasource_may_not_be_the_catalog_database() -> None:
    from knowflow_analytics.oss.runtime import probe_datasource

    catalog = "postgresql+psycopg://u:p@127.0.0.1:5456/analytics_catalog"
    with pytest.raises(ValueError, match="catalog"):
        probe_datasource("postgresql://u:p@127.0.0.1:5456/analytics_catalog", catalog_url=catalog)


def test_bare_postgresql_urls_are_pinned_to_psycopg3() -> None:
    from knowflow_analytics.oss.config import normalize_postgres_url

    assert normalize_postgres_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalize_postgres_url("postgres://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalize_postgres_url("postgresql+psycopg://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    config = OssConfig(datasource_database_url=SecretStr("postgresql://u:p@h/d"))
    assert config.datasource_database_url.get_secret_value().startswith("postgresql+psycopg://")


def test_psycopg2_url_is_rejected_with_the_supported_driver_format() -> None:
    from knowflow_analytics.oss.runtime import probe_datasource

    with pytest.raises(ValueError, match="psycopg 3"):
        probe_datasource("postgresql+psycopg2://u:p@h/d")


def test_model_gateway_strips_reasoning_and_puts_schema_in_system_prompt() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '<think>let me see</think>\n{"ok": true}'}}]},
        )

    gateway = OpenAiCompatibleModelGateway(
        ModelEndpoint(base_url="http://llm/v1", model="m"), client=_chat_client(handler)
    )
    assert gateway.probe() == "m"
    system = seen[0]["messages"][0]
    assert system["role"] == "system"
    assert "JSON Schema" in system["content"] and '"ok"' in system["content"]
    assert seen[0]["messages"][-1]["role"] == "user"


def test_model_gateway_rejects_output_that_violates_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": "yes"}'}}]})

    gateway = OpenAiCompatibleModelGateway(
        ModelEndpoint(base_url="http://llm/v1", model="m"), client=_chat_client(handler)
    )
    with pytest.raises(ModelGatewayError) as exc:
        gateway.probe()
    assert exc.value.code == "MODEL_OUTPUT_INVALID"
