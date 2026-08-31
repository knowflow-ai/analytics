from __future__ import annotations

from knowflow_analytics.contracts import FieldSpec, ModelSpec
from knowflow_analytics.modeling.contracts import SuggestionPatch, SuggestionSource, SuggestionState
from knowflow_analytics.modeling.proposal_defaults import default_accept_for_suggestion


def _patch(changes, *, source=SuggestionSource.AI_SCHEMA, high_impact=False, target_kind="field"):
    return SuggestionPatch(
        id="s",
        target_kind=target_kind,
        target_id="f",
        changes=changes,
        source=source,
        confidence=0.9,
        state=SuggestionState.PENDING,
        high_impact=high_impact,
    )


def _fresh_field(**overrides) -> dict:
    spec = FieldSpec(
        id="f", model_id="m", name="region_code", column="region_code", data_type="text"
    )
    return {**spec.model_dump(mode="json"), **overrides}


def test_classifying_a_fresh_field_is_accepted_by_default():
    """kind 默认 "field"、create_dimension 默认 False —— 都是"还没分类"，不是人填的。
    把它们当成已有值，AI 的每条分类建议都被判成"覆盖"而默认不勾选；
    「确认并应用全部」只应用勾选项，分类一条都没进去。"""

    assert default_accept_for_suggestion(
        _patch({"kind": "dimension", "dimension_type": "categorical", "create_dimension": True}),
        _fresh_field(),
    )


def test_the_import_placeholder_name_does_not_count_as_a_human_value():
    """AI 把 name + kind 打包在一条里。导入时 name 就是列名；只要它被当成人填的，
    整条（含分类）就默认不勾选 —— 刚导入的表上每条都是。"""

    assert default_accept_for_suggestion(
        _patch({"name": "客户分群", "kind": "dimension", "create_dimension": True}),
        _fresh_field(),
    )


def test_a_human_rename_is_still_protected():
    assert not default_accept_for_suggestion(
        _patch({"name": "客户分群"}),
        _fresh_field(name="我改过的名字"),
    )


def test_measure_classification_is_adopted_by_default_too():
    """弹窗本身就是逐条审核 AI 草稿、按钮写着「确认并应用全部」；默认关掉
    high_impact 等于要求用户把 AI 做的事再做一遍。表格仍标「高影响，需重点核对」。"""

    assert default_accept_for_suggestion(
        _patch({"kind": "measure", "aggregation": "sum", "create_metric": True}, high_impact=True),
        _fresh_field(),
    )


def test_overwriting_something_a_human_typed_is_still_opt_in():
    """唯一保留的默认关闭：不该悄悄丢掉用户已经写下的东西。"""

    assert not default_accept_for_suggestion(
        _patch({"description": "AI 写的说明"}, high_impact=True),
        _fresh_field(description="我写的说明"),
    )


def test_database_constraints_are_always_accepted():
    assert default_accept_for_suggestion(
        _patch(
            {"kind": "identifier"},
            source=SuggestionSource.DATABASE_CONSTRAINT,
            high_impact=True,
        ),
        _fresh_field(name="谁改过都无所谓"),
    )


def test_model_name_equal_to_table_is_a_placeholder_too():
    model = ModelSpec(id="m", name="orders", table="orders", schema_name="public")
    patch = SuggestionPatch(
        id="s",
        target_kind="model",
        target_id="m",
        changes={"name": "订单"},
        source=SuggestionSource.AI_SCHEMA,
        confidence=0.9,
        state=SuggestionState.PENDING,
    )
    assert default_accept_for_suggestion(patch, model.model_dump(mode="json"))


# ---- 导入时抄来的值不是人工内容 --------------------------------------------------


def _imported_model(**overrides) -> dict:
    spec = ModelSpec(
        id="m",
        name="城市",
        biz_name="城市",
        table="城市",
        schema_name="bench_6",
        description="SeSQL 银行贷款：城市",
    )
    return {**spec.model_dump(mode="json"), **overrides}


def test_import_time_biz_name_and_comment_description_are_not_human_values():
    """导入把 biz_name 抄成表名、description 抄成表注释——都不是人写的。
    把它们当人工内容,三个实体的建议全部默认不勾,「确认并应用全部」一条不进。"""

    accepted = default_accept_for_suggestion(
        _patch(
            {"name": "城市", "biz_name": "model", "description": "存储城市的房贷说明"},
            target_kind="model",
        ),
        _imported_model(),
        physical_comment="SeSQL 银行贷款：城市",
    )
    assert accepted


def test_a_hand_written_model_description_still_blocks_the_default():
    accepted = default_accept_for_suggestion(
        _patch({"description": "存储城市的房贷说明"}, target_kind="model"),
        _imported_model(description="运营手工维护的口径说明"),
        physical_comment="SeSQL 银行贷款：城市",
    )
    assert not accepted


def test_a_field_description_equal_to_the_column_comment_is_a_placeholder():
    current = _fresh_field(description="所在地区")
    patch = _patch({"description": "客户所在的销售大区"})

    assert default_accept_for_suggestion(patch, current, physical_comment="所在地区")
    # 不给注释上下文时保持保守:当成人工内容,不默认覆盖。
    assert not default_accept_for_suggestion(patch, current)


def test_physical_comments_map_models_and_fields_from_the_snapshot():
    from datetime import UTC, datetime

    from knowflow_analytics.modeling.contracts import (
        SchemaColumnSnapshot,
        SchemaSnapshot,
        TableSnapshot,
    )
    from knowflow_analytics.modeling.proposal_defaults import physical_comments_for

    snapshot = SchemaSnapshot.create(
        database_name="db",
        captured_at=datetime(2026, 8, 24, tzinfo=UTC),
        tables=(
            TableSnapshot(
                schema_name="bench_6",
                name="城市",
                comment="SeSQL 银行贷款：城市",
                columns=(
                    SchemaColumnSnapshot(
                        name="名称",
                        data_type="TEXT",
                        nullable=True,
                        comment="城市名",
                        ordinal_position=0,
                    ),
                    SchemaColumnSnapshot(
                        name="平均房价（万）",
                        data_type="NUMERIC",
                        nullable=True,
                        comment="",
                        ordinal_position=1,
                    ),
                ),
            ),
        ),
    )
    model = ModelSpec(id="model:bench_6:城市", name="城市", table="城市", schema_name="bench_6")
    named = FieldSpec(
        id="field:bench_6:城市:名称",
        model_id=model.id,
        name="名称",
        column="名称",
        data_type="TEXT",
    )
    silent = FieldSpec(
        id="field:bench_6:城市:平均房价（万）",
        model_id=model.id,
        name="平均房价（万）",
        column="平均房价（万）",
        data_type="NUMERIC",
    )

    comments = physical_comments_for((model,), (named, silent), snapshot)

    assert comments[model.id] == "SeSQL 银行贷款：城市"
    assert comments[named.id] == "城市名"
    assert silent.id not in comments  # 空注释不进映射
