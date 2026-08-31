"""HTTP shell for the open-source edition.

Responsibilities, in request order:

1. optional shared-password check for everything under ``/api`` and ``/v1``;
2. settings endpoints under ``/api/oss`` (datasource + models, with probes);
3. forwarding ``/v1/analytics/*`` to the shared core with the internal
   signed-context headers the core expects, so the core's auth contract stays
   exactly what the commercial BFF uses;
4. serving the bundled single-page web UI for everything else.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from knowflow_analytics.errors import AnalyticsError
from knowflow_analytics.oss.config import ModelEndpoint, OssConfig, unmask_url
from knowflow_analytics.oss.gateways import (
    OpenAiCompatibleEmbeddingGateway,
    OpenAiCompatibleModelGateway,
)
from knowflow_analytics.oss.runtime import (
    OSS_ACTOR_ID,
    OSS_PROJECT_ID_PREFIX,
    OSS_SCOPE_HASH,
    OssRuntime,
    OssSettings,
    probe_datasource,
)

LOGGER = logging.getLogger(__name__)

_PROTECTED_PREFIXES = ("/api/", "/v1/")


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def _settings_router(runtime: OssRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/oss")

    @router.get("/status")
    def status() -> dict[str, Any]:
        return {
            "ready": runtime.core is not None,
            "error": runtime.core_error,
            "login_required": bool(runtime.settings.access_password),
            "project_id_prefix": OSS_PROJECT_ID_PREFIX,
        }

    @router.get("/settings")
    def get_settings() -> dict[str, Any]:
        return runtime.config.public_view()

    @router.put("/settings")
    def put_settings(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            incoming = OssConfig.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc)) from exc
        try:
            merged = runtime.update_config(incoming)
        except Exception as exc:  # noqa: BLE001 - unreachable DB etc. is a user-facing 400
            raise HTTPException(status_code=400, detail=_safe_message(exc)) from exc
        return {
            **merged.public_view(),
            "ready": runtime.core is not None,
            "error": runtime.core_error,
        }

    @router.post("/settings/test-datasource")
    def test_datasource(payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("datasource_database_url") or "")
        try:
            url = unmask_url(url, runtime.config.datasource_database_url.get_secret_value())
            if not url:
                raise ValueError("请填写数据库连接串")
            probe_datasource(url, catalog_url=runtime.settings.catalog_database_url)
        except Exception as exc:  # noqa: BLE001 - message shown on the settings page
            raise HTTPException(status_code=400, detail=_safe_message(exc)) from exc
        return {"ok": True}

    @router.post("/settings/test-model")
    def test_model(payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        if kind not in {"chat_model", "embedding_model"}:
            raise HTTPException(
                status_code=422, detail="kind must be chat_model or embedding_model"
            )
        try:
            endpoint = ModelEndpoint.model_validate(payload.get("endpoint") or {})
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_message(exc)) from exc
        stored = getattr(runtime.config, kind)
        if endpoint.api_key.get_secret_value() == "********":
            if endpoint.base_url != stored.base_url:
                raise HTTPException(status_code=400, detail="更换 Base URL 后请重新填写 API Key")
            endpoint = endpoint.model_copy(update={"api_key": stored.api_key})
        if not endpoint.is_configured():
            raise HTTPException(status_code=422, detail="base_url and model are required")
        try:
            if kind == "chat_model":
                gateway = OpenAiCompatibleModelGateway(
                    endpoint, timeout_seconds=runtime.settings.model_timeout_seconds
                )
                try:
                    return {"ok": True, "model": gateway.probe()}
                finally:
                    gateway.close()
            embedding = OpenAiCompatibleEmbeddingGateway(endpoint)
            try:
                return {"ok": True, "dimension": embedding.probe()}
            finally:
                embedding.close()
        except AnalyticsError as exc:
            raise HTTPException(status_code=400, detail=_safe_message(exc)) from exc

    return router


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors(include_url=False, include_context=False)[:1]
    if not first:
        return "请求参数无效"
    location = ".".join(str(part) for part in first[0].get("loc", ()))
    message = str(first[0].get("msg", "invalid")).removeprefix("Value error, ")
    return f"{location}: {message}" if location else message


def _safe_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:500]


class _CoreForwarder:
    """Pure-ASGI middleware: hands ``/v1/analytics/*`` to the core with signed headers."""

    def __init__(self, app: Any, runtime: OssRuntime) -> None:
        self._app = app
        self._runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/v1/analytics"):
            await self._app(scope, receive, send)
            return
        core = self._runtime.core
        if core is None:
            response = JSONResponse(
                status_code=503,
                content={"detail": self._runtime.core_error or "not_configured"},
            )
            await response(scope, receive, send)
            return
        drop = {
            b"x-knowflow-service-token",
            b"x-knowflow-actor-id",
            b"x-knowflow-permission-scope-hash",
        }
        headers = [(key, value) for key, value in scope["headers"] if key.lower() not in drop]
        headers.extend(
            [
                (b"x-knowflow-service-token", self._runtime.service_secret.encode()),
                (b"x-knowflow-actor-id", OSS_ACTOR_ID.encode()),
                (b"x-knowflow-permission-scope-hash", OSS_SCOPE_HASH.encode()),
            ]
        )
        await core.api({**scope, "headers": headers}, receive, send)


def _spa(app: FastAPI, dist: Path | None) -> None:
    if dist is None or not (dist / "index.html").exists():
        LOGGER.warning("oss web bundle not found at %s; only the API is served", dist)
        return
    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if "\0" in path:
            return FileResponse(index)
        candidate = (dist / path).resolve() if path else index
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


def _default_web_dist() -> Path:
    # src/knowflow_analytics/oss/server.py -> knowflow-analytics/web/dist
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def create_app(settings: OssSettings | None = None) -> FastAPI:
    settings = settings or OssSettings()
    runtime = OssRuntime(settings)
    app = FastAPI(title="KnowFlow Analytics OSS", docs_url=None, redoc_url=None, openapi_url=None)
    # Starlette wraps middleware in reverse registration order, so registering
    # the forwarder first keeps the password guard outermost.
    app.add_middleware(_CoreForwarder, runtime=runtime)
    app.state.runtime = runtime

    @app.middleware("http")
    async def access_guard(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        password = settings.access_password
        path = request.url.path
        protected = path.startswith(_PROTECTED_PREFIXES) and path != "/api/oss/status"
        if (
            password
            and protected
            and not secrets.compare_digest(
                _bearer(request).encode("utf-8", "surrogateescape"), password.encode("utf-8")
            )
        ):
            return JSONResponse(status_code=401, content={"detail": "login_required"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.include_router(_settings_router(runtime))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    _spa(app, settings.web_dist or _default_web_dist())

    @app.on_event("shutdown")
    def shutdown() -> None:
        runtime.close()

    return app


def main() -> None:
    import uvicorn

    settings = OssSettings()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
