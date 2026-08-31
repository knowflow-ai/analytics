from __future__ import annotations

import json
from datetime import UTC, datetime

from knowflow_analytics.gateways.model import ModelGatewayError
from knowflow_analytics.modeling.ai_modeller import AiSemanticModeller
from knowflow_analytics.modeling.contracts import (
    ForeignKeySnapshot,
    SchemaColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
)
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.modeling.revision import RevisionEditor
from knowflow_analytics.modeling.rule_modeller import RuleSemanticModeller


def _col(name, dtype="TEXT", *, pk=False):
    return SchemaColumnSnapshot(
        name=name, data_type=dtype, nullable=not pk, comment="", ordinal_position=0, primary_key=pk
    )


def _snapshot(*tables):
    return SchemaSnapshot.create(
        database_name="db", captured_at=datetime(2026, 8, 23, tzinfo=UTC), tables=tables
    )


CUSTOMERS = TableSnapshot(
    schema_name="s",
    name="customers",
    columns=(_col("id", "BIGINT", pk=True), _col("segment"), _col("region")),
)
ORDERS = TableSnapshot(
    schema_name="s",
    name="orders",
    columns=(
        _col("id", "BIGINT", pk=True),
        _col("customer_id", "BIGINT"),
        _col("region"),
        _col("amount", "NUMERIC(18,2)"),
        _col("year", "INTEGER"),
    ),
    foreign_keys=(
        ForeignKeySnapshot(
            constrained_columns=("customer_id",),
            referred_schema="s",
            referred_table="customers",
            referred_columns=("id",),
        ),
    ),
)


class _Gateway:
    """记录每次调用；角色按表答；命名按 column → 中文名表答，可注入幻觉列或漏列。"""

    def __init__(
        self,
        names: dict[str, str],
        *,
        hallucinate=False,
        drop: set[str] = frozenset(),
        first_invalid=False,
        classify: dict[str, tuple[str, str | None]] | None = None,
        classify_error=False,
        naming_error=False,
    ):
        self.names = names
        self.hallucinate = hallucinate
        self.drop = drop
        self.first_invalid = first_invalid
        self.classify = classify or {}
        self.classify_error = classify_error
        self.naming_error = naming_error
        self.calls: list[dict] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        purpose = kwargs["purpose"]
        body = json.loads(kwargs["messages"][1]["content"]) if len(kwargs["messages"]) >= 2 else {}
        if purpose == "analytics.modeling.table_role":
            table = body["table"].split(".")[1]
            return {
                "role": "dimension" if table == "customers" else "fact",
                "grain": f"一行代表一个{table}",
                "name": {"customers": "客户", "orders": "订单"}.get(table, table),
                "description": f"{table} 表",
            }
        if purpose == "analytics.modeling.naming":
            if self.naming_error:
                raise ModelGatewayError("upstream 503")
            if self.first_invalid and kwargs["trace"]["attempt"] == "1":
                return {"columns": [{"column_name": "x"}]}  # 缺 name → ValidationError
            asked = [c["column"] for c in body["columns"]]
            out = [
                {
                    "column_name": c,
                    "name": self.names.get(c, c),
                    "description": f"{c} 的说明",
                    "unit": "元" if c == "amount" else None,
                }
                for c in asked
                if c not in self.drop
            ]
            if self.hallucinate:
                out.append({"column_name": "ghost", "name": "幽灵列"})
            return {"columns": out}
        if purpose == "analytics.modeling.classify":
            if self.classify_error:
                raise ModelGatewayError("gateway down")
            out = []
            for c in body["columns"]:
                # 盲判：payload 不带规则结论，未在剧本里的列一律答 dimension。
                kind, agg = self.classify.get(c["column"], ("dimension", None))
                out.append(
                    {
                        "column_name": c["column"],
                        "kind": kind,
                        "aggregation": agg,
                        "reason": f"{c['column']} 看画像像 {kind}",
                    }
                )
            return {"columns": out}
        raise AssertionError(f"unexpected purpose {purpose}")


def _run(snapshot, gateway, *, profiles=None, chunk_size=20):
    result = RuleSemanticModeller().build(project_id="p", snapshot=snapshot)
    revision = RevisionEditor().create(
        project_id="p",
        schema_snapshot_hash=snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=(),
    )
    patches = AiSemanticModeller(model_gateway=gateway, chunk_size=chunk_size).suggest(
        modeling_job_id="job", revision=revision, snapshot=snapshot, profiles=profiles
    )
    fields = {f.id: f for f in revision.semantic_spec.fields}
    by = {}
    for p in patches:
        if p.target_kind == "field":
            f = fields[p.target_id]
            by[(f.model_id.split(":")[-1] if ":" in f.model_id else f.model_id, f.column)] = p
    models = {p.target_id: p for p in patches if p.target_kind == "model"}
    return by, models, revision


def _field(by, table, column):
    return next(v for (m, c), v in by.items() if c == column and table in m)


def test_names_come_from_the_model_and_classification_from_the_rules():
    """S4 只负责起名：命名 response schema 里没有 kind/agg，分类走 S3+S5 对账。

    盲判剧本与规则一致（amount=measure·SUM），本测试只守"命名不得夹带分类"。
    """

    gateway = _Gateway(
        {"amount": "订单金额", "year": "年份", "segment": "客户分群"},
        classify={"amount": ("measure", "SUM")},
    )
    by, _models, _rev = _run(_snapshot(CUSTOMERS, ORDERS), gateway)

    amount = _field(by, "orders", "amount")
    assert amount.changes["name"] == "订单金额"
    assert amount.changes["kind"] == "measure" and amount.changes["aggregation"] == "sum"
    assert amount.changes["unit"] == "元"
    year = _field(by, "orders", "year")
    assert year.changes["name"] == "年份"
    assert year.changes["kind"] == "dimension" and "aggregation" not in year.changes
    # 命名调用里没有任何分类字段让模型填
    naming = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.naming"]
    schema = naming[0]["response_schema"]
    assert "kind" not in json.dumps(schema) and "agg" not in json.dumps(schema)


def test_dimension_tables_are_named_first_and_attribute_columns_stay_per_table():
    """2026-08-28 合同修订（I1/I2 锚定复用）：非标识列不再跨表沿用。

    旧合同让 customers 定下的 region → 「区域」直接盖到 orders.region 上——
    同一机制在城市/图书馆回归里把 图书馆.名称 盖成了「城市名称」（3/12 的
    直接根因）。新合同：实体属性列逐表询问模型；跨表硬沿用仅限非主键标识列。
    """

    gateway = _Gateway({"region": "区域", "segment": "客户分群"})
    by, _models, _rev = _run(_snapshot(CUSTOMERS, ORDERS), gateway)

    naming = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.naming"]
    tables = [json.loads(c["messages"][1]["content"])["table"]["name"] for c in naming]
    assert tables[0] == "s.customers"
    orders_asked = json.loads(naming[-1]["messages"][1]["content"])
    # orders 的 region 是本表实体属性：必须真的送给模型，不被约定 settle。
    assert "region" in [c["column"] for c in orders_asked["columns"]]
    assert orders_asked.get("naming_conventions", {}) == {}
    assert _field(by, "orders", "region").changes["name"] == "区域"
    # 事实表的关联表带上了已命名的「客户」
    assert orders_asked["related"][0] == {
        "table": "s.customers",
        "name": "客户",
        "via": "customer_id",
    }


def test_the_naming_prompt_carries_role_grain_and_profile_values():
    profile = TableProfile(
        schema_name="s",
        table="customers",
        row_count=1000,
        columns=(
            ColumnProfile(
                column="segment",
                row_count=1000,
                non_null_count=990,
                distinct_count=3,
                sample_values=("VIP", "普通", "流失"),
            ),
        ),
    )
    gateway = _Gateway({})
    _run(_snapshot(CUSTOMERS), gateway, profiles={("s", "customers"): profile})

    body = json.loads(
        next(c for c in gateway.calls if c["purpose"] == "analytics.modeling.naming")["messages"][
            1
        ]["content"]
    )
    assert body["table"]["role"] == "dimension"
    assert body["table"]["grain"] == "一行代表一个customers"
    segment = next(c for c in body["columns"] if c["column"] == "segment")
    assert segment["profile"]["values"] == ["VIP", "普通", "流失"]
    assert segment["profile"]["distinct"] == 3
    assert segment["kind"] == "dimension"  # 命名需要角色定单位；这是任务输入不是待复核结论


def test_wide_tables_are_chunked_and_merged_without_losing_columns():
    wide = TableSnapshot(
        schema_name="s",
        name="wide",
        columns=(_col("id", "BIGINT", pk=True), *[_col(f"c{i}") for i in range(24)]),
    )
    gateway = _Gateway({f"c{i}": f"列{i}" for i in range(24)})
    by, _models, _rev = _run(_snapshot(wide), gateway, chunk_size=10)

    naming = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.naming"]
    assert len(naming) == 3  # 25 列 → 10 + 10 + 5
    assert len([k for k in by if k[0].endswith("wide") or "wide" in k[0]]) == 25
    assert _field(by, "wide", "c23").changes["name"] == "列23"


def test_hallucinated_columns_are_dropped_and_missing_ones_fall_back_to_the_column_name():
    gateway = _Gateway({"segment": "客户分群"}, hallucinate=True, drop={"region"})
    by, _models, _rev = _run(_snapshot(CUSTOMERS), gateway)

    assert not any(c == "ghost" for (_m, c) in by)
    assert _field(by, "customers", "region").changes["name"] == "region"  # 兜底
    assert _field(by, "customers", "segment").changes["name"] == "客户分群"


def test_a_schema_violation_is_fed_back_and_the_second_attempt_lands():
    gateway = _Gateway({"segment": "客户分群"}, first_invalid=True)
    by, _models, _rev = _run(_snapshot(CUSTOMERS), gateway)

    naming = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.naming"]
    assert [c["trace"]["attempt"] for c in naming] == ["1", "2"]
    second = naming[1]["messages"]
    assert second[-1]["role"] == "user" and "不符合 JSON Schema" in second[-1]["content"]
    assert _field(by, "customers", "segment").changes["name"] == "客户分群"


def test_table_level_name_and_description_come_from_the_role_call():
    gateway = _Gateway({})
    _by, models, revision = _run(_snapshot(CUSTOMERS), gateway)
    patch = next(iter(models.values()))
    assert patch.changes["name"] == "客户"
    assert patch.changes["biz_name"] == "customers"


# ---- S5 存疑分类 --------------------------------------------------------------

PRODUCTS = TableSnapshot(
    schema_name="s",
    name="products",
    columns=(
        _col("id", "BIGINT", pk=True),
        _col("category"),
        _col("list_price", "NUMERIC"),
        _col("stock_qty", "INTEGER"),
    ),
)
_PRODUCTS_PROFILE = TableProfile(
    schema_name="s",
    table="products",
    row_count=5000,
    columns=(
        ColumnProfile(column="category", row_count=5000, non_null_count=5000, distinct_count=6),
        ColumnProfile(
            column="list_price",
            row_count=5000,
            non_null_count=5000,
            distinct_count=3200,
            min_value="1.00",
            max_value="9999.00",
        ),
        ColumnProfile(
            column="stock_qty",
            row_count=5000,
            non_null_count=5000,
            distinct_count=800,
            min_value="0",
            max_value="20000",
        ),
    ),
)


class _DimGateway(_Gateway):
    def generate_json(self, **kwargs):
        if kwargs["purpose"] == "analytics.modeling.table_role":
            self.calls.append(kwargs)
            return {
                "role": "dimension",
                "grain": "一行代表一个商品",
                "name": "商品",
                "description": "",
            }
        return super().generate_json(**kwargs)


def _products(gateway):
    return _run(_snapshot(PRODUCTS), gateway, profiles={("s", "products"): _PRODUCTS_PROFILE})[0]


def test_judgment_zone_columns_are_sent_blind_without_the_rules_verdict():
    """判断域列（数值非键）全部盲判；id / category 是事实与强证据，不进 S5。

    payload 不得携带规则结论——带着答案问"你同意吗"只会复读规则（假绿）。
    """

    gateway = _DimGateway(
        {}, classify={"list_price": ("dimension", None), "stock_qty": ("measure", "SUM")}
    )
    _products(gateway)

    classify = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.classify"]
    assert len(classify) == 1
    body = json.loads(classify[0]["messages"][1]["content"])
    assert {c["column"] for c in body["columns"]} == {"list_price", "stock_qty"}
    price = next(c for c in body["columns"] if c["column"] == "list_price")
    assert "prefill" not in price
    assert price["profile"]["max"] == "9999.00"
    system = classify[0]["messages"][0]["content"]
    assert "已按规则给出" not in system
    assert "维度表里的数值" not in system
    # 枚举只给 6 个合法聚合
    agg_schema = classify[0]["response_schema"]["$defs"]["ClassifiedColumn"]["properties"][
        "aggregation"
    ]
    assert set(agg_schema["anyOf"][0]["enum"]) == {
        "SUM",
        "COUNT",
        "COUNT_DISTINCT",
        "AVG",
        "MIN",
        "MAX",
    }


def test_the_models_verdict_overrides_the_rules_guess_and_clears_the_review_flag():
    by = _products(
        _DimGateway(
            {}, classify={"list_price": ("dimension", None), "stock_qty": ("measure", "SUM")}
        )
    )

    stock = _field(by, "products", "stock_qty")
    assert stock.changes["kind"] == "measure" and stock.changes["aggregation"] == "sum"
    assert "分歧" in stock.reason  # 规则判维度、模型盲判度量：这是分歧，不是确认
    price = _field(by, "products", "list_price")
    assert price.changes["kind"] == "dimension" and "aggregation" not in price.changes
    assert "一致" in price.reason and "存疑" not in price.reason


def test_when_the_classify_call_fails_the_rules_guess_stays_and_says_review_never_ran():
    """模型没跑成和模型确认过必须长得不一样——静默降级是拿规则冒充复核。"""

    by = _products(_DimGateway({}, classify_error=True))

    price = _field(by, "products", "list_price")
    assert price.changes["kind"] == "dimension"
    assert "模型复核未执行" in price.reason


def test_a_measure_verdict_without_an_aggregation_is_rejected_and_the_rule_kept():
    by = _products(_DimGateway({}, classify={"stock_qty": ("measure", None)}))

    stock = _field(by, "products", "stock_qty")
    assert stock.changes["kind"] == "dimension"  # 规则预填保留
    assert "模型复核未执行" in stock.reason


def test_a_kind_dispute_defaults_to_the_blind_verdict_and_is_escalated():
    """规则判维度(0.5)、模型盲判度量 MAX：默认采模型，且分歧必须升级为高影响。"""

    by = _products(_DimGateway({}, classify={"list_price": ("measure", "MAX")}))

    price = _field(by, "products", "list_price")
    assert price.changes["kind"] == "measure"
    assert price.changes["aggregation"] == "max"
    assert "分歧" in price.reason
    assert "规则" in price.reason and "模型" in price.reason
    assert price.high_impact is True


def test_a_confident_fact_table_sum_still_gets_a_blind_second_opinion():
    """规则 10：事实表数值 → SUM(0.8) 以前免审。利率求和就是这么漏的。"""

    gateway = _Gateway({"amount": "订单金额", "year": "年份", "region": "地区"})
    _run(_snapshot(CUSTOMERS, ORDERS), gateway)

    classify = [c for c in gateway.calls if c["purpose"] == "analytics.modeling.classify"]
    asked = {
        c["column"]
        for call in classify
        for c in json.loads(call["messages"][1]["content"])["columns"]
    }
    assert "amount" in asked


def test_a_degenerate_biz_name_is_not_proposed_for_chinese_tables():
    """纯中文表名派生不出英文标识,兜底常量 "model" 三表撞名。
    派生退化时不提 biz_name,保留导入时的现值。"""

    chinese = TableSnapshot(
        schema_name="s",
        name="城市",
        columns=(_col("id", "BIGINT", pk=True), _col("名称")),
    )
    _by, models, _rev = _run(_snapshot(chinese), _Gateway({"名称": "城市名称"}))

    patch = next(p for target_id, p in models.items() if "城市" in target_id)
    assert "biz_name" not in patch.changes
    assert patch.changes["name"]  # 名称与说明照常建议


def test_a_verdict_that_mangles_the_column_name_lands_by_index():
    """真实故障第二形态：模型直接丢掉「（万）」后缀，归一化也救不回。
    payload 按 index 编号，模型按 index 作答，名字只是人读的。"""

    fullwidth = TableSnapshot(
        schema_name="s",
        name="城市",
        columns=(_col("id", "BIGINT", pk=True), _col("平均房价（万）", "NUMERIC")),
    )

    class _MangledGateway(_Gateway):
        def generate_json(self, **kwargs):
            if kwargs["purpose"] == "analytics.modeling.classify":
                self.calls.append(kwargs)
                body = json.loads(kwargs["messages"][1]["content"])
                assert body["columns"][0]["index"] == 0  # payload 必须带 index
                return {
                    "columns": [
                        {
                            "index": 0,
                            "column_name": "平均房价",  # 后缀被吃掉
                            "kind": "measure",
                            "aggregation": "MAX",
                            "reason": "每城市一行，取值即当值",
                        }
                    ]
                }
            return super().generate_json(**kwargs)

    by = _run(_snapshot(fullwidth), _MangledGateway({"平均房价（万）": "平均房价"}))[0]

    price = _field(by, "城市", "平均房价（万）")
    assert price.changes["kind"] == "measure"
    assert price.changes["aggregation"] == "max"
    assert "模型复核未执行" not in price.reason


def test_a_verdict_with_normalized_punctuation_still_lands_on_the_column():
    """真实故障：列名「平均房价（万）」，模型返回「平均房价(万)」（全角→半角），
    精确匹配失败被当成"未复核"。对账必须 NFKC 归一后再对一次。"""

    fullwidth = TableSnapshot(
        schema_name="s",
        name="城市",
        columns=(_col("id", "BIGINT", pk=True), _col("平均房价（万）", "NUMERIC")),
    )

    class _NormalizingGateway(_Gateway):
        def generate_json(self, **kwargs):
            if kwargs["purpose"] == "analytics.modeling.classify":
                self.calls.append(kwargs)
                return {
                    "columns": [
                        {
                            "column_name": "平均房价(万)",  # 半角括号
                            "kind": "measure",
                            "aggregation": "MAX",
                            "reason": "每城市一行，取值即当值",
                        }
                    ]
                }
            return super().generate_json(**kwargs)

    by = _run(_snapshot(fullwidth), _NormalizingGateway({"平均房价（万）": "平均房价"}))[0]

    price = _field(by, "城市", "平均房价（万）")
    assert price.changes["kind"] == "measure"
    assert price.changes["aggregation"] == "max"
    assert "模型复核未执行" not in price.reason


def test_an_aggregation_dispute_is_escalated_with_the_blind_aggregation():
    """kind 都是 measure 但聚合不同（SUM vs AVG）：这是最危险的分歧，必须升级。"""

    gateway = _Gateway(
        {"amount": "订单金额", "year": "年份", "region": "地区"},
        classify={"amount": ("measure", "AVG")},
    )
    by = _run(_snapshot(CUSTOMERS, ORDERS), gateway)[0]

    amount = _field(by, "orders", "amount")
    assert amount.changes["kind"] == "measure"
    assert amount.changes["aggregation"] == "avg"
    assert "分歧" in amount.reason
    assert amount.high_impact is True


# ---- 单表隔离 ------------------------------------------------------------------


class _ExplodeOnOrders(_Gateway):
    def generate_json(self, **kwargs):
        body = json.loads(kwargs["messages"][1]["content"]) if len(kwargs["messages"]) >= 2 else {}
        if kwargs["purpose"] == "analytics.modeling.naming" and "orders" in str(
            body.get("table", {}).get("name", "")
        ):
            raise RuntimeError("orders blew up")
        return super().generate_json(**kwargs)


def test_one_table_failing_does_not_discard_the_others():
    """此前 executor.map 第一个异常丢掉整轮结果。现在 customers 照常产出，
    orders 记进进度为 failed，整轮只有全表失败才抛。"""

    progress = []
    snapshot = _snapshot(CUSTOMERS, ORDERS)
    result = RuleSemanticModeller().build(project_id="p", snapshot=snapshot)
    revision = RevisionEditor().create(
        project_id="p",
        schema_snapshot_hash=snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=(),
    )
    patches = AiSemanticModeller(model_gateway=_ExplodeOnOrders({"segment": "客户分群"})).suggest(
        modeling_job_id="job",
        revision=revision,
        snapshot=snapshot,
        progress=lambda mid, name, status, err: progress.append((name, status, err)),
    )

    fields = {f.id: f for f in revision.semantic_spec.fields}
    touched_models = {fields[p.target_id].model_id for p in patches if p.target_kind == "field"}
    assert any("customers" in m for m in touched_models)
    assert not any("orders" in m for m in touched_models)
    assert ("orders", "failed", "orders blew up") in progress
    assert ("customers", "completed", None) in progress


def test_all_tables_failing_raises():
    class _AlwaysExplode(_Gateway):
        def generate_json(self, **kwargs):
            if kwargs["purpose"] == "analytics.modeling.naming":
                raise RuntimeError("down")
            return super().generate_json(**kwargs)

    snapshot = _snapshot(CUSTOMERS)
    result = RuleSemanticModeller().build(project_id="p", snapshot=snapshot)
    revision = RevisionEditor().create(
        project_id="p",
        schema_snapshot_hash=snapshot.content_hash,
        semantic_spec=result.semantic_spec,
        suggestions=(),
    )
    import pytest

    with pytest.raises(RuntimeError, match="down"):
        AiSemanticModeller(model_gateway=_AlwaysExplode({})).suggest(
            modeling_job_id="job", revision=revision, snapshot=snapshot
        )


def test_a_naming_gateway_failure_is_loud_and_marks_every_fallback_name(caplog):
    """2026-08-24 事故：命名调用瞬时失败,全表字段名静默退化成物理列名,任务照样
    "完成"。中文表上看不出来(列名本来就是中文),英文表让它显形(segment / net_amount),
    存库别名跟着同语言变英文,中文问法 0 命中。失败必须留日志,兜底必须在决策卡可见。"""

    import logging

    with caplog.at_level(logging.WARNING):
        by = _products(_DimGateway({}, naming_error=True))

    price = _field(by, "products", "list_price")
    assert price.changes["name"] == "list_price"  # 兜底成物理列名——行为保持
    assert "业务名未生成" in price.reason  # 但必须可见
    assert any("naming" in r.message and "products" in r.message for r in caplog.records)


def test_a_column_the_model_omitted_is_marked_for_review():
    """整表成功但漏了一列:那一列同样是兜底名,同样要标出来;其它列不受牵连。"""

    by = _products(_DimGateway({"list_price": "标价"}, drop={"stock_qty"}))

    stock = _field(by, "products", "stock_qty")
    assert stock.changes["name"] == "stock_qty"
    assert "业务名未生成" in stock.reason
    price = _field(by, "products", "list_price")
    assert price.changes["name"] == "标价"
    assert "业务名未生成" not in price.reason


def test_a_classify_gateway_failure_is_logged(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        _products(_DimGateway({}, classify_error=True))

    assert any("classify" in r.message and "products" in r.message for r in caplog.records)
