from knowflow_analytics.settings import AnalyticsSettings


def _settings(**overrides):
    values = {
        "catalog_database_url": "postgresql://user:pass@localhost/catalog",
        "datasource_database_url": "postgresql://user:pass@localhost/source",
        "service_secret": "s" * 32,
        "ragflow_base_url": "http://127.0.0.1:9380",
        "ragflow_service_token": "t" * 16,
        "llm_id": "chat-model",
        "embedding_id": "embedding-model",
    }
    values.update(overrides)
    return AnalyticsSettings(**values)


def test_llm_correctors_are_disabled_by_default():
    settings = _settings()

    assert settings.s2sql_corrector_enabled is False
    assert settings.physical_sql_corrector_enabled is False


def test_llm_correctors_can_be_enabled_independently():
    settings = _settings(s2sql_corrector_enabled=True)

    assert settings.s2sql_corrector_enabled is True
    assert settings.physical_sql_corrector_enabled is False


def test_model_gateway_timeout_is_configurable():
    """A 120B model routinely spends more than a minute on one modeling call, so
    the gateway read timeout has to be a deployment setting rather than a
    hard-coded 60 seconds that silently fails the whole modeling run."""

    assert _settings().model_gateway_timeout_seconds == 60.0
    assert _settings(model_gateway_timeout_seconds=300).model_gateway_timeout_seconds == 300.0


def test_llm_id_is_optional_so_the_tenant_default_model_is_used():
    """Leaving LLM_ID unset delegates model choice to RAGFlow's global default,
    which is what the modeling page presents to the user as "默认模型"."""

    values = {
        "catalog_database_url": "postgresql://user:pass@localhost/catalog",
        "datasource_database_url": "postgresql://user:pass@localhost/source",
        "service_secret": "s" * 32,
        "ragflow_base_url": "http://127.0.0.1:9380",
        "ragflow_service_token": "t" * 16,
        "embedding_id": "embedding-model",
    }

    assert AnalyticsSettings(**values).llm_id == ""
    assert AnalyticsSettings(**values, llm_id="pinned-model").llm_id == "pinned-model"


def test_modeling_concurrency_is_configurable():
    """One-click modeling fans out per table and per business entity. Free model
    tiers reject that burst (Groq 429, Gemini 503) while answering the same calls
    serially, so a deployment must be able to match its provider's quota."""

    assert _settings().modeling_max_concurrency == 5
    assert _settings(modeling_max_concurrency=1).modeling_max_concurrency == 1


def test_weak_metric_adjudication_defaults_to_shadow_and_supports_rollout_modes():
    assert _settings().weak_metric_adjudication_mode == "shadow"
    assert _settings(weak_metric_adjudication_mode="shadow").weak_metric_adjudication_mode == (
        "shadow"
    )
    assert _settings(weak_metric_adjudication_mode="off").weak_metric_adjudication_mode == "off"


def test_analysis_object_adjudication_has_an_independent_shadow_rollout():
    assert _settings().analysis_object_adjudication_mode == "shadow"
    assert (
        _settings(analysis_object_adjudication_mode="auto").analysis_object_adjudication_mode
        == "auto"
    )


def test_confirmation_memory_ttl_is_bounded_and_configurable():
    assert _settings().confirmation_memory_ttl_seconds == 2_592_000
    assert _settings(confirmation_memory_ttl_seconds=3600).confirmation_memory_ttl_seconds == 3600
    assert (
        _settings(analysis_object_adjudication_mode="off").analysis_object_adjudication_mode
        == "off"
    )


def test_semantic_intent_adjudication_does_not_inherit_the_legacy_auto_gate():
    settings = _settings(weak_metric_adjudication_mode="auto")
    assert settings.weak_metric_adjudication_mode == "auto"
    assert settings.semantic_intent_adjudication_mode == "shadow"
    assert (
        _settings(semantic_intent_adjudication_mode="auto").semantic_intent_adjudication_mode
        == "auto"
    )
