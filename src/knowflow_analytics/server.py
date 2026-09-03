from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import create_engine

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.data_sources import DataSourceRegistry
from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.execution.executor import SqlExecutor
from knowflow_analytics.gateways.embedding import HttpEmbeddingGateway
from knowflow_analytics.gateways.knowledge import HttpKnowledgeGateway
from knowflow_analytics.gateways.model import HttpModelGateway
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester
from knowflow_analytics.modeling.introspector import SchemaIntrospector
from knowflow_analytics.modeling.profile import ColumnStatisticsProfiler
from knowflow_analytics.modeling.profiler import DimensionValueProfiler
from knowflow_analytics.modeling.quality import ModelingQualityProfiler
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector, LlmSqlCorrector
from knowflow_analytics.query.exemplars import GoldenSuiteExemplarProvider
from knowflow_analytics.query.intent_adjudicator import LlmIntentAdjudicator
from knowflow_analytics.query.multi_turn import MultiTurnRewriter
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector
from knowflow_analytics.query.weak_metric_adjudicator import LlmWeakMetricAdjudicator
from knowflow_analytics.settings import AnalyticsSettings


def create_app() -> FastAPI:
    settings = AnalyticsSettings()
    service_secret = settings.service_secret.get_secret_value()
    catalog_engine = create_engine(settings.catalog_database_url.get_secret_value())
    datasource_engine = create_engine(settings.datasource_database_url.get_secret_value())
    catalog = CatalogStore(catalog_engine)
    if settings.auto_create_schema:
        catalog.create_schema()
    gateway_token = settings.ragflow_service_token.get_secret_value()
    # 租户不做静态装配:每次调用由请求 actor 携带(trace / for_tenant),缺失拒绝。
    embedding_gateway = HttpEmbeddingGateway(
        base_url=settings.ragflow_base_url,
        service_token=gateway_token,
        embedding_id=settings.embedding_id,
    )
    model_gateway = HttpModelGateway(
        base_url=settings.ragflow_base_url,
        service_token=gateway_token,
        llm_id=settings.llm_id,
        timeout_seconds=settings.model_gateway_timeout_seconds,
    )
    knowledge_gateway = HttpKnowledgeGateway(
        base_url=settings.ragflow_base_url,
        service_token=gateway_token,
    )
    executor = SqlExecutor(settings.datasource_database_url.get_secret_value())
    # 数据源解析器。没绑数据源的项目回落到这个进程级默认库——存量项目一个绑定行
    # 都没有，不回落的话这次升级会让它们当场问不了数。
    data_sources = DataSourceRegistry(
        catalog=catalog,
        secret_box=DataSourceSecretBox(settings.service_secret.get_secret_value()),
        default_database_url=settings.datasource_database_url.get_secret_value(),
        modeling_sample_values=settings.modeling_sample_values,
    )
    exemplar_provider = GoldenSuiteExemplarProvider(
        catalog=catalog,
        embedding_gateway=embedding_gateway,
    )
    # 一次性迁移：把部署配置的那个库变成真实数据源，并绑上所有还没绑的项目。
    # 数据源成为实体之前所有项目都连着它，升级后它们没有绑定行，而 for_project
    # 已经不再回落。幂等，每次启动都跑。
    data_sources.ensure_default_data_source()
    application = AnalyticsApplication(
        catalog=catalog,
        data_sources=data_sources,
        introspector=SchemaIntrospector(datasource_engine),
        executor=executor,
        embedding_gateway=embedding_gateway,
        semantic_profiler=DimensionValueProfiler(datasource_engine),
        column_profiler=ColumnStatisticsProfiler(
            datasource_engine, sample_values=settings.modeling_sample_values
        ),
        quality_profiler=ModelingQualityProfiler(datasource_engine, executor),
        ai_modeller=AiSemanticModeller(
            model_gateway=model_gateway,
            knowledge_gateway=knowledge_gateway,
            max_concurrency=settings.modeling_max_concurrency,
        ),
        dimension_alias_suggester=DimensionValueAliasSuggester(
            model_gateway, max_concurrency=settings.modeling_max_concurrency
        ),
        llm_parser=LlmS2SqlParser(
            model_gateway,
            exemplar_provider=exemplar_provider,
            self_consistency_number=settings.self_consistency_number,
        ),
        textual_corrector=TextualS2SqlCorrector(
            llm_sql_corrector=LlmSqlCorrector(
                model_gateway,
                enabled=settings.s2sql_corrector_enabled,
            )
        ),
        physical_sql_corrector=LlmPhysicalSqlCorrector(
            model_gateway,
            enabled=settings.physical_sql_corrector_enabled,
        ),
        multi_turn_rewriter=MultiTurnRewriter(
            model_gateway,
            enabled=settings.multi_turn_enabled,
        ),
        minimum_evaluation_cases=settings.minimum_evaluation_cases,
        minimum_accuracy=settings.minimum_accuracy,
        dry_run_before_execute=settings.dry_run_before_execute,
        modeling_max_concurrency=settings.modeling_max_concurrency,
        selection_secret=service_secret,
        weak_metric_adjudicator=LlmWeakMetricAdjudicator(model_gateway),
        weak_metric_adjudication_mode=settings.weak_metric_adjudication_mode,
        intent_adjudicator=LlmIntentAdjudicator(model_gateway),
        semantic_intent_adjudication_mode=settings.semantic_intent_adjudication_mode,
        analysis_object_adjudication_mode=settings.analysis_object_adjudication_mode,
        confirmation_memory_ttl_seconds=settings.confirmation_memory_ttl_seconds,
    )
    api = create_api(
        application=application,
        service_secret=service_secret,
        allow_debug_sql=settings.allow_debug_sql,
        request_body_limit_bytes=settings.request_body_limit_bytes,
        requests_per_minute=settings.requests_per_minute,
        expensive_requests_per_minute=settings.expensive_requests_per_minute,
    )

    @api.on_event("shutdown")
    def shutdown() -> None:
        application.close()
        knowledge_gateway.close()
        model_gateway.close()
        embedding_gateway.close()
        executor.close()
        datasource_engine.dispose()
        catalog_engine.dispose()

    return api
