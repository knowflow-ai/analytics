from __future__ import annotations

import pytest

from knowflow_analytics.hashing import semantic_evidence_hash


def _mutate(release, **changes):
    return release.model_copy(update=changes)


def test_cosmetic_edits_keep_the_evidence_hash_stable(sales_release) -> None:
    """改中文名/描述不影响关系基数、扇出或指标口径，不该作废 3 分钟的全表扫描。

    此前质量报告、评测和索引全绑 spec_hash，而 spec_hash 覆盖整个 release，
    改一个字就三项全废。
    """

    renamed = _mutate(
        sales_release,
        metrics=tuple(
            item.model_copy(update={"name": f"{item.name} 修订", "description": "新说明"})
            for item in sales_release.metrics
        ),
    )
    assert semantic_evidence_hash(renamed) == semantic_evidence_hash(sales_release)
    # 但 spec_hash 仍然变化，版本追溯不受影响
    assert renamed.metrics != sales_release.metrics


def test_alias_edits_keep_the_evidence_hash_stable(sales_release) -> None:
    """别名影响自然语言映射，不影响物理数据质量证据。"""

    aliased = _mutate(
        sales_release,
        dimensions=tuple(
            item.model_copy(update={"aliases": (*item.aliases, "新别名")})
            for item in sales_release.dimensions
        ),
    )
    assert semantic_evidence_hash(aliased) == semantic_evidence_hash(sales_release)


def test_changing_a_relation_cardinality_invalidates_the_evidence(sales_release) -> None:
    """基数决定扇出判定，改了它质量报告必须重跑。"""

    if not sales_release.relations:
        pytest.skip("fixture has no relations")
    flipped = _mutate(
        sales_release,
        relations=tuple(
            item.model_copy(update={"cardinality": "many_to_many"})
            for item in sales_release.relations
        ),
    )
    assert semantic_evidence_hash(flipped) != semantic_evidence_hash(sales_release)


def test_changing_a_metric_aggregation_invalidates_the_evidence(sales_release) -> None:
    """聚合方式直接决定数值对错。"""

    changed = _mutate(
        sales_release,
        metrics=tuple(
            item.model_copy(update={"aggregation": "max"}) if item.aggregation is not None else item
            for item in sales_release.metrics
        ),
    )
    assert semantic_evidence_hash(changed) != semantic_evidence_hash(sales_release)


def test_evidence_hash_is_deterministic(sales_release) -> None:
    assert semantic_evidence_hash(sales_release) == semantic_evidence_hash(sales_release)


def test_nested_semantic_fields_are_not_stripped_by_name() -> None:
    """按键名递归剥离会误删嵌套结构里同名但承载语义的字段。

    - DatasetTimeDefaultConfig.unit 是默认时间窗口的天数（parser.py 用它算
      WHERE 边界），不是展示单位；
    - ModelSpec.biz_name 是 S2SQL 的 FROM 业务名，参与符号解析；
    - DimensionValueSpec.display_name 参与精确维度值 grounding；
    - modeling_catalog 是原始上游 DTO，里面任意 name/format/unit 都不该被动。
    """

    from knowflow_analytics.hashing import _strip_cosmetic

    payload = {
        "datasets": [{"id": "d", "aggregate_time_default": {"unit": 7, "period": "DAY"}}],
        "models": [{"id": "m", "biz_name": "orders"}],
        "dimension_values": [{"id": "v", "display_name": "已支付"}],
        "modeling_catalog": {"metrics": [{"id": "x", "name": "n"}]},
    }
    out = _strip_cosmetic(payload)

    assert out["datasets"][0]["aggregate_time_default"]["unit"] == 7
    assert out["models"][0]["biz_name"] == "orders"
    assert out["dimension_values"][0]["display_name"] == "已支付"
    assert out["modeling_catalog"]["metrics"][0]["name"] == "n"


def test_changing_the_default_time_window_invalidates_the_evidence(sales_release) -> None:
    """默认时间窗口的 unit 是天数，参与 WHERE 边界计算（parser 的 _subtract_period）。

    把它从 7 天改成 365 天会改变物理 SQL，必须作废需要扫库的质量证据。
    此前按键名递归剥离会把这个 unit 当成展示单位删掉，导致证据不作废。
    """

    from knowflow_analytics.contracts import DatasetTimeDefaultConfig

    dataset = sales_release.datasets[0]
    seven_days = dataset.model_copy(
        update={"aggregate_time_default": DatasetTimeDefaultConfig(unit=7, period="DAY")}
    )
    a_year = dataset.model_copy(
        update={"aggregate_time_default": DatasetTimeDefaultConfig(unit=365, period="DAY")}
    )
    narrow = sales_release.model_copy(
        update={"datasets": (seven_days, *sales_release.datasets[1:])}
    )
    wide = sales_release.model_copy(update={"datasets": (a_year, *sales_release.datasets[1:])})
    assert semantic_evidence_hash(narrow) != semantic_evidence_hash(wide)
