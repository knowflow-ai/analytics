"""缓存键即失效规则。

建模页对着一个对象点「用数据核对」，结果要缓存下来，否则每次点开编辑器都去客户库
扫全表。难的是作废：按版本 ETag 绑，每保存一次所有标记一起变灰；按「改了哪里」比对，
总有漏判的一天，而漏判意味着把旧数字摆在改过的对象旁边——比不显示更糟。

所以键是内容寻址的：对象一变，键就变，旧结果根本查不到。这组测试钉住哪些改动该让
键变、哪些不该。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import SemanticCatalog
from knowflow_analytics.modeling.fact_checks import (
    FactCheckKind,
    FactCheckSubjectError,
    fact_subject_hash,
    metric_subject_id,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "modeling_contract_v1.json"
_SNAPSHOT = "sha256:snapshot"


@pytest.fixture
def release():
    return compile_semantic_catalog(
        SemanticCatalog.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))
    )


def _key(kind: FactCheckKind, subject_id: str, release, snapshot: str = _SNAPSHOT) -> str:
    return fact_subject_hash(kind, subject_id, release=release, schema_snapshot_hash=snapshot)


def _model_with_primary_identifier(release):
    for model in release.models:
        for field in release.fields:
            if field.model_id == model.id and field.identifier_type == "primary":
                return model, field
    raise AssertionError("夹具里至少有一个模型配置了主标识")


def _relation_touching(model_id: str, release):
    return next(
        item for item in release.relations if model_id in (item.left_model_id, item.right_model_id)
    )


def test_changing_the_primary_identifier_invalidates_the_grain_and_its_relations(release):
    """主标识换一列，粒度检查与碰这个模型的关系检查一起落空——它们的结论都变了。"""

    model, identifier = _model_with_primary_identifier(release)
    relation = _relation_touching(model.id, release)
    before = {
        "grain": _key(FactCheckKind.GRAIN, model.id, release),
        "relation": _key(FactCheckKind.RELATION, relation.id, release),
    }

    moved = release.model_copy(
        update={
            "fields": tuple(
                item.model_copy(update={"identifier_type": None})
                if item.id == identifier.id
                else item
                for item in release.fields
            )
        }
    )

    assert _key(FactCheckKind.GRAIN, model.id, moved) != before["grain"]
    # 关系检查读的是两端的行集，主标识不进它的 SQL——它的键不该因此变化。
    assert _key(FactCheckKind.RELATION, relation.id, moved) == before["relation"]


def test_changing_a_join_column_invalidates_that_relation(release):
    relation = release.relations[0]
    before = _key(FactCheckKind.RELATION, relation.id, release)
    joined_field_id = relation.conditions[0].left_field_id

    moved = release.model_copy(
        update={
            "fields": tuple(
                item.model_copy(update={"column": item.column + "_v2"})
                if item.id == joined_field_id
                else item
                for item in release.fields
            )
        }
    )

    assert _key(FactCheckKind.RELATION, relation.id, moved) != before


def test_renaming_things_invalidates_nothing(release):
    """改名不改数字。整版证据哈希也是这么判的，这里只是把同一条规则下沉到单个对象。"""

    model, _ = _model_with_primary_identifier(release)
    relation = _relation_touching(model.id, release)
    dataset = next(item for item in release.datasets if item.metric_ids)
    subject = metric_subject_id(dataset.id, dataset.metric_ids[0])
    before = {
        kind: _key(kind, value, release)
        for kind, value in (
            (FactCheckKind.GRAIN, model.id),
            (FactCheckKind.RELATION, relation.id),
            (FactCheckKind.METRIC, subject),
            (FactCheckKind.ROWS, model.id),
        )
    }

    renamed = release.model_copy(
        update={
            "models": tuple(
                item.model_copy(update={"name": item.name + "（改名）", "description": "新说明"})
                for item in release.models
            ),
            "metrics": tuple(
                item.model_copy(update={"name": item.name + "（改名）", "aliases": ("新说法",)})
                for item in release.metrics
            ),
            "fields": tuple(
                item.model_copy(update={"name": item.name + "（改名）"}) for item in release.fields
            ),
        }
    )

    for kind, value in (
        (FactCheckKind.GRAIN, model.id),
        (FactCheckKind.RELATION, relation.id),
        (FactCheckKind.METRIC, subject),
        (FactCheckKind.ROWS, model.id),
    ):
        assert _key(kind, value, renamed) == before[kind]


def test_a_different_database_is_a_different_key(release):
    """同一份目录接到另一个库上，量出来的东西不同名。"""

    model, _ = _model_with_primary_identifier(release)

    assert _key(FactCheckKind.GRAIN, model.id, release) != _key(
        FactCheckKind.GRAIN, model.id, release, snapshot="sha256:another"
    )


def test_an_unknown_subject_is_refused_instead_of_hashed(release):
    with pytest.raises(FactCheckSubjectError):
        _key(FactCheckKind.GRAIN, "model_that_does_not_exist", release)
    with pytest.raises(FactCheckSubjectError):
        _key(FactCheckKind.METRIC, "missing_separator", release)
