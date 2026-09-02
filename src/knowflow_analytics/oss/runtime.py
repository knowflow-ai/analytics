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
from typing import Literal

from fastapi import FastAPI
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester
from knowflow_analytics.modeling.introspector import PostgreSqlIntrospector
from knowflow_analytics.modeling.profile import PostgreSqlColumnProfiler
from knowflow_analytics.modeling.profiler import PostgreSqlSemanticProfiler
from knowflow_analytics.modeling.quality import PostgreSqlModelingQualityProfiler
from knowflow_analytics.oss.config import ConfigStore, OssConfig, normalize_postgres_url
from knowflow_analytics.oss.gateways import (
    OpenAiCompatibleEmbeddingGateway,
    OpenAiCompatibleModelGateway,
)
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector, LlmSqlCorrector
from knowflow_analytics.query.exemplars import GoldenSuiteExemplarProvider
from knowflow_analytics.query.intent_adjudicator import LlmIntentAdjudicator
from knowflow_analytics.query.multi_turn import MultiTurnRewriter
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector
from knowflow_analytics.query.weak_metric_adjudicator import (
    LlmWeakMetricAdjudicator,
)

LOGGER = logging.getLogger(__name__)

# Fixed identity for the single local user; the core only uses these for
# ownership stamps and rate-limit buckets.
OSS_ACTOR_ID = "local"
OSS_SCOPE_HASH = "oss-single-user"
OSS_PROJECT_ID_PREFIX = "prj_oss_"
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
    weak_metric_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    semantic_intent_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    analysis_object_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    confirmation_memory_ttl_seconds: int = Field(
        default=2_592_000,
        ge=60,
        le=31_536_000,
    )
    # 自洽投票次数。1 = 单次生成(上游默认);调大后同一问题独立生成多次取多数,
    # 压 LLM 形态漂移,代价是每次问数的模型调用数 xN。
    self_consistency_number: int = Field(default=1, ge=1, le=8)
    allow_debug_sql: bool = True


@dataclass(frozen=True)
class CoreBundle:
    api: FastAPI
    application: AnalyticsApplication
    datasource_engine: Engine
    executor: SqlExecutor
    model_gateway: OpenAiCompatibleModelGateway
    embedding_gateway: OpenAiCompatibleEmbeddingGateway

    def close(self) -> None:
        self.application.close()
        self.model_gateway.close()
        self.embedding_gateway.close()
        self.executor.close()
        self.datasource_engine.dispose()


def _database_identity(url: str) -> tuple[str, str, str]:
    """(host, port, database) so two spellings of one database compare equal."""

    parsed = make_url(url)
    return (parsed.host or "localhost", str(parsed.port or 5432), parsed.database or "")


def probe_datasource(url: str, *, catalog_url: str | None = None) -> None:
    url = normalize_postgres_url(url)
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError(
            "仅支持 psycopg 3 PostgreSQL 连接串：postgresql+psycopg://user:password@host:5432/database；"
            "postgresql:// 与 postgres:// 会自动转换"
        )
    if catalog_url and _database_identity(url) == _database_identity(catalog_url):
        raise ValueError(
            "数据源不能与服务自己的 catalog 库是同一个数据库：catalog 里的 analytics_* "
            "内部表会被当成业务表建模。请把业务库作为数据源，或给 catalog 单独建一个库。"
        )
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


class OssRuntime:
    def __init__(self, settings: OssSettings) -> None:
        self.settings = settings
        self.service_secret = secrets.token_urlsafe(48)
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
            datasource = merged.datasource_database_url.get_secret_value()
            if datasource:
                probe_datasource(datasource, catalog_url=self.settings.catalog_database_url)
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
        datasource_url = config.datasource_database_url.get_secret_value()
        datasource_engine = create_engine(datasource_url, pool_pre_ping=True)
        executor = SqlExecutor(datasource_url)
        model_gateway = OpenAiCompatibleModelGateway(
            config.chat_model, timeout_seconds=settings.model_timeout_seconds
        )
        embedding_gateway = OpenAiCompatibleEmbeddingGateway(config.embedding_model)
        try:
            return self._assemble(
                config, datasource_engine, executor, model_gateway, embedding_gateway
            )
        except Exception:
            model_gateway.close()
            embedding_gateway.close()
            executor.close()
            datasource_engine.dispose()
            raise

    def _assemble(
        self,
        config: OssConfig,
        datasource_engine: Engine,
        executor: SqlExecutor,
        model_gateway: OpenAiCompatibleModelGateway,
        embedding_gateway: OpenAiCompatibleEmbeddingGateway,
    ) -> CoreBundle:
        settings = self.settings
        del config  # gateways already carry the endpoint configuration
        application = AnalyticsApplication(
            catalog=self.catalog,
            introspector=PostgreSqlIntrospector(datasource_engine),
            executor=executor,
            embedding_gateway=embedding_gateway,
            semantic_profiler=PostgreSqlSemanticProfiler(datasource_engine),
            column_profiler=PostgreSqlColumnProfiler(
                datasource_engine, sample_values=settings.modeling_sample_values
            ),
            quality_profiler=PostgreSqlModelingQualityProfiler(datasource_engine, executor),
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
            weak_metric_adjudicator=LlmWeakMetricAdjudicator(model_gateway),
            weak_metric_adjudication_mode=settings.weak_metric_adjudication_mode,
            intent_adjudicator=LlmIntentAdjudicator(model_gateway),
            semantic_intent_adjudication_mode=(settings.semantic_intent_adjudication_mode),
            analysis_object_adjudication_mode=(settings.analysis_object_adjudication_mode),
            confirmation_memory_ttl_seconds=settings.confirmation_memory_ttl_seconds,
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
            datasource_engine=datasource_engine,
            executor=executor,
            model_gateway=model_gateway,
            embedding_gateway=embedding_gateway,
        )

    def close(self) -> None:
        with self._lock:
            if self._bundle is not None:
                self._bundle.close()
                self._bundle = None
            self._catalog_engine.dispose()
