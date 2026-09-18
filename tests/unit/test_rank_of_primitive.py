"""「某个实体在全体中排第几」是一个受治理原语，不是一段要模型背下来的顺序约定。

提示词第 33 条说「必须先在未过滤的完整集合中完成聚合和排名，目标实体的过滤必须发生在
全量排名之后」。它在替一件正确性约束记账，而没有任何治理关守着：模型先 WHERE 到目标
实体再 RANK()，名次是在只剩一行的子集里算的——永远第一，SQL 合法、执行成功、数字正常。
从 SQL 上判不出来：WHERE 到底是「范围」（在华东内排）还是「目标」（华南排第几），
只有意图知道。所以它只能是原语：``RANK_OF(实体维度, 该实体的值, 指标)``——目标实体由
参数说明，剩下的 WHERE 一律当范围留在 CTE 里；先在全体上聚合、排名，最后才取目标。
分区取前 N 这类真正的组合写法仍走 RANK() OVER 窗口，那条不收。
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


def test_the_target_is_filtered_only_after_ranking_everyone(sales_release) -> None:
    translated = _translate(
        sales_release, f'SELECT RANK_OF("区域", \'华南\', "净收入") AS "_排名_" FROM {_T}'
    )

    sql = translated.physical_query.sql
    upper = sql.upper()
    assert "RANK() OVER" in upper
    # 先在全体上聚合与排名（CTE 里没有目标过滤），最后才在外层取目标实体。
    ranked_part = sql[: upper.rindex("SELECT")]
    assert "华南" not in ranked_part
    assert "华南" in translated.physical_query.parameters.values()
    assert '"region"' in sql
    assert translated.metric_ids == ("net_revenue",)
    assert translated.dimension_ids == ()
    assert [item.kind for item in translated.physical_query.columns] == ["calculation"]
    assert translated.physical_query.columns[0].name == "_排名_"
    PhysicalSqlGuard().validate(query=translated.physical_query, release=sales_release)


def test_a_remaining_where_is_the_scope_not_the_target(sales_release) -> None:
    """「在华东范围内，直营渠道排第几」：范围过滤进 CTE，先缩小全体再排名。"""

    translated = _translate(
        sales_release,
        f'SELECT RANK_OF("渠道", \'直营\', "净收入") AS "_排名_" FROM {_T} WHERE "区域" = \'华东\'',
    )

    sql = translated.physical_query.sql
    upper = sql.upper()
    scope_part = sql[: upper.index("RANK() OVER")]
    assert "WHERE" in scope_part.upper()
    assert "华东" in translated.physical_query.parameters.values()


def test_it_can_rank_within_each_group(sales_release) -> None:
    """「各区域里直营渠道排第几」：外层分组维度进 PARTITION BY。"""

    translated = _translate(
        sales_release,
        f'SELECT "区域", RANK_OF("渠道", \'直营\', "净收入") AS "_排名_" FROM {_T} GROUP BY "区域"',
    )

    upper = translated.physical_query.sql.upper()
    assert "PARTITION BY" in upper
    assert translated.dimension_ids == ("region",)
    kinds = [item.kind for item in translated.physical_query.columns]
    assert kinds == ["dimension", "calculation"]


@pytest.mark.parametrize(
    ("s2sql", "code"),
    [
        (f'SELECT RANK_OF("净收入", \'华南\', "退款金额") FROM {_T}', "S2SQL_RATIO_SCOPE_INVALID"),
        (f'SELECT RANK_OF("区域", "渠道", "净收入") FROM {_T}', "S2SQL_RATIO_SHAPE_INVALID"),
        (f'SELECT RANK_OF("区域", \'华南\') FROM {_T}', "S2SQL_RATIO_SHAPE_INVALID"),
        (
            f'SELECT RANK_OF("区域", \'华南\', AVG("净收入")) FROM {_T}',
            "S2SQL_RATIO_METRIC_PRE_AGGREGATED",
        ),
    ],
)
def test_malformed_calls_are_refused_not_guessed(sales_release, s2sql: str, code: str) -> None:
    with pytest.raises(SemanticParsingError) as raised:
        _translate(sales_release, s2sql)

    assert raised.value.code == code


def test_single_quoted_member_names_are_normalized(sales_release) -> None:
    normalized = _normalize_semantic_function_identifier_literals(
        "SELECT RANK_OF('区域', '华南', '净收入') FROM \"销售经营\"",
        release=sales_release,
        dataset=sales_release.datasets[0],
    )

    assert 'RANK_OF("区域", \'华南\', "净收入")' in normalized
