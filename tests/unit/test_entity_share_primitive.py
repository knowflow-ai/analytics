"""「满足某个指标条件的实体占比」是一个受治理原语，不是一段要模型背下来的 CTE。

现场（2026-09-17）：「余额大于 2000 的账户占比」这类问题答不出来，补救是往提示词里
加了五行散文——先 WITH 按实体分组聚合，再外层 COUNT(CASE WHEN 聚合别名 > 阈值)，
「阈值必须作用在按实体聚合后的值上，不得直接对明细行判断」。

那句话在替一件正确性约束记账，而它没有任何治理关在守：模型把阈值打在明细行上
（``COUNT(DISTINCT 账号) WHERE 账户余额 > 2000``）是合法 SQL、执行成功、六道关全绿，
只是数字错了。而且从 SQL 上判不出来——明细查询按行过滤指标列本来就合法
（``test_multi_stage_shapes`` 钉着），坏的那条与合法形态在 SQL 层面没有可判定的区别。
区别只在「这是实体占比」这个意图，它从没被写进 SQL。

所以它只能是原语：模型写 ``ENTITY_SHARE(实体维度, 指标 比较 阈值)``，粒度归编译器。
展开与 ``RATIO_*`` 同一条路——先按实体（和外层分组）聚合到一层 CTE，再在实体粒度上
数达标的比例。散文规则随之从提示词里删掉。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
from knowflow_analytics.execution.guard import PhysicalSqlGuard
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.parser import _normalize_semantic_function_identifier_literals
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

_T = '"销售经营"'


def _translate(release, s2sql: str):
    return S2SqlSemanticTranslator().translate(
        release=release, dataset_id="sales_dataset", corrected_s2sql=s2sql
    )


def test_the_threshold_is_applied_after_aggregating_to_the_entity(sales_release) -> None:
    """先按实体聚合，再在实体粒度上比阈值——这一顺序由编译器保证，不再靠模型记住。"""

    translated = _translate(
        sales_release,
        f'SELECT ENTITY_SHARE("渠道", "净收入" > 1000) AS "_占比_" FROM {_T}',
    )

    sql = translated.physical_query.sql
    upper = sql.upper()
    # 内层：按实体分组，把指标聚合到实体粒度。
    assert "WITH" in upper
    assert "GROUP BY" in upper
    assert "SUM(" in upper
    # 外层：在聚合后的值上比阈值，数达标实体占全部实体的比例。
    assert "CASE WHEN" in upper
    assert "NULLIF" in upper
    assert "DOUBLE PRECISION" in upper
    # 阈值是参数，不是拼进 SQL 的字面量。
    assert 1000 in translated.physical_query.parameters.values()
    # 实体维度参与了路由（物理列进了 CTE 的 GROUP BY），但不是输出列——
    # dimension_ids 记的是投影维度，与 RATIO_TO_TOTAL 的范围维度同一待遇。
    assert '"channel"' in sql
    assert translated.dimension_ids == ()
    assert translated.metric_ids == ("net_revenue",)
    assert [item.kind for item in translated.physical_query.columns] == ["ratio"]
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_it_can_be_broken_down_by_another_dimension(sales_release) -> None:
    """「各区域里，净收入超过 1000 的渠道占比」：外层分组维度进内层一起分组。"""

    translated = _translate(
        sales_release,
        f'SELECT "区域", ENTITY_SHARE("渠道", "净收入" > 1000) AS "_占比_" '
        f'FROM {_T} GROUP BY "区域"',
    )

    upper = translated.physical_query.sql.upper()
    # 内层同时按区域与渠道分组，外层按区域分组。
    assert upper.count("GROUP BY") == 2
    assert translated.dimension_ids == ("region",)
    assert '"channel"' in translated.physical_query.sql
    assert [item.kind for item in translated.physical_query.columns] == ["dimension", "ratio"]


def test_a_row_filter_is_applied_before_the_entity_is_aggregated(sales_release) -> None:
    """「华东地区里，净收入超过 1000 的渠道占比」：过滤先决定哪些行入选，再聚合。"""

    translated = _translate(
        sales_release,
        f'SELECT ENTITY_SHARE("渠道", "净收入" > 1000) AS "_占比_" '
        f"FROM {_T} WHERE \"区域\" = '华东'",
    )

    sql = translated.physical_query.sql
    # WHERE 落在 CTE 内部（在 GROUP BY 之前），而不是外层。
    with_body = sql[sql.upper().index("WITH") : sql.upper().rindex(") SELECT")]
    assert "WHERE" in with_body.upper()
    assert "华东" in translated.physical_query.parameters.values()


@pytest.mark.parametrize(
    ("s2sql", "code"),
    [
        # 指标位已经自带一层不同的聚合：展开会变成 SUM(AVG(...))。
        (
            f'SELECT ENTITY_SHARE("渠道", AVG("净收入") > 1000) FROM {_T}',
            "S2SQL_RATIO_METRIC_PRE_AGGREGATED",
        ),
        # 第一个参数必须是受治理维度。
        (
            f'SELECT ENTITY_SHARE("净收入", "退款金额" > 1000) FROM {_T}',
            "S2SQL_RATIO_SCOPE_INVALID",
        ),
        # 第二个参数必须是「指标 比较 字面量」。
        (f'SELECT ENTITY_SHARE("渠道", "净收入") FROM {_T}', "S2SQL_RATIO_SHAPE_INVALID"),
        (
            f'SELECT ENTITY_SHARE("渠道", "净收入" > "退款金额") FROM {_T}',
            "S2SQL_RATIO_SHAPE_INVALID",
        ),
    ],
)
def test_malformed_calls_are_refused_not_guessed(sales_release, s2sql: str, code: str) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(sales_release, s2sql)

    assert raised.value.code == code


def test_writing_the_governed_aggregate_out_is_the_same_as_a_bare_metric(sales_release) -> None:
    """``SUM("净收入")`` 与 ``"净收入"`` 同义（净收入的治理聚合就是 SUM），与 RATIO_* 同一规则。"""

    bare = _translate(
        sales_release, f'SELECT ENTITY_SHARE("渠道", "净收入" > 1000) AS "_占比_" FROM {_T}'
    )
    written = _translate(
        sales_release, f'SELECT ENTITY_SHARE("渠道", SUM("净收入") > 1000) AS "_占比_" FROM {_T}'
    )

    assert written.physical_query.sql == bare.physical_query.sql


def test_single_quoted_member_names_are_normalized_like_ratio_to_total(sales_release) -> None:
    """模型会把目录里的单引号抄进参数位；与 RATIO_TO_TOTAL 同一条兼容规则。"""

    dataset = sales_release.datasets[0]
    normalized = _normalize_semantic_function_identifier_literals(
        "SELECT ENTITY_SHARE('渠道', '净收入' > 1000) FROM \"销售经营\"",
        release=sales_release,
        dataset=dataset,
    )

    assert 'ENTITY_SHARE("渠道", "净收入" > 1000)' in normalized
