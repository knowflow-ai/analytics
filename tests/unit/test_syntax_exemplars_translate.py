"""内置语法样例必须每一条都能过翻译器；样例承担的是「组合写法」的教学。

提示词里曾有七条散文教组合写法——嵌套聚合要用 WITH、组内占比/排名/与均值比写分区窗口、
与整体比用标量子查询、集合运算可用、分区取前 N 用窗口、聚合条件写 HAVING。这些不是
治理约束（翻译器早就放行，``test_multi_stage_shapes`` 钉着），是方言教学。教学用样例
比用散文可靠：散文写错了没人知道，样例写错了这里先红。

样例用中性的虚构 schema（``示例数据集``），不带任何客户词汇；这里按同一 schema 建一份
发布版本，把每条样例原样送进翻译器。此前四条样例从未被翻译器核过。
"""

from __future__ import annotations

import pytest

import knowflow_analytics.query.parser  # noqa: F401  # 先加载 query 包，避免循环导入
from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    DimensionSpec,
    FieldKind,
    FieldSpec,
    MetricSpec,
    ModelSpec,
    SemanticRelease,
)
from knowflow_analytics.query.syntax_exemplars import SYNTAX_EXEMPLARS
from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator


def _field(name: str, column: str, kind: FieldKind, data_type: str | None = None) -> FieldSpec:
    return FieldSpec(
        id=f"visits.{column}",
        model_id="visits",
        name=name,
        column=column,
        kind=kind,
        **({"data_type": data_type} if data_type else {}),
    )


def _metric(metric_id: str, name: str, column: str) -> MetricSpec:
    return MetricSpec(
        id=metric_id,
        name=name,
        model_id="visits",
        field_id=f"visits.{column}",
        aggregation=Aggregation.SUM,
    )


@pytest.fixture
def exemplar_release() -> SemanticRelease:
    """与 ``syntax_exemplars.py`` 的虚构 schema 一一对应的发布版本。"""

    return SemanticRelease(
        id="release_exemplar_v1",
        project_id="exemplar",
        spec_hash="exemplar-v1",
        models=(ModelSpec(id="visits", name="访问记录", schema_name="demo", table="visits"),),
        fields=(
            _field("记录ID", "id", FieldKind.IDENTIFIER),
            _field("部门", "department", FieldKind.DIMENSION),
            _field("团队", "team", FieldKind.DIMENSION),
            _field("数据日期", "data_date", FieldKind.TIME, "date"),
            _field("访问次数", "visit_count", FieldKind.MEASURE, "numeric"),
            _field("访问人数", "visitor_count", FieldKind.MEASURE, "numeric"),
            _field("访问时长", "visit_duration", FieldKind.MEASURE, "numeric"),
        ),
        dimensions=(
            DimensionSpec(
                id="department", name="部门", model_id="visits", field_id="visits.department"
            ),
            DimensionSpec(id="team", name="团队", model_id="visits", field_id="visits.team"),
            DimensionSpec(
                id="data_date",
                name="数据日期",
                model_id="visits",
                field_id="visits.data_date",
                semantic_type="time",
            ),
        ),
        metrics=(
            _metric("visit_count", "访问次数", "visit_count"),
            _metric("visitor_count", "访问人数", "visitor_count"),
            _metric("visit_duration", "访问时长", "visit_duration"),
        ),
        datasets=(
            DatasetSpec(
                id="demo_dataset",
                name="示例数据集",
                model_ids=("visits",),
                metric_ids=("visit_count", "visitor_count", "visit_duration"),
                dimension_ids=("department", "team", "data_date"),
                default_limit=100,
                max_limit=1_000,
            ),
        ),
    )


@pytest.mark.parametrize(
    "exemplar", SYNTAX_EXEMPLARS, ids=[item["question"] for item in SYNTAX_EXEMPLARS]
)
def test_every_syntax_exemplar_translates(exemplar_release, exemplar: dict[str, str]) -> None:
    translated = S2SqlSemanticTranslator().translate(
        release=exemplar_release,
        dataset_id="demo_dataset",
        corrected_s2sql=exemplar["sql"],
    )

    assert translated.physical_query.sql


# 每一种此前靠散文教的组合写法，都必须有一条样例在教它。键是散文原来的意思，
# 值是样例 SQL 里必然出现的形状标志。
_SHAPES_TAUGHT_BY_EXAMPLE = {
    "嵌套聚合用 WITH": "WITH ",
    "计算列起下划线别名": 'AS "_',
    "占本组用分区窗口": "OVER (PARTITION BY ",
    "与整体比用标量子查询": "> (SELECT AVG(",
    "集合运算": " INTERSECT ",
    "分区取前 N 用窗口": "RANK() OVER (PARTITION BY ",
    "聚合条件写 HAVING": " HAVING ",
}


@pytest.mark.parametrize("shape", sorted(_SHAPES_TAUGHT_BY_EXAMPLE))
def test_each_shape_that_used_to_be_prose_has_an_exemplar(shape: str) -> None:
    marker = _SHAPES_TAUGHT_BY_EXAMPLE[shape]

    assert any(marker in item["sql"] for item in SYNTAX_EXEMPLARS), shape


def test_exemplars_never_use_a_wildcard_projection() -> None:
    """样例是模型照抄的对象；``SELECT *`` 在数据集上会被拒（EMPTY_ONTOLOGY_PROJECTION）。"""

    for item in SYNTAX_EXEMPLARS:
        assert "SELECT *" not in item["sql"], item["question"]
