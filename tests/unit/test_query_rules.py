from __future__ import annotations

from datetime import UTC, datetime

from knowflow_analytics.contracts import QueryRuleMode, QueryRuleSpec, QueryRuleType
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import QueryRuleContract
from knowflow_analytics.query.rules import QueryRuleEngine


def test_add_date_recent_applies_only_when_s2sql_has_no_time_predicate(sales_release) -> None:
    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    release = sales_release.model_copy(
        update={
            "datasets": (dataset,),
            "query_rules": (
                QueryRuleSpec(
                    id="recent-seven-days",
                    dataset_id="sales_dataset",
                    priority=3,
                    rule_type=QueryRuleType.ADD_DATE,
                    mode=QueryRuleMode.RECENT,
                    parameters=(7,),
                ),
            ),
        }
    )
    engine = QueryRuleEngine()

    applied = engine.apply(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    explicit = engine.apply(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=(
            'SELECT SUM("净收入") FROM "销售经营" WHERE "下单日期" >= DATE \'2026-08-01\''
        ),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert applied.applied_rule_ids == ("recent-seven-days",)
    assert "2026-08-14" in applied.corrected_s2sql
    assert explicit.applied_rule_ids == ()
    assert "2026-08-14" not in explicit.corrected_s2sql


def test_add_select_exist_adds_governed_dimensions_and_grouping(sales_release) -> None:
    release = sales_release.model_copy(
        update={
            "query_rules": (
                QueryRuleSpec(
                    id="region-add-channel",
                    dataset_id="sales_dataset",
                    priority=2,
                    rule_type=QueryRuleType.ADD_SELECT,
                    mode=QueryRuleMode.EXIST,
                    parameters=("region",),
                    outputs=("channel",),
                ),
            ),
        }
    )

    applied = QueryRuleEngine().apply(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql=('SELECT "区域", SUM("净收入") FROM "销售经营" GROUP BY "区域"'),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert applied.applied_rule_ids == ("region-add-channel",)
    assert '"渠道"' in applied.corrected_s2sql
    assert "GROUP BY" in applied.corrected_s2sql


def test_only_highest_priority_add_date_rule_is_consumed(sales_release) -> None:
    dataset = sales_release.datasets[0].model_copy(
        update={"default_time_dimension_id": "order_date"}
    )
    release = sales_release.model_copy(
        update={
            "datasets": (dataset,),
            "query_rules": (
                QueryRuleSpec(
                    id="low",
                    dataset_id="sales_dataset",
                    priority=1,
                    rule_type=QueryRuleType.ADD_DATE,
                    mode=QueryRuleMode.RECENT,
                    parameters=(30,),
                ),
                QueryRuleSpec(
                    id="high",
                    dataset_id="sales_dataset",
                    priority=3,
                    rule_type=QueryRuleType.ADD_DATE,
                    mode=QueryRuleMode.RECENT,
                    parameters=(7,),
                ),
            ),
        }
    )

    applied = QueryRuleEngine().apply(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT SUM("净收入") FROM "销售经营"',
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert applied.applied_rule_ids == ("high",)
    assert "2026-08-14" in applied.corrected_s2sql
    assert "2026-07-22" not in applied.corrected_s2sql


def test_query_rules_round_trip_through_the_authoritative_catalog(sales_catalog) -> None:
    rule = QueryRuleSpec(
        id="region-add-channel",
        dataset_id="sales_dataset",
        priority=2,
        rule_type=QueryRuleType.ADD_SELECT,
        mode=QueryRuleMode.EXIST,
        parameters=("region",),
        outputs=("channel",),
    )

    contract = QueryRuleContract.model_validate(rule.model_dump(mode="python"))
    release = compile_semantic_catalog(
        sales_catalog.model_copy(update={"query_rules": (contract,)})
    )

    assert release.query_rules == (rule,)
    assert release.modeling_catalog["queryRules"][0]["id"] == "region-add-channel"
    assert release.modeling_catalog["queryRules"][0]["datasetId"] == "sales_dataset"
