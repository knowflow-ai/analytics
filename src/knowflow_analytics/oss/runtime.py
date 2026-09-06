"""Builds the shared analytics core from the open-source settings file.

The catalog database lives for the whole process; everything that depends on
the user-editable settings (datasource engine, model gateways, the core FastAPI
app) is rebuilt as one immutable bundle whenever the settings change.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.data_sources import DataSourceRegistry
from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester
from knowflow_analytics.oss.config import ConfigStore, OssConfig, normalize_postgres_url
from knowflow_analytics.oss.gateways import (
    OpenAiCompatibleEmbeddingGateway,
    OpenAiCompatibleModelGateway,
)
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector, LlmSqlCorrector
from knowflow_analytics.query.exemplars import GoldenSuiteExemplarProvider
from knowflow_analytics.query.multi_turn import MultiTurnRewriter
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector

LOGGER = logging.getLogger(__name__)

# Fixed identity for the single local user; the core only uses these for
# ownership stamps and rate-limit buckets.
OSS_ACTOR_ID = "local"
OSS_SCOPE_HASH = "oss-single-user"
OSS_PROJECT_ID_PREFIX = "prj_oss_"
_SERVICE_SECRET_FILE = "service_secret"
_RETIRE_GRACE_SECONDS = 600.0


class OssSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KNOWFLOW_OSS_", env_file=None, extra="ignore")

    catalog_database_url: str
    data_dir: Path = Path("./data")
    web_dist: Path | None = None
    # Optional shared password for the web UI. Empty means no login screen.
    access_password: str = Field(default="", max_length=256)
    # Loopback by default; the container image sets 0.0.0.0 explicitly.
    host: str = "127.0.0.1"
    port: int = Field(default=9395, ge=1, le=65535)
    model_timeout_seconds: float = Field(default=120.0, ge=1.0, le=600.0)
    modeling_max_concurrency: int = Field(default=3, ge=1, le=16)
    modeling_sample_values: bool = True
    multi_turn_enabled: bool = False
    # 自洽投票次数。1 = 单次生成(上游默认);调大后同一问题独立生成多次取多数,
    # 压 LLM 形态漂移,代价是每次问数的模型调用数 xN。
    self_consistency_number: int = Field(default=1, ge=1, le=8)
    allow_debug_sql: bool = True


@dataclass(frozen=True)
class CoreBundle:
    api: FastAPI
    application: AnalyticsApplication
    data_sources: DataSourceRegistry
    model_gateway: OpenAiCompatibleModelGateway
    embedding_gateway: OpenAiCompatibleEmbeddingGateway

    def close(self) -> None:
        self.application.close()
        self.model_gateway.close()
        self.embedding_gateway.close()
        self.data_sources.close()


def load_service_secret(data_dir: Path) -> str:
    """服务密钥落在数据目录里，重启不变。

    此前每次启动随机生成。多数据源的连接串是用它派生的密钥加密后存在 catalog 里的，
    密钥一换全部解不开——重启一次，所有数据源都得重填。澄清/下钻的签名 token 同样
    绑着它。文件 0600，与 config.json 同一处、同一权限。
    """

    path = data_dir / _SERVICE_SECRET_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= 32:
        return existing
    secret = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    return secret


class OssRuntime:
    def __init__(self, settings: OssSettings) -> None:
        self.settings = settings
        self.service_secret = load_service_secret(settings.data_dir)
        self._store = ConfigStore(settings.data_dir)
        self._config = self._store.load()
        self._catalog_engine = create_engine(normalize_postgres_url(settings.catalog_database_url))
        self.catalog = CatalogStore(self._catalog_engine)
        self.catalog.create_schema()
        self._lock = threading.Lock()
        self._bundle: CoreBundle | None = None
        self._bundle_error: str | None = None
        self._rebuild()

    @property
    def config(self) -> OssConfig:
        return self._config

    @property
    def core(self) -> CoreBundle | None:
        return self._bundle

    @property
    def core_error(self) -> str | None:
        return self._bundle_error

    def update_config(self, incoming: OssConfig) -> OssConfig:
        with self._lock:
            merged = self._config.merged_with(incoming)
            self._store.save(merged)
            self._config = merged
            self._rebuild()
            return merged

    def _rebuild(self) -> None:
        previous, self._bundle, self._bundle_error = self._bundle, None, None
        if previous is not None:
            # Requests that captured the old bundle may still be running (a
            # modeling job can take minutes); retire it after a grace period.
            timer = threading.Timer(_RETIRE_GRACE_SECONDS, previous.close)
            timer.daemon = True
            timer.start()
        if not self._config.is_complete():
            self._bundle_error = "not_configured"
            return
        try:
            self._bundle = self._build(self._config)
        except Exception as exc:  # noqa: BLE001 - surfaced to the settings page
            LOGGER.exception("oss runtime failed to build the analytics core")
            self._bundle_error = str(exc)[:500] or exc.__class__.__name__

    def _build(self, config: OssConfig) -> CoreBundle:
        settings = self.settings
        # 与商业版同一套装配：连库组件按项目绑定的数据源解析（多数据源、上传表格
        # 都走这条）。数据源只在「数据库连接」里添加，设置页不再有"那一个库"。
        data_sources = DataSourceRegistry(
            catalog=self.catalog,
            secret_box=DataSourceSecretBox(self.service_secret),
            default_database_url="",
            catalog_database_url=normalize_postgres_url(settings.catalog_database_url),
            modeling_sample_values=settings.modeling_sample_values,
        )
        model_gateway = OpenAiCompatibleModelGateway(
            config.chat_model, timeout_seconds=settings.model_timeout_seconds
        )
        embedding_gateway = OpenAiCompatibleEmbeddingGateway(config.embedding_model)
        try:
            return self._assemble(config, data_sources, model_gateway, embedding_gateway)
        except Exception:
            model_gateway.close()
            embedding_gateway.close()
            data_sources.close()
            raise

    def _assemble(
        self,
        config: OssConfig,
        data_sources: DataSourceRegistry,
        model_gateway: OpenAiCompatibleModelGateway,
        embedding_gateway: OpenAiCompatibleEmbeddingGateway,
    ) -> CoreBundle:
        settings = self.settings
        del config  # gateways already carry the endpoint configuration
        application = AnalyticsApplication(
            catalog=self.catalog,
            data_sources=data_sources,
            # 上传的表格落在 catalog 同一个 PostgreSQL 实例的独立库里。
            catalog_database_url=normalize_postgres_url(settings.catalog_database_url),
            embedding_gateway=embedding_gateway,
            ai_modeller=AiSemanticModeller(
                model_gateway=model_gateway,
                max_concurrency=settings.modeling_max_concurrency,
            ),
            dimension_alias_suggester=DimensionValueAliasSuggester(
                model_gateway, max_concurrency=settings.modeling_max_concurrency
            ),
            llm_parser=LlmS2SqlParser(
                model_gateway,
                exemplar_provider=GoldenSuiteExemplarProvider(
                    catalog=self.catalog, embedding_gateway=embedding_gateway
                ),
                self_consistency_number=self.settings.self_consistency_number,
            ),
            textual_corrector=TextualS2SqlCorrector(
                llm_sql_corrector=LlmSqlCorrector(model_gateway, enabled=False)
            ),
            physical_sql_corrector=LlmPhysicalSqlCorrector(model_gateway, enabled=False),
            multi_turn_rewriter=MultiTurnRewriter(
                model_gateway, enabled=settings.multi_turn_enabled
            ),
            # Default only: the standalone edition publishes on structural
            # validation alone. The Golden-suite and quality-report endpoints are
            # mounted here too — what differs is whether passing them is required.
            require_evaluation_for_publish=False,
            require_quality_report_for_publish=False,
            modeling_max_concurrency=settings.modeling_max_concurrency,
            selection_secret=self.service_secret,
        )
        api = create_api(
            application=application,
            service_secret=self.service_secret,
            allow_debug_sql=settings.allow_debug_sql,
            requests_per_minute=600,
            expensive_requests_per_minute=60,
        )
        return CoreBundle(
            api=api,
            application=application,
            data_sources=data_sources,
            model_gateway=model_gateway,
            embedding_gateway=embedding_gateway,
        )

    def close(self) -> None:
        with self._lock:
            if self._bundle is not None:
                self._bundle.close()
                self._bundle = None
            self._catalog_engine.dispose()
