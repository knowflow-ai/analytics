"""别名草稿的覆盖面必须与「什么能被问到」是同一个判据。

现场（2026-09-17）：AI 自动建模整轮失败，
`AI_MODELING_ALIAS_REVIEW_INCOMPLETE: no alias draft for queryable resources:
[账户模型预警.流水号, 账户模型预警.账户号, 账户评分模型结果.账户代码, 账户评分模型结果.账户号]`。

两处判据在 2026-09-17 之前**结果上**一致，所以谁都没发现它们其实不是一回事：
生成端跳过 `field.kind is IDENTIFIER` 的维度，校验端跳过 `semantic_type == "identifier"`
的维度。标识列那时根本进不了作用域，校验端压根看不见它们，于是分歧不可见。

外部标识可分组之后它们进来了，生成端还在跳过——可问的资源没有别名草稿，整轮 AI 建模
被自己的完备性校验拒掉。判据必须只有一处。
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
from knowflow_analytics.modeling.ai_artifacts import queryable_alias_dimensions
from knowflow_analytics.modeling.analysis_topics import AnalysisTopicProposer


def _release(*, semantic_type: str = "categorical") -> SemanticRelease:
    return SemanticRelease(
        id="release_alias",
        project_id="p",
        spec_hash="alias-v1",
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
                identifier_type="foreign",
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


def _scope_dimension_ids(release: SemanticRelease) -> set[str]:
    proposal = next(
        item
        for item in AnalysisTopicProposer().propose(release)
        if item.route.root_model_id == "warns"
    )
    return set(proposal.dataset.dimension_ids)


def test_everything_a_scope_exposes_gets_an_alias_draft() -> None:
    """作用域开放了什么，就得给什么起别名——否则完备性校验会拒掉整轮建模。"""

    release = _release()

    assert _scope_dimension_ids(release) <= {
        item.id for item in queryable_alias_dimensions(release)
    }


def test_a_hidden_identifier_needs_no_alias() -> None:
    """主标识与显式技术字段不可问，不必为它们起名。"""

    release = _release()

    drafted = {item.id for item in queryable_alias_dimensions(release)}
    assert "dim_warn_id" not in drafted
    assert "dim_account" in drafted


@pytest.mark.parametrize("semantic_type", ["identifier"])
def test_an_explicitly_technical_dimension_stays_out(semantic_type: str) -> None:
    release = _release(semantic_type=semantic_type)

    drafted = {item.id for item in queryable_alias_dimensions(release)}
    assert "dim_account" not in drafted
    assert _scope_dimension_ids(release) <= drafted
