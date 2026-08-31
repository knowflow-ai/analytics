from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from knowflow_analytics.gateways.model import StructuredModelGateway
from knowflow_analytics.query.contracts import MappingResult
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.semantic.index import SemanticElementType

_REWRITE_INSTRUCTION = (
    "#Role: You are a data product manager experienced in data requirements."
    "#Task: Your will be provided with current and history questions asked by a user,"
    "along with their mapped schema elements(metric, dimension and value),"
    "please try understanding the semantics and rewrite a question."
    "#Rules: "
    "1.ALWAYS keep relevant entities, metrics, dimensions, values and date ranges."
    "2.ONLY respond with the rewritten question."
)


class _RewriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_question: str = Field(min_length=1, max_length=4_000)


class MultiTurnContext(BaseModel):
    """Inputs used by the multi-turn question rewrite stage.

    The context intentionally contains only logical S2SQL and governed mapper
    evidence. Physical SQL and result rows are not part of this contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_question: str = Field(min_length=1, max_length=4_000)
    # 改写调用的模型归属:当前请求 actor 的租户,无静态兜底。
    tenant_id: str = ""
    current_mapping: MappingResult
    previous_question: str = Field(min_length=1, max_length=4_000)
    previous_mapping: MappingResult
    previous_corrected_s2sql: str = Field(min_length=1, max_length=100_000)


class QueryHistoryTurn(BaseModel):
    """One successful logical turn eligible for the next rewrite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str = Field(min_length=1, max_length=4_000)
    effective_question: str = Field(min_length=1, max_length=4_000)
    corrected_s2sql: str = Field(min_length=1, max_length=100_000)
    mapping: MappingResult
    dataset_id: str = Field(min_length=1, max_length=128)
    release_id: str = Field(min_length=1, max_length=128)
    spec_hash: str = Field(min_length=1, max_length=128)
    index_snapshot_id: str = Field(min_length=1, max_length=128)


class QueryHistoryStore(Protocol):
    def last_success(
        self,
        *,
        actor_id: str,
        project_id: str,
        conversation_id: str,
        release_id: str,
        spec_hash: str,
        index_snapshot_id: str,
        dataset_id: str,
    ) -> QueryHistoryTurn | None: ...

    def save_success(
        self,
        turn: QueryHistoryTurn,
        *,
        actor_id: str,
        project_id: str,
        conversation_id: str,
    ) -> None: ...


class MultiTurnRewriter:
    """One-turn question rewrite stage."""

    def __init__(self, gateway: StructuredModelGateway, *, enabled: bool = False) -> None:
        self._gateway = gateway
        self.enabled = enabled

    def rewrite(self, context: MultiTurnContext) -> str:
        if not self.enabled:
            return context.current_question
        payload = self._gateway.generate_json(
            purpose="analytics.multi_turn_rewrite",
            messages=[
                {"role": "system", "content": _REWRITE_INSTRUCTION},
                {
                    "role": "user",
                    "content": (
                        f"#Current Question: {context.current_question}"
                        f"#Current Mapped Schema: {_schema_prompt(context.current_mapping)}"
                        f"#History Question: {context.previous_question}"
                        f"#History Mapped Schema: {_schema_prompt(context.previous_mapping)}"
                        f"#History SQL: {context.previous_corrected_s2sql}"
                        "#Rewritten Question:"
                    ),
                },
            ],
            response_schema=_RewriteOutput.model_json_schema(),
            trace={
                "tenant_id": context.tenant_id,
                "contract_version": "knowflow-multi-turn-v1",
                "dataset_id": context.current_mapping.dataset_id,
            },
        )
        try:
            rewritten = _RewriteOutput.model_validate(payload).rewritten_question.strip()
        except ValueError as exc:
            raise SemanticParsingError(
                "模型未返回合法的多轮改写问题",
                code="MULTI_TURN_REWRITE_INVALID",
            ) from exc
        if not rewritten:
            raise SemanticParsingError(
                "模型未返回合法的多轮改写问题",
                code="MULTI_TURN_REWRITE_INVALID",
            )
        return rewritten


def _schema_prompt(mapping: MappingResult) -> str:
    metrics: list[str] = []
    dimensions: list[str] = []
    values: list[str] = []
    for match in mapping.matches:
        target = None
        if match.element_type is SemanticElementType.METRIC:
            target = metrics
        elif match.element_type is SemanticElementType.DIMENSION:
            target = dimensions
        elif match.element_type is SemanticElementType.DIMENSION_VALUE:
            target = values
        if target is not None and match.phrase not in target:
            target.append(match.phrase)
    return (
        f"'metrics:':[{','.join(metrics)}],"
        f"'dimensions:':[{','.join(dimensions)}],"
        f"'values:':[{','.join(values)}]"
    )
