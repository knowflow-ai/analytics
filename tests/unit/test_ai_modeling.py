from __future__ import annotations

import threading

import pytest
from pydantic import ValidationError

from knowflow_analytics.errors import SemanticValidationError
from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.catalog_contracts import (
    AggOperator,
    SemanticMetricContract,
)
from knowflow_analytics.modeling.contracts import EvidenceRef, SuggestionSource
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller


def _role_call(kwargs):
    """S2 表角色是独立的小调用，先于 ModelSchema。假网关统一按 fact 回答，
    不记录、不参与 barrier —— 这些测试断言的是 ModelSchema 那次调用。"""

    if kwargs.get("purpose") == "analytics.modeling.table_role":
        return {"role": "fact", "grain": "一行代表一条记录", "description": "测试表"}
    return None


class _FakeModelGateway:
    def __init__(self, model_id: str, field) -> None:
        self.model_id = model_id
        self.field = field
        self.schemas = []
        self.messages = []

    def generate_json(self, **kwargs):
        if (role := _role_call(kwargs)) is not None:
            return role
        self.schemas.append(kwargs["response_schema"])
        self.messages.append(kwargs["messages"])
        if kwargs["trace"]["model_id"] != self.model_id:
            return {
                "name": "其他模型",
                "bizName": "other_model",
                "description": "其他模型",
                "semanticColumns": [],
            }
        return {
            "name": "订单",
            "bizName": "orders",
            "description": "销售订单事实表",
            "semanticColumns": [
                {
                    "columnName": self.field.column,
                    "dataType": self.field.data_type,
                    "comment": "扣除退款后的确认收入",
                    "filedType": "categorical",
                    "name": "净收入",
                    "expr": self.field.column,
                },
                {
                    "columnName": "hallucinated_field",
                    "dataType": "TEXT",
                    "comment": "不存在字段",
                    "filedType": "categorical",
                    "name": "不存在字段",
                    "expr": "hallucinated_field",
                },
            ],
            "metrics": [
                {"columnName": self.field.column, "agg": "SUM", "unit": "元"},
            ],
        }


class _WrongForeignKeyGateway:
    def __init__(self, model_id: str, field) -> None:
        self.model_id = model_id
        self.field = field

    def generate_json(self, **kwargs):
        if (role := _role_call(kwargs)) is not None:
            return role
        if kwargs["trace"]["model_id"] != self.model_id:
            return {
                "name": "其他模型",
                "bizName": "other_model",
                "semanticColumns": [],
            }
        return {
            "name": "订单",
            "bizName": "orders",
            "semanticColumns": [
                {
                    "columnName": self.field.column,
                    "dataType": self.field.data_type,
                    "comment": "客户标识",
                    "filedType": "categorical",
                    "name": "客户ID",
                    "expr": self.field.column,
                }
            ],
        }


class _ConcurrentModelGateway:
    def __init__(self, expected_calls: int) -> None:
        self._barrier = threading.Barrier(expected_calls, timeout=1)
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def generate_json(self, **kwargs):
        if (role := _role_call(kwargs)) is not None:
            return role
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self._barrier.wait()
            model_id = kwargs["trace"]["model_id"]
            return {
                "name": f"模型 {model_id}",
                "bizName": model_id.replace(":", "_"),
                "description": "并发建模测试",
                "semanticColumns": [],
            }
        finally:
            with self._lock:
                self._active -= 1


def test_ai_modeler_builds_table_models_concurrently_and_preserves_model_order(
    schema_snapshot,
):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    table_models = tuple(
        model for model in revision.semantic_spec.models if model.query_type == "table_query"
    )
    gateway = _ConcurrentModelGateway(expected_calls=len(table_models))

    patches = AiSemanticModeller(model_gateway=gateway, workflow="single_call").suggest(
        modeling_job_id="job1",
        revision=revision,
        snapshot=schema_snapshot,
    )

    assert gateway.max_active == len(table_models)
    assert [patch.target_id for patch in patches if patch.target_kind == "model"] == [
        model.id for model in table_models
    ]


def test_ai_modeler_returns_exact_model_schema_patches_without_mutating_revision(
    schema_snapshot,
):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    orders = next(model for model in result.semantic_spec.models if model.table == "orders")
    net_amount = next(
        field for field in result.semantic_spec.fields if field.column == "net_amount"
    )
    original = revision.model_dump(mode="json")

    gateway = _FakeModelGateway(orders.id, net_amount)
    patches = AiSemanticModeller(model_gateway=gateway, workflow="single_call").suggest(
        modeling_job_id="job1",
        revision=revision,
        snapshot=schema_snapshot,
    )

    field_patches = [item for item in patches if item.target_kind == "field"]
    assert len(field_patches) == 1
    assert field_patches[0].target_id == net_amount.id
    assert field_patches[0].changes == {
        "name": "净收入",
        "description": "扣除退款后的确认收入",
        # Quoted so a digit-leading or punctuated column name still parses.
        "semantic_expr": '"net_amount"',
        "kind": "measure",
        "aggregation": "sum",
        "create_metric": True,
        "unit": "元",
    }
    assert "aliases" not in field_patches[0].changes
    assert all(
        "filedType" in schema["$defs"]["SemanticColumnContract"]["properties"]
        for schema in gateway.schemas
    )
    assert revision.model_dump(mode="json") == original


def test_database_constraint_wins_when_ai_misclassifies_a_foreign_key(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    orders = next(model for model in result.semantic_spec.models if model.table == "orders")
    customer_id = next(
        field
        for field in result.semantic_spec.fields
        if field.model_id == orders.id and field.column == "customer_id"
    )

    patches = AiSemanticModeller(
        model_gateway=_WrongForeignKeyGateway(orders.id, customer_id),
        workflow="single_call",
    ).suggest(
        modeling_job_id="job1",
        revision=revision,
        snapshot=schema_snapshot,
    )

    field_patch = next(item for item in patches if item.target_kind == "field")
    assert field_patch.changes["kind"] == "identifier"
    assert field_patch.changes["identifier_type"] == "foreign"
    assert "aggregation" not in field_patch.changes
    assert field_patch.confidence == 0.8
    assert "AI 不得覆盖数据库约束" in field_patch.reason


class _FakeKnowledgeGateway:
    def __init__(self) -> None:
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return (
            EvidenceRef(
                knowledgebase_id="kb1",
                document_id="doc1",
                document_revision="sha256:doc",
                chunk_id="chunk1",
                quote_hash="sha256:quote",
                citation="净收入是扣除退款后的确认收入，单位为元。",
            ),
        )


def test_m1_knowledge_enrichment_is_evidence_bound_and_non_mutating(schema_snapshot):
    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )

    orders = next(model for model in result.semantic_spec.models if model.table == "orders")
    net_amount = next(
        field for field in result.semantic_spec.fields if field.column == "net_amount"
    )
    model_gateway = _FakeModelGateway(orders.id, net_amount)
    knowledge_gateway = _FakeKnowledgeGateway()

    patches = AiSemanticModeller(
        model_gateway=model_gateway,
        knowledge_gateway=knowledge_gateway,
        workflow="single_call",
    ).suggest(
        modeling_job_id="job1",
        revision=revision,
        snapshot=schema_snapshot,
        manifest_hash="sha256:knowledge",
    )

    assert knowledge_gateway.calls
    assert {item.source for item in patches} == {
        SuggestionSource.AI_SCHEMA,
        SuggestionSource.AI_KNOWLEDGE,
    }
    knowledge_patches = [item for item in patches if item.source is SuggestionSource.AI_KNOWLEDGE]
    schema_patches = [item for item in patches if item.source is SuggestionSource.AI_SCHEMA]
    assert all(item.evidence[0].quote_hash == "sha256:quote" for item in knowledge_patches)
    assert all(not item.evidence for item in schema_patches)
    field_knowledge = next(
        item
        for item in knowledge_patches
        if item.target_kind == "field" and item.target_id == net_amount.id
    )
    field_schema = next(
        item
        for item in schema_patches
        if item.target_kind == "field" and item.target_id == net_amount.id
    )
    assert set(field_knowledge.changes) == {"name", "description", "unit"}
    assert "kind" in field_schema.changes
    assert "aggregation" in field_schema.changes
    assert all(call["manifest_hash"] == "sha256:knowledge" for call in knowledge_gateway.calls)
    assert "KnowledgeEvidence=" in model_gateway.messages[0][1]["content"]
    assert "sha256:quote" in model_gateway.messages[0][1]["content"]


class _DuplicateColumnThenValidGateway:
    """First response repeats a column; the retry returns a valid ModelSchema."""

    def __init__(self) -> None:
        self.attempts: list[int] = []

    def generate_json(self, **kwargs):
        if (role := _role_call(kwargs)) is not None:
            return role
        attempt = int(kwargs["trace"].get("attempt", "1"))
        self.attempts.append(attempt)
        column = {
            "columnName": "id",
            "dataType": "bigint",
            "filedType": "primary_key",
            "name": "编号",
            "expr": "id",
        }
        columns = [column, dict(column)] if attempt == 1 else [column]
        return {
            "name": "订单",
            "bizName": "orders",
            "description": "订单",
            "semanticColumns": columns,
        }


def test_invalid_model_schema_is_retried_before_failing_the_whole_run(schema_snapshot):
    """One malformed LLM response used to abort modeling for every table. The
    S2SQL parser already retries a rejected generation; modeling must too, or a
    single duplicated column throws away an otherwise complete run."""

    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    gateway = _DuplicateColumnThenValidGateway()

    patches = AiSemanticModeller(model_gateway=gateway, workflow="single_call").suggest(
        modeling_job_id="job-retry",
        revision=revision,
        snapshot=schema_snapshot,
    )

    assert 2 in gateway.attempts
    assert patches


class _TraceCapturingGateway:
    def __init__(self) -> None:
        self.traces: list[dict] = []

    def generate_json(self, **kwargs):
        if (role := _role_call(kwargs)) is not None:
            return role
        self.traces.append(kwargs["trace"])
        return {
            "name": "订单",
            "bizName": "orders",
            "description": "订单",
            "semanticColumns": [],
        }


def test_modeling_forwards_the_requesting_tenant_to_the_gateway(schema_snapshot):
    """The tenant owning the model configuration is known per request, so it has
    to travel with the modeling call instead of being pinned at service start."""

    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )
    gateway = _TraceCapturingGateway()

    AiSemanticModeller(model_gateway=gateway, workflow="single_call").suggest(
        modeling_job_id="job-tenant",
        revision=revision,
        snapshot=schema_snapshot,
        tenant_id="tenant-42",
    )

    assert gateway.traces
    assert all(item["tenant_id"] == "tenant-42" for item in gateway.traces)


def test_batch_alias_suggestion_sends_one_request_for_a_whole_model():
    """One request per metric and per dimension makes a 40-table schema cost
    hundreds of model calls. Aliases depend only on a resource's own metadata, so
    a model's resources are requested together, matching the batching the
    dimension-value stage already uses."""

    captured = []

    class Gateway:
        def generate_json(self, **kwargs):
            if (role := _role_call(kwargs)) is not None:
                return role
            captured.append(kwargs)
            return {
                "items": [
                    {"resource_id": "dim-region", "aliases": ["大区", "区域"]},
                    {"resource_id": "metric-revenue", "aliases": ["营收"]},
                ]
            }

    outputs = AiSemanticModeller(model_gateway=Gateway()).suggest_alias_batch(
        model_name="订单",
        resources=(
            {
                "resource_id": "dim-region",
                "resource_type": "dimension",
                "name": "地区",
                "biz_name": "region",
                "description": "订单归属地区",
                "existing_aliases": (),
            },
            {
                "resource_id": "metric-revenue",
                "resource_type": "metric",
                "name": "净收入",
                "biz_name": "net_revenue",
                "description": "扣除退款后的收入",
                "existing_aliases": (),
            },
        ),
        trace={"revision_id": "rev-1"},
    )

    assert len(captured) == 1
    assert outputs["dim-region"].aliases == ("大区", "区域")
    assert outputs["metric-revenue"].aliases == ("营收",)


def test_batch_alias_suggestion_keeps_the_single_resource_cleaning_rules():
    """Batching must not weaken the per-resource filters: the original name, the
    bizName, existing aliases and duplicates are all still rejected."""

    class Gateway:
        def generate_json(self, **kwargs):
            if (role := _role_call(kwargs)) is not None:
                return role
            return {
                "items": [
                    {
                        "resource_id": "dim-region",
                        # name, bizName, an existing alias, a duplicate and a blank
                        "aliases": ["地区", "region", "已有别名", "大区", "大区", " "],
                    }
                ]
            }

    outputs = AiSemanticModeller(model_gateway=Gateway()).suggest_alias_batch(
        model_name="订单",
        resources=(
            {
                "resource_id": "dim-region",
                "resource_type": "dimension",
                "name": "地区",
                "biz_name": "region",
                "description": "订单归属地区",
                "existing_aliases": ("已有别名",),
            },
        ),
        trace={"revision_id": "rev-1"},
    )

    assert outputs["dim-region"].aliases == ("大区",)


def test_batch_alias_suggestion_retries_a_structured_output_rejection():
    class Gateway:
        def __init__(self) -> None:
            self.attempts: list[int] = []

        def generate_json(self, **kwargs):
            attempt = int(kwargs["trace"].get("attempt", "1"))
            self.attempts.append(attempt)
            if attempt == 1:
                raise ModelGatewayError("model gateway rejected the request")
            return {
                "items": [
                    {"resource_id": "metric-revenue", "aliases": ["营收"]}
                ]
            }

    gateway = Gateway()
    outputs = AiSemanticModeller(model_gateway=gateway).suggest_alias_batch(
        model_name="订单",
        resources=(
            {
                "resource_id": "metric-revenue",
                "resource_type": "metric",
                "name": "净收入",
                "biz_name": "net_revenue",
                "description": "扣除退款后的收入",
                "existing_aliases": (),
            },
        ),
        trace={"revision_id": "rev-retry"},
    )

    assert gateway.attempts == [1, 2]
    assert outputs["metric-revenue"].aliases == ("营收",)


def test_batch_alias_suggestion_rejects_a_mismatched_resource_set():
    """A response that invents or drops a resource cannot be mapped back safely."""

    class Gateway:
        def generate_json(self, **kwargs):
            if (role := _role_call(kwargs)) is not None:
                return role
            return {"items": [{"resource_id": "unknown", "aliases": ["x"]}]}

    with pytest.raises(SemanticValidationError) as raised:
        AiSemanticModeller(model_gateway=Gateway()).suggest_alias_batch(
            model_name="订单",
            resources=(
                {
                    "resource_id": "dim-region",
                    "resource_type": "dimension",
                    "name": "地区",
                    "biz_name": "region",
                    "description": "",
                    "existing_aliases": (),
                },
            ),
            trace={"revision_id": "rev-1"},
        )

    assert raised.value.code == "AI_ALIAS_OUTPUT_INVALID"


def test_modeling_prompt_requires_an_aggregate_on_measures(schema_snapshot):
    """聚合方式曾经是列上的一个可选字段：真实 schema（股价、金额）回来是
    measure + NONE，校验拒收，每次重试都重复同一个输出，整个建模跑挂。

    聚合改由独立的 metrics 区块承载后，这个失败模式在结构上不可能出现——
    度量条目的 agg 必填且不接受 NONE。这里同时钉住 prompt 仍然要求把可聚合
    的数值列写进 metrics：不写就没有指标，用户问「总交易额」时无从映射。"""

    captured: list = []

    class _Gateway:
        def generate_json(self, **kwargs):
            if (role := _role_call(kwargs)) is not None:
                return role
            captured.append(kwargs["messages"])
            return {
                "name": "订单",
                "bizName": "orders",
                "description": "订单",
                "semanticColumns": [],
            }

    result = RuleSemanticModeller().build(project_id="sales", snapshot=schema_snapshot)
    revision = RevisionEditor().create(
        project_id="sales",
        schema_snapshot_hash=schema_snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=result.suggestions,
    )

    AiSemanticModeller(model_gateway=_Gateway(), workflow="single_call").suggest(
        modeling_job_id="job-agg-rule",
        revision=revision,
        snapshot=schema_snapshot,
    )

    system = captured[0][0]["content"]
    assert "metrics 是独立的一组" in system
    assert "跨行相加或计数有业务含义的数值列" in system

    # 结构上不再可能出现"度量没有聚合方式"
    with pytest.raises(ValidationError):
        SemanticMetricContract(column_name="net_amount", agg=AggOperator.NONE)


def test_semantic_expression_quotes_identifiers_that_sql_cannot_parse_bare():
    """A bare Chinese column is a valid SQL identifier, but one starting with a
    digit (500强排名) parses as a number and resolves to zero governed fields,
    failing the whole modeling run. Emit a quoted identifier so every physical
    column name survives expression parsing."""

    from knowflow_analytics.modeling.ai_modeller import _semantic_expression

    assert _semantic_expression("平均房价（万）") == '"平均房价（万）"'
    assert _semantic_expression("500强排名") == '"500强排名"'
    assert _semantic_expression("net_amount") == '"net_amount"'
    # An identifier that already carries quotes is not double-quoted.
    assert _semantic_expression('"已加引号"') == '"已加引号"'
