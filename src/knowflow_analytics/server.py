from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import create_engine

from knowflow_analytics.api import create_api
from knowflow_analytics.application import AnalyticsApplication
from knowflow_analytics.catalog.data_sources import DataSourceRegistry
from knowflow_analytics.catalog.secrets import DataSourceSecretBox
from knowflow_analytics.catalog.store import CatalogStore
from knowflow_analytics.gateways.embedding import HttpEmbeddingGateway
from knowflow_analytics.gateways.knowledge import HttpKnowledgeGateway
from knowflow_analytics.gateways.model import HttpModelGateway
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.dimension_aliases import DimensionValueAliasSuggester
from knowflow_analytics.query.corrector import LlmPhysicalSqlCorrector, LlmSqlCorrector
from knowflow_analytics.query.exemplars import GoldenSuiteExemplarProvider
from knowflow_analytics.query.interpret import ResultInterpreter
from knowflow_analytics.query.multi_turn import MultiTurnRewriter
from knowflow_analytics.query.parser import LlmS2SqlParser, TextualS2SqlCorrector
from knowflow_analytics.settings import AnalyticsSettings


def create_app() -> FastAPI:
    settings = AnalyticsSettings()
    service_secret = settings.service_secret.get_secret_value()
    catalog_engine = create_engine(settings.catalog_database_url.get_secret_value())
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
    # 一次性迁移：数据源实体之前建的项目没有绑定行，而 for_project 不回落。
    # 幂等；没有存量项目要迁就什么都不建。
    data_sources.migrate_legacy_projects()
    # 连库的组件全部由 data_sources 按项目解析，这里一个都不传。
    #
    # 曾经在这里按 datasource_database_url 建过一整套引擎/执行器/画像器传进去，
    # 但解析器优先，它们**一个都没被用过**——白建一个连接池，还让人误以为存在
    # 一个"默认数据源"。
    application = AnalyticsApplication(
        catalog=catalog,
        data_sources=data_sources,
        catalog_database_url=settings.catalog_database_url.get_secret_value(),
        embedding_gateway=embedding_gateway,
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
        result_interpreter=ResultInterpreter(
            model_gateway,
            enabled=settings.result_interpretation_enabled,
        ),
        minimum_evaluation_cases=settings.minimum_evaluation_cases,
        minimum_accuracy=settings.minimum_accuracy,
        dry_run_before_execute=settings.dry_run_before_execute,
        modeling_max_concurrency=settings.modeling_max_concurrency,
        selection_secret=service_secret,
    )
    api = create_api(
        application=application,
        service_secret=service_secret,
        allow_debug_sql=settings.allow_debug_sql,
        request_body_limit_bytes=settings.request_body_limit_bytes,
        requests_per_minute=settings.requests_per_minute,
        query_defaults={
            "self_consistency_number": settings.self_consistency_number,
            "s2sql_corrector_enabled": settings.s2sql_corrector_enabled,
            "physical_sql_corrector_enabled": settings.physical_sql_corrector_enabled,
            "multi_turn_enabled": settings.multi_turn_enabled,
            "dry_run_before_execute": settings.dry_run_before_execute,
        },
        expensive_requests_per_minute=settings.expensive_requests_per_minute,
    )

    @api.on_event("shutdown")
    def shutdown() -> None:
        application.close()
        knowledge_gateway.close()
        model_gateway.close()
        embedding_gateway.close()
        data_sources.close()
        catalog_engine.dispose()

    return api
