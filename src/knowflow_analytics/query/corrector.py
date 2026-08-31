from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowflow_analytics.contracts import PhysicalQuery, SemanticRelease
from knowflow_analytics.gateways.model import StructuredModelGateway
from knowflow_analytics.query.contracts import ParsedSemanticCandidate
from knowflow_analytics.semantic.index import SemanticElementType

LOGGER = logging.getLogger(__name__)

_S2SQL_INSTRUCTION = (
    "#Role: You are a senior data engineer experienced in writing SQL."
    "\n#Task: Your will be provided with a user question and the SQL written by a "
    "junior engineer,please take a review and help correct it if necessary."
    "\n#Rules: "
    "1.ALWAYS specify time range using `>`,`<`,`>=`,`<=` operator."
    "2.DO NOT calculate date range using functions."
    "3.SQL columns and values must be mentioned in the `#Schema`."
    "\n#Question:{question} #Schema:{schema} #InputSQL:{sql} #Response:"
)

_PHYSICAL_SQL_INSTRUCTION = (
    "#Role: You are a senior database performance optimization expert experienced in SQL "
    "tuning."
    "\n\n#Task: You will be provided with a user question and the corresponding physical SQL "
    "query, please analyze and optimize this SQL to improve query performance."
    "\n\n#Rules:"
    "\n1. DO NOT add or introduce any new fields, columns, or aliases that are not in the "
    "original SQL."
    "\n2. Push WHERE conditions into JOIN ON clauses when possible to reduce intermediate "
    "result sets."
    "\n3. Optimize JOIN order by placing smaller tables or tables with selective conditions "
    "first."
    "\n4. For date range conditions, ensure they are applied as early as possible in the query "
    "execution."
    "\n5. Remove or comment out database-specific index hints (like USE INDEX) that may cause "
    "syntax errors."
    "\n6. ONLY modify the structure and order of existing elements, do not change field names "
    "or add new ones."
    "\n7. Ensure the optimized SQL is syntactically correct and logically equivalent to the "
    "original."
    "\n\n#Question: {question}"
    "\n\n#OriginalSQL: {sql}"
)


class _CorrectionOutput(BaseModel):
    """Structured output shared by both LLM correctors."""

    model_config = ConfigDict(extra="forbid")

    opinion: Literal["positive", "negative"]
    sql: str = Field(default="", max_length=100_000)

    @field_validator("opinion", mode="before")
    @classmethod
    def normalize_opinion(cls, value: object) -> object:
        return value.casefold() if isinstance(value, str) else value


class LlmSqlCorrector:
    """LLM semantic-SQL corrector, disabled by default.

    Parity source:
    ``headless/chat/.../corrector/LLMSqlCorrector.java``. Gateway or output
    failures retain the original S2SQL because upstream ``BaseSemanticCorrector``
    catches every corrector exception and continues the workflow.
    """

    name = "LLMSqlCorrector"

    def __init__(
        self,
        gateway: StructuredModelGateway | None = None,
        *,
        enabled: bool = False,
    ) -> None:
        if enabled and gateway is None:
            raise ValueError("enabled LLMSqlCorrector requires a model gateway")
        self._gateway = gateway
        self.enabled = enabled

    def correct(
        self,
        *,
        candidate: ParsedSemanticCandidate,
        question: str,
        release: SemanticRelease,
        query_id: str,
        tenant_id: str = "",
    ) -> ParsedSemanticCandidate:
        if not self.enabled or self._gateway is None or candidate.parser != "llm":
            return candidate
        prompt = _S2SQL_INSTRUCTION.format(
            question=question,
            schema=_schema_context(candidate=candidate, release=release),
            sql=candidate.corrected_s2sql,
        )
        try:
            payload = self._gateway.generate_json(
                purpose="analytics.s2sql.corrector",
                messages=[{"role": "user", "content": prompt}],
                response_schema=_CorrectionOutput.model_json_schema(),
                trace={
                    "query_id": query_id,
                    "tenant_id": tenant_id,
                    "release_id": release.id,
                    "spec_hash": release.spec_hash,
                    "corrector": self.name,
                    "attempt": "1",
                },
            )
            output = _CorrectionOutput.model_validate(payload)
        except Exception:
            LOGGER.exception("LLMSqlCorrector failed; retaining original S2SQL")
            return candidate
        if output.opinion != "negative" or not output.sql.strip():
            return candidate
        return candidate.model_copy(update={"corrected_s2sql": output.sql.strip()})


class LlmPhysicalSqlCorrector:
    """LLM physical-SQL corrector, disabled by default.

    Parity source:
    ``headless/chat/.../corrector/LLMPhysicalSqlCorrector.java`` and
    ``ChatWorkflowEngine.performPhysicalSqlCorrecting``. It runs only after
    semantic translation and changes only the physical SQL text. Existing query
    parameters, result columns, route relations, defaults and limits remain bound
    to the translated query contract.
    """

    registry = ("LLMPhysicalSqlCorrector",)
    name = "LLMPhysicalSqlCorrector"

    def __init__(
        self,
        gateway: StructuredModelGateway | None = None,
        *,
        enabled: bool = False,
    ) -> None:
        if enabled and gateway is None:
            raise ValueError("enabled LLMPhysicalSqlCorrector requires a model gateway")
        self._gateway = gateway
        self.enabled = enabled

    @property
    def enabled_correctors(self) -> tuple[str, ...]:
        return (self.name,) if self.enabled else ()

    def correct(
        self,
        *,
        question: str,
        query: PhysicalQuery,
        release: SemanticRelease,
        query_id: str,
        tenant_id: str = "",
    ) -> PhysicalQuery:
        if not self.enabled or self._gateway is None:
            return query
        prompt = _PHYSICAL_SQL_INSTRUCTION.format(question=question, sql=query.sql)
        try:
            payload = self._gateway.generate_json(
                purpose="analytics.physical_sql.corrector",
                messages=[{"role": "user", "content": prompt}],
                response_schema=_CorrectionOutput.model_json_schema(),
                trace={
                    "query_id": query_id,
                    "tenant_id": tenant_id,
                    "release_id": release.id,
                    "spec_hash": release.spec_hash,
                    "corrector": self.name,
                    "attempt": "1",
                },
            )
            output = _CorrectionOutput.model_validate(payload)
        except Exception:
            LOGGER.exception("LLMPhysicalSqlCorrector failed; retaining original physical SQL")
            return query
        if output.opinion != "negative" or not output.sql.strip():
            return query
        return query.model_copy(update={"sql": output.sql.strip()})


def _schema_context(
    *,
    candidate: ParsedSemanticCandidate,
    release: SemanticRelease,
) -> str:
    """Rebuild the mapped schema carried by a Text2SQL exemplar."""

    dataset = next(item for item in release.datasets if item.id == candidate.dataset_id)
    matched_metric_ids = {
        item.element_id
        for item in candidate.mapping.matches
        if item.element_type is SemanticElementType.METRIC
    }
    matched_dimension_ids = {
        item.element_id
        for item in candidate.mapping.matches
        if item.element_type is SemanticElementType.DIMENSION
    }
    dimensions_by_id = {item.id: item for item in release.dimensions}
    fields_by_id = {item.id: item for item in release.fields}
    partition_dimension_ids = {
        dimension_id
        for dimension_id in dataset.dimension_ids
        if dimension_id in dimensions_by_id
        and dimensions_by_id[dimension_id].field_id in fields_by_id
        and fields_by_id[dimensions_by_id[dimension_id].field_id].dimension_type == "partition_time"
    }
    if dataset.default_time_dimension_id is not None:
        partition_dimension_ids.add(dataset.default_time_dimension_id)
    matched_dimension_ids.update(partition_dimension_ids)
    mapped_only = bool(candidate.mapping.matches)
    metrics = [
        {
            "name": item.name,
            "description": item.description,
            "aliases": item.aliases,
            "aggregation": item.aggregation.value if item.aggregation is not None else None,
            "unit": item.unit,
            "format": item.format,
        }
        for item in release.metrics
        if item.id in dataset.metric_ids and (not mapped_only or item.id in matched_metric_ids)
    ]
    dimensions = [
        {
            "name": item.name,
            "description": item.description,
            "aliases": item.aliases,
            "semantic_type": item.semantic_type,
        }
        for item in release.dimensions
        if item.id in dataset.dimension_ids
        and (not mapped_only or item.id in matched_dimension_ids)
    ]
    partition_dimension = (
        dimensions_by_id.get(dataset.default_time_dimension_id)
        if dataset.default_time_dimension_id is not None
        else dimensions_by_id[next(iter(partition_dimension_ids))]
        if len(partition_dimension_ids) == 1
        else None
    )
    primary_dimension = next(
        (
            dimensions_by_id[dimension_id]
            for dimension_id in dataset.dimension_ids
            if dimension_id in dimensions_by_id
            and dimensions_by_id[dimension_id].field_id in fields_by_id
            and fields_by_id[dimensions_by_id[dimension_id].field_id].identifier_type == "primary"
        ),
        None,
    )
    route = next(
        (item for item in release.analysis_topic_routes if item.dataset_id == dataset.id),
        None,
    )
    metrics_by_id = {item.id: item for item in release.metrics}
    count_metric = (
        metrics_by_id.get(route.default_count_metric_id)
        if route is not None and route.default_count_metric_id is not None
        else None
    )
    return json.dumps(
        {
            "dataset": {"name": dataset.name},
            "partition_time": (
                {"name": partition_dimension.name} if partition_dimension is not None else None
            ),
            "primary_key": (
                {"name": primary_dimension.name} if primary_dimension is not None else None
            ),
            "default_count_metric": (
                {
                    "name": count_metric.name,
                    "aggregation": count_metric.aggregation.value,
                }
                if count_metric is not None and count_metric.aggregation is not None
                else None
            ),
            "metrics": metrics,
            "dimensions": dimensions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
