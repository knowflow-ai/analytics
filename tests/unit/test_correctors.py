from __future__ import annotations

from knowflow_analytics.contracts import PhysicalQuery
from knowflow_analytics.query.contracts import (
    MapMode,
    MappingResult,
    ParsedSemanticCandidate,
)
from knowflow_analytics.query.corrector import (
    LlmPhysicalSqlCorrector,
    LlmSqlCorrector,
)


class _Gateway:
    def __init__(self, payload: dict | None = None, *, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[dict] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.payload


def _candidate() -> ParsedSemanticCandidate:
    sql = 'SELECT SUM("净收入") FROM "销售经营"'
    return ParsedSemanticCandidate(
        id="candidate-corrector",
        dataset_id="sales_dataset",
        parsed_s2sql=sql,
        corrected_s2sql=sql,
        query_type="aggregate",
        score=1.0,
        map_mode=MapMode.ALL,
        mapping=MappingResult(
            dataset_id="sales_dataset",
            mode=MapMode.ALL,
            normalized_question="净收入平均值",
            matches=(),
            config_version="test",
        ),
        parser="llm",
    )


def _physical_query(sales_release) -> PhysicalQuery:
    return PhysicalQuery(
        release_id=sales_release.id,
        dataset_id="sales_dataset",
        sql='SELECT SUM("net_amount") FROM "orders"',
        parameters={},
        columns=(),
    )


def test_llm_sql_corrector_is_disabled_by_default(sales_release):
    gateway = _Gateway({"opinion": "negative", "sql": 'SELECT AVG("净收入") FROM "销售经营"'})
    corrector = LlmSqlCorrector(gateway)

    corrected = corrector.correct(
        candidate=_candidate(),
        question="净收入平均值",
        release=sales_release,
        query_id="q-disabled",
    )

    assert corrected.corrected_s2sql == _candidate().corrected_s2sql
    assert gateway.calls == []
    assert corrector.enabled is False


def test_enabled_llm_sql_corrector_uses_question_schema_and_corrected_s2sql(sales_release):
    gateway = _Gateway({"opinion": "NEGATIVE", "sql": 'SELECT AVG("净收入") FROM "销售经营"'})
    corrector = LlmSqlCorrector(gateway, enabled=True)

    corrected = corrector.correct(
        candidate=_candidate(),
        question="净收入平均值",
        release=sales_release,
        query_id="q-s2sql",
    )

    assert corrected.parsed_s2sql == _candidate().parsed_s2sql
    assert corrected.corrected_s2sql == 'SELECT AVG("净收入") FROM "销售经营"'
    assert gateway.calls[0]["purpose"] == "analytics.s2sql.corrector"
    prompt = gateway.calls[0]["messages"][0]["content"]
    assert "净收入平均值" in prompt
    assert "销售经营" in prompt
    assert "净收入" in prompt
    assert _candidate().corrected_s2sql in prompt


def test_llm_sql_corrector_failure_keeps_original_s2sql(sales_release):
    corrector = LlmSqlCorrector(
        _Gateway(error=RuntimeError("model unavailable")),
        enabled=True,
    )

    corrected = corrector.correct(
        candidate=_candidate(),
        question="净收入平均值",
        release=sales_release,
        query_id="q-fail-open",
    )

    assert corrected.corrected_s2sql == _candidate().corrected_s2sql


def test_llm_sql_corrector_schema_follows_published_business_name_changes(sales_release):
    renamed = sales_release.model_copy(
        update={
            "datasets": (sales_release.datasets[0].model_copy(update={"name": "经营主题甲"}),),
            "metrics": tuple(
                metric.model_copy(update={"name": "业务指标甲"})
                if metric.id == "net_revenue"
                else metric
                for metric in sales_release.metrics
            ),
        }
    )
    gateway = _Gateway({"opinion": "positive", "sql": ""})
    corrector = LlmSqlCorrector(gateway, enabled=True)

    corrector.correct(
        candidate=_candidate(),
        question="查询指标",
        release=renamed,
        query_id="q-rename-invariant",
    )

    prompt = gateway.calls[0]["messages"][0]["content"]
    assert "经营主题甲" in prompt
    assert "业务指标甲" in prompt


def test_llm_physical_sql_corrector_is_disabled_by_default(sales_release):
    gateway = _Gateway({"opinion": "negative", "sql": 'SELECT SUM("net_amount") FROM "orders"'})
    corrector = LlmPhysicalSqlCorrector(gateway)
    original = _physical_query(sales_release)

    corrected = corrector.correct(
        question="净收入",
        query=original,
        release=sales_release,
        query_id="q-physical-disabled",
    )

    assert corrected is original
    assert gateway.calls == []
    assert corrector.enabled is False


def test_enabled_llm_physical_sql_corrector_replaces_only_sql_text(sales_release):
    optimized_sql = 'SELECT SUM("net_amount") FROM "orders" /* optimized */'
    gateway = _Gateway({"opinion": "negative", "sql": optimized_sql})
    corrector = LlmPhysicalSqlCorrector(gateway, enabled=True)
    original = _physical_query(sales_release)

    corrected = corrector.correct(
        question="净收入",
        query=original,
        release=sales_release,
        query_id="q-physical",
    )

    assert corrected.sql == optimized_sql
    assert corrected.model_copy(update={"sql": original.sql}) == original
    assert gateway.calls[0]["purpose"] == "analytics.physical_sql.corrector"
    prompt = gateway.calls[0]["messages"][0]["content"]
    assert "净收入" in prompt
    assert original.sql in prompt


def test_llm_physical_sql_corrector_failure_keeps_original_query(sales_release):
    corrector = LlmPhysicalSqlCorrector(
        _Gateway(error=RuntimeError("model unavailable")),
        enabled=True,
    )
    original = _physical_query(sales_release)

    corrected = corrector.correct(
        question="净收入",
        query=original,
        release=sales_release,
        query_id="q-physical-fail-open",
    )

    assert corrected is original
