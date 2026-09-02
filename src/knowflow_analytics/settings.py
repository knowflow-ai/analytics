from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KNOWFLOW_ANALYTICS_",
        env_file=None,
        extra="ignore",
    )

    catalog_database_url: SecretStr
    datasource_database_url: SecretStr
    service_secret: SecretStr = Field(min_length=32)
    ragflow_base_url: str
    ragflow_service_token: SecretStr = Field(min_length=16)
    # The signed-in actor carries the tenant on every request, so this is only a
    # fallback for callers with no request context.
    # Empty delegates the choice to the tenant's default chat model, which is the
    # model RAGFlow's global settings present to the user. Pin a value only to
    # deliberately run analytics on a different model from the product default.
    llm_id: str = Field(default="", max_length=256)
    # Empty delegates to the tenant's default embedding model, matching llm_id.
    embedding_id: str = Field(default="", max_length=256)
    # Governed defaults: both LLM correctors are opt-in.
    s2sql_corrector_enabled: bool = False
    # 自洽投票次数。1 = 单次生成(上游默认,线上不加开销);调大后同一问题独立生成
    # 多次取多数,压 LLM 形态漂移,代价是每次问数的模型调用数 xN。
    self_consistency_number: int = 1
    physical_sql_corrector_enabled: bool = False
    # Multi-turn rewrite is opt-in by default.
    multi_turn_enabled: bool = False
    # Weak metric 候选先交给受限 AI 业务裁决；失败仍回退人工确认。
    weak_metric_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    # V2 cross-type / multi-phrase semantic adjudication has its own rollout gate.
    semantic_intent_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    # 无指标锚点的多事实粒度单独灰度，便于只回滚业务对象自动选择。
    analysis_object_adjudication_mode: Literal["off", "shadow", "auto"] = "shadow"
    confirmation_memory_ttl_seconds: int = Field(
        default=2_592_000,
        ge=60,
        le=31_536_000,
    )
    auto_create_schema: bool = False
    allow_debug_sql: bool = False
    dry_run_before_execute: bool = False
    # One governed modeling or S2SQL call against a large model can exceed a
    # minute, and the gateway's read timeout aborts the whole run when it does.
    model_gateway_timeout_seconds: float = Field(default=60.0, ge=1.0, le=600.0)
    # One-click modeling fans out per table and per business entity. Free model
    # tiers reject that burst (Groq 429 on tokens-per-minute, Gemini 503) while
    # answering the same calls serially, so the fan-out has to match the
    # provider's quota. Set to 1 for a strictly serial run.
    modeling_max_concurrency: int = Field(default=5, ge=1, le=16)
    # 建模画像里是否把低基数列的实际取值送给模型。统计量（基数/NULL 率/值域）
    # 不含原始值，永远给；实际值对命名质量帮助很大，但会离开内网。
    modeling_sample_values: bool = True
    request_body_limit_bytes: int = Field(default=1_048_576, ge=1_024, le=10_485_760)
    requests_per_minute: int = Field(default=120, ge=1, le=10_000)
    # 一次报表看板加载 = 一张卡一次 structured-query。上游产品允许一块看板挂 50
    # 张卡，而这里原来是 10——满编看板永远加载不完，且失败面目全非（FastAPI 的
    # 429 体是 `{"detail": ...}`，不带 error.code，调用方只能报"请求失败"）。
    # 60 与 OSS runtime 一致，可放行一整块看板并留出提问余量。
    expensive_requests_per_minute: int = Field(default=60, ge=1, le=1_000)
    minimum_evaluation_cases: int = Field(default=30, ge=1, le=10_000)
    minimum_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_urls(self) -> AnalyticsSettings:
        for label, value in (
            ("catalog_database_url", self.catalog_database_url.get_secret_value()),
            ("datasource_database_url", self.datasource_database_url.get_secret_value()),
        ):
            if not value.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError(f"{label} must use PostgreSQL")
        if not self.ragflow_base_url.startswith(("http://", "https://")):
            raise ValueError("ragflow_base_url must be HTTP(S)")
        return self
