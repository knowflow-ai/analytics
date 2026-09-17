"""外部标识是人要分组的那个东西，不是要藏的内部结构。

现场（2026-09-16）：「账户余额大于 10000 的人数占比情况」答不了。账户号（zhhao）是外部
标识，被「技术标识不得对外暴露」整条剔出可问范围，于是这张表**结构上就没有**可以按
账户分组的维度；模型退而抓了并集里另一张表的「账户代码」——那列其实是评分调整值
（15 个取值，-30、-20、-10、0、20……），两张表之间又一条关系都没有，跨事实根拒答。
拒答是对的，但重新生成永远修不好它，整问最后以超时收场。

主标识确实该藏：按它分组等于按行分组，没有业务含义（Cube 同样只隐藏 primaryKey）。
外部标识正相反——账户号、门店号就是人说「各账户」「各门店」时指的那个东西。
显式把维度语义类型标成 identifier 仍然隐藏：那是建模者亲口说「这列是技术字段」。
"""

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
    SemanticRelease,
)
from knowflow_analytics.modeling.analysis_topics import AnalysisTopicProposer


def _release(
    *,
    identifier_type: str | None = "foreign",
    semantic_type: str = "categorical",
) -> SemanticRelease:
    return SemanticRelease(
        id="release_ids",
        project_id="p",
        spec_hash="ids-v1",
        models=(ModelSpec(id="warns", name="账户模型预警", schema_name="public", table="warns"),),
        fields=(
            FieldSpec(
                id="warns.id",
                model_id="warns",
                name="预警记录ID",
                column="id",
                data_type="bigint",
                kind=FieldKind.IDENTIFIER,
                identifier_type="primary",
            ),
            FieldSpec(
                id="warns.zhhao",
                model_id="warns",
                name="账户号",
                column="zhhao",
                data_type="varchar(64)",
                kind=FieldKind.IDENTIFIER,
                identifier_type=identifier_type,
            ),
            FieldSpec(
                id="warns.balance",
                model_id="warns",
                name="账户余额",
                column="zhye",
                data_type="numeric",
                kind=FieldKind.MEASURE,
            ),
        ),
        dimensions=(
            DimensionSpec(
                id="dim_warn_id",
                model_id="warns",
                field_id="warns.id",
                name="预警记录ID",
                semantic_type="categorical",
            ),
            DimensionSpec(
                id="dim_account",
                model_id="warns",
                field_id="warns.zhhao",
                name="账户号",
                semantic_type=semantic_type,
            ),
        ),
        metrics=(
            MetricSpec(
                id="balance",
                name="账户余额",
                model_id="warns",
                kind=MetricKind.ATOMIC,
                field_id="warns.balance",
                aggregation=Aggregation.AVG,
                define_type="FIELD",
            ),
        ),
        datasets=(
            DatasetSpec(
                id="legacy",
                name="账户模型预警分析",
                model_ids=("warns",),
                metric_ids=("balance",),
                dimension_ids=("dim_account",),
            ),
        ),
    )


def _scope(release: SemanticRelease):
    proposals = AnalysisTopicProposer().propose(release)
    return next(item for item in proposals if item.route.root_model_id == "warns")


def test_a_foreign_identifier_can_be_grouped_by() -> None:
    """账户号进可问范围，「各账户的余额」「余额大于 1 万的账户占比」才有落点。"""

    scope = _scope(_release())

    assert "dim_account" in scope.dataset.dimension_ids


def test_the_primary_identifier_stays_hidden() -> None:
    """主标识是粒度键：按它分组等于按行分组，没有业务含义。"""

    scope = _scope(_release())

    assert "dim_warn_id" not in scope.dataset.dimension_ids
    assert any(
        item.element_id == "dim_warn_id" and item.reason_code == "technical_identifier"
        for item in scope.exclusions
    )


@pytest.mark.parametrize(
    ("identifier_type", "semantic_type", "reason"),
    [
        (None, "categorical", "角色未确认的标识列不猜"),
        ("foreign", "identifier", "建模者显式标成技术字段"),
    ],
)
def test_it_still_hides_what_should_stay_hidden(
    identifier_type: str | None, semantic_type: str, reason: str
) -> None:
    scope = _scope(_release(identifier_type=identifier_type, semantic_type=semantic_type))

    assert "dim_account" not in scope.dataset.dimension_ids, reason
