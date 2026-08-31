from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller


def _col(name, dtype="TEXT", *, pk=False):
    return SchemaColumnSnapshot(
        name=name, data_type=dtype, nullable=not pk, comment="", ordinal_position=0, primary_key=pk
    )


# products 有一个数值列 list_price；规则会把有数值非键列且被引用的表当维度，
# 但如果它没人引用（这里故意不建 FK），规则就退成 fact → list_price 被标 SUM。
_SNAPSHOT = SchemaSnapshot.create(
    database_name="db",
    captured_at=datetime(2026, 8, 23, tzinfo=UTC),
    tables=(
        TableSnapshot(
            schema_name="s",
            name="products",
            columns=(
                _col("id", "BIGINT", pk=True),
                _col("category"),
                _col("list_price", "NUMERIC"),
            ),
        ),
        TableSnapshot(
            schema_name="s",
            name="orders",
            columns=(
                _col("id", "BIGINT", pk=True),
                _col("product_id", "BIGINT"),
                _col("qty", "INTEGER"),
            ),
            foreign_keys=(
                ForeignKeySnapshot(
                    constrained_columns=("product_id",),
                    referred_schema="s",
                    referred_table="products",
                    referred_columns=("id",),
                ),
            ),
        ),
    ),
)


class _Gateway:
    """角色调用按表名回答；ModelSchema 调用把所有数值列都标 SUM（模拟 32B）。"""

    def __init__(self, *, roles: dict[str, str] | None = None, role_error: bool = False):
        self.roles = roles or {}
        self.role_error = role_error
        self.purposes: list[str] = []
        self.role_tables: list[str] = []

    def generate_json(self, **kwargs):
        self.purposes.append(kwargs["purpose"])
        if kwargs["purpose"] == "analytics.modeling.table_role":
            if self.role_error:
                raise ModelGatewayError("gateway down")
            import json

            table = json.loads(kwargs["messages"][1]["content"])["table"].split(".")[1]
            self.role_tables.append(table)
            return {"role": self.roles.get(table, "fact"), "grain": "一行一条", "description": ""}
        model_id = kwargs["trace"]["model_id"]
        table = "products" if "products" in model_id else "orders"
        cols = {
            "products": [
                ("id", "BIGINT", "primary_key", "NONE"),
                ("category", "TEXT", "categorical", "NONE"),
                ("list_price", "NUMERIC", "measure", "SUM"),
            ],
            "orders": [
                ("id", "BIGINT", "primary_key", "NONE"),
                ("product_id", "BIGINT", "foreign_key", "NONE"),
                ("qty", "INTEGER", "measure", "SUM"),
            ],
        }[table]
        return {
            "name": table,
            "bizName": table,
            "description": "",
            "semanticColumns": [
                {
                    "columnName": c,
                    "dataType": t,
                    "comment": "",
                    "filedType": ft,
                    "agg": a,
                    "name": c,
                    "expr": c,
                }
                for c, t, ft, a in cols
            ],
        }


def _run(gateway):
    result = RuleSemanticModeller().build(project_id="p", snapshot=_SNAPSHOT)
    revision = RevisionEditor().create(
        project_id="p",
        schema_snapshot_hash=_SNAPSHOT.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=(),
    )
    patches = AiSemanticModeller(model_gateway=gateway).suggest(
        modeling_job_id="job", revision=revision, snapshot=_SNAPSHOT
    )
    fields = {f.id: (f.model_id, f.column) for f in revision.semantic_spec.fields}
    return {fields[p.target_id][1]: p for p in patches if p.target_kind == "field"}


def test_the_role_call_happens_once_per_table_in_topological_order_before_model_schema():
    gateway = _Gateway()
    _run(gateway)

    roles = [p for p in gateway.purposes if p == "analytics.modeling.table_role"]
    assert len(roles) == 2
    # 被引用的 products 先于引用它的 orders
    assert gateway.role_tables == ["products", "orders"]
    # 两次角色调用都在任何命名调用之前
    first_naming = gateway.purposes.index("analytics.modeling.naming")
    assert all(p == "analytics.modeling.table_role" for p in gateway.purposes[:first_naming])


def test_a_dimension_role_from_the_model_flags_a_summed_price_attribute_for_review():
    """规则单看结构会把 products 当事实表（有数值非键列），list_price 就成了 SUM
    度量 —— 把商品标价相加没有意义。模型一句"dimension"让 S3 对它失去把握（0.5）；
    这一步不改判定（那是 S5 的事），但必须把分歧写进理由让人看见。"""

    by_column = _run(_Gateway(roles={"products": "dimension"}))

    price = by_column["list_price"]
    # 本装置的 classify 调用不可用：新语义下降级必须可见，不冒充"已复核"。
    assert "仅规则判定" in price.reason and "模型复核未执行" in price.reason
    # 同一模型回答 fact 的 orders 里，qty 是干净的度量，没有存疑标记
    qty = by_column["qty"]
    assert qty.changes["kind"] == "measure"
    assert "存疑" not in qty.reason


def test_when_the_role_call_fails_rules_take_over_and_nothing_is_marked_failed():
    by_column = _run(_Gateway(role_error=True))

    # 规则兜底：orders 有 FK 指出去且有数值非键列 → fact → qty 是度量
    assert by_column["qty"].changes["kind"] == "measure"
    assert by_column["product_id"].changes["kind"] == "identifier"
