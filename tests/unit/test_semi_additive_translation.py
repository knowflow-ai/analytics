from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    MetricKind,
    MetricSpec,
    ModelSpec,
    NonAdditiveDimension,
    SemanticQuery,
    SemanticQueryType,
    SemanticRelease,
)
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.semantic.translator import SemanticTranslator


def _release(*, non_additive: bool) -> SemanticRelease:
    """会员余额：每账户每日一条快照，按门店可加、按时间不可加。"""

    return SemanticRelease(
        id="members_release",
        project_id="members_project",
        spec_hash="members-fixture",
        models=(
            ModelSpec(
                id="members",
                name="会员账户",
                schema_name="analytics",
                table="member_balance_daily",
            ),
        ),
        fields=(
            FieldSpec(
                id="members.balance",
                model_id="members",
                name="余额",
                column="balance",
                data_type="numeric",
                kind=FieldKind.MEASURE,
            ),
            FieldSpec(
                id="members.stat_date",
                model_id="members",
                name="统计日期",
                column="stat_date",
                data_type="date",
                kind=FieldKind.TIME,
            ),
            FieldSpec(
                id="members.store_code",
                model_id="members",
                name="门店编码",
                column="store_code",
                kind=FieldKind.DIMENSION,
            ),
        ),
        dimensions=(
            DimensionSpec(
                id="stat_date",
                name="统计日期",
                model_id="members",
                field_id="members.stat_date",
                semantic_type="time",
                time_granularity="day",
            ),
            DimensionSpec(
                id="store_code",
                name="门店编码",
                model_id="members",
                field_id="members.store_code",
            ),
        ),
        metrics=(
            MetricSpec(
                id="member_balance",
                name="会员余额",
                model_id="members",
                field_id="members.balance",
                aggregation=Aggregation.SUM,
                non_additive_dimension=(
                    NonAdditiveDimension(
                        dimension_id="stat_date",
                        window_choice=Aggregation.MAX,
                    )
                    if non_additive
                    else None
                ),
            ),
        ),
        datasets=(
            DatasetSpec(
                id="members_dataset",
                name="会员分析",
                model_ids=("members",),
                metric_ids=("member_balance",),
                dimension_ids=("stat_date", "store_code"),
            ),
        ),
    )


def _translate(release: SemanticRelease, query: SemanticQuery):
    return SemanticTranslator().translate(release=release, query=query)


def test_summing_a_semi_additive_metric_across_time_is_refused() -> None:
    """这正是实测中返回错答的形态：跨时间求和会把整段区间的余额相加。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("member_balance",),
        dimension_ids=("store_code",),
    )
    with pytest.raises(TranslationError) as excinfo:
        _translate(_release(non_additive=True), query)
    assert excinfo.value.code == "NON_ADDITIVE_DIMENSION_COLLAPSED"
    # 报错必须点名是哪个维度，否则用户无从修正。
    assert "统计日期" in str(excinfo.value)


def test_grouping_by_the_non_additive_dimension_is_allowed() -> None:
    """按天分组时每组只有一条快照，求和没有跨时间叠加，属于正常查询。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("member_balance",),
        dimension_ids=("stat_date", "store_code"),
    )
    physical = _translate(_release(non_additive=True), query)
    assert "SUM" in physical.sql.upper()


def test_metric_without_the_declaration_keeps_the_previous_behaviour() -> None:
    """未声明的指标必须与补齐前逐字节一致，避免影响既有 Revision。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("member_balance",),
        dimension_ids=("store_code",),
    )
    before = _translate(_release(non_additive=False), query)
    assert "SUM" in before.sql.upper()


def test_detail_query_is_not_affected_by_additivity() -> None:
    """明细查询不做聚合，不存在跨维度相加的问题。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        query_type=SemanticQueryType.DETAIL,
        metric_ids=("member_balance",),
        dimension_ids=("store_code",),
    )
    physical = _translate(_release(non_additive=True), query)
    assert physical.sql


def test_an_explicit_boundary_override_is_allowed_across_time() -> None:
    """只有"相加"是错的。用户显式要 MIN/MAX 时问题本身是良定义的
    （"历史最低余额"），拒绝它属于过度拦截。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("member_balance",),
        dimension_ids=("store_code",),
        aggregation_overrides=({"metric_id": "member_balance", "aggregation": Aggregation.MIN},),
    )
    physical = _translate(_release(non_additive=True), query)
    assert "MIN" in physical.sql.upper()


def test_an_average_override_across_time_is_still_refused() -> None:
    """AVG 同样把多条快照混成一个值，仍属于静默错答。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("member_balance",),
        dimension_ids=("store_code",),
        aggregation_overrides=({"metric_id": "member_balance", "aggregation": Aggregation.AVG},),
    )
    with pytest.raises(TranslationError) as excinfo:
        _translate(_release(non_additive=True), query)
    assert excinfo.value.code == "NON_ADDITIVE_DIMENSION_COLLAPSED"


def test_declared_grain_reaches_the_parser_schema() -> None:
    """粒度声明必须进入最小 schema，否则模型仍只能从问句猜 DATE_TRUNC 粒度。"""

    from knowflow_analytics.query.parser import _dimension_payload

    release = _release(non_additive=False)
    payload = {item["name"]: item for item in _dimension_payload(release, release.datasets[0])}
    assert payload["统计日期"]["time_granularity"] == "day"
    # 非时间维度不带该键，避免给模型无意义的噪声。
    assert "time_granularity" not in payload["门店编码"]


def _with_derived(release: SemanticRelease) -> SemanticRelease:
    derived = MetricSpec(
        id="balance_x2",
        name="余额两倍",
        model_id="members",
        kind=MetricKind.DERIVED,
        formula="{member_balance} * 2",
    )
    return release.model_copy(
        update={
            "metrics": (*release.metrics, derived),
            "datasets": (
                release.datasets[0].model_copy(
                    update={"metric_ids": ("member_balance", "balance_x2")}
                ),
            ),
        }
    )


def test_a_derived_metric_cannot_bypass_the_non_additive_guard() -> None:
    """半可加声明只能挂在原子指标上，而 governed_metrics 不展开派生依赖，
    于是任何包一层的派生指标都能绕过拒答，返回跨时间相加的错误数字。

    对照 metric_is_fanout_safe / metric_model_ids —— 它们都会递归展开 formula。
    """

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("balance_x2",),
        dimension_ids=("store_code",),
    )
    with pytest.raises(TranslationError) as excinfo:
        _translate(_with_derived(_release(non_additive=True)), query)
    assert excinfo.value.code == "NON_ADDITIVE_DIMENSION_COLLAPSED"


def test_a_derived_metric_is_fine_when_grouped_by_that_dimension() -> None:
    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("balance_x2",),
        dimension_ids=("stat_date", "store_code"),
    )
    physical = _translate(_with_derived(_release(non_additive=True)), query)
    assert "SUM" in physical.sql.upper()


def test_a_derived_metric_over_an_additive_base_stays_allowed() -> None:
    """依赖本身可加时不该误伤。"""

    query = SemanticQuery(
        dataset_id="members_dataset",
        metric_ids=("balance_x2",),
        dimension_ids=("store_code",),
    )
    physical = _translate(_with_derived(_release(non_additive=False)), query)
    assert "SUM" in physical.sql.upper()


def _translate_text(release: SemanticRelease, sql: str):
    # 局部导入:``semantic.s2sql_translator`` 与 ``query`` 包之间有既有的循环
    # 导入,模块级先导它会炸。本文件不需要任何 query 模块,不为绕开而加无用导入。
    from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

    return S2SqlSemanticTranslator().translate(
        release=release, dataset_id="members_dataset", corrected_s2sql=sql
    )


def test_textual_path_refuses_summing_across_the_non_additive_dimension() -> None:
    """自然语言路径必须和结构化路径拒同一个问题。

    守卫原先只装在 ``SemanticTranslator`` 上,而客户走的是文本路径:实测同一个
    半可加指标,Playground 拒答、问句放行并生成跨日期的 SUM——守卫装在了没人
    走的门上。
    """

    with pytest.raises(TranslationError) as excinfo:
        _translate_text(_release(non_additive=True), 'SELECT SUM("会员余额") FROM "会员分析"')
    assert excinfo.value.code == "NON_ADDITIVE_DIMENSION_COLLAPSED"


def test_textual_path_allows_grouping_by_the_non_additive_dimension() -> None:
    translation = _translate_text(
        _release(non_additive=True),
        'SELECT "统计日期", SUM("会员余额") FROM "会员分析" GROUP BY "统计日期"',
    )
    assert translation.physical_query.sql


def test_textual_path_still_refuses_when_grouped_by_another_dimension() -> None:
    """按别的维度分组不解除限制:每组内仍跨日期叠加。"""

    with pytest.raises(TranslationError) as excinfo:
        _translate_text(
            _release(non_additive=True),
            'SELECT "门店编码", SUM("会员余额") FROM "会员分析" GROUP BY "门店编码"',
        )
    assert excinfo.value.code == "NON_ADDITIVE_DIMENSION_COLLAPSED"


def test_textual_path_keeps_metrics_without_the_declaration_working() -> None:
    translation = _translate_text(
        _release(non_additive=False), 'SELECT SUM("会员余额") FROM "会员分析"'
    )
    assert translation.physical_query.sql
