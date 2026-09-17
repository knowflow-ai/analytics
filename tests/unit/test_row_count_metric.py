"""行数指标：没有主标识的表也该答得出「有多少条」。

原先的默认计数只从**已确认的主标识**派生（`COUNT("主键列")`），理由是不能凭空发明
实体键——这条仍然成立。但它连带的后果是：没有主标识的表既没有默认计数，也进不了
任何作用域，整表字段问不到。于是建模者被逼着给每张表指一个主标识，而 AI 总能指出
一个来——客户现场 411/44224 那根列就是这么来的。

Cube 的做法是条件性的：cube 参与 join 且带可加度量时才强制主键，独立一张表照样可查。
行数指标就是那个「独立一张表照样可查」：数的是**这张表的行**，不假装它是任何实体。
一旦它被一对多连接放大，数出来的就不再是行数——那时必须 fail-closed（见扇出那组测试）。
"""

from __future__ import annotations

import pytest

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    FieldKind,
    FieldSpec,
    FilterOperator,
    FixedFilter,
    MetricKind,
    MetricSpec,
    ModelSpec,
    SemanticQuery,
    SemanticRelease,
)
from knowflow_analytics.modeling.catalog_compiler import compile_semantic_catalog
from knowflow_analytics.modeling.catalog_contracts import (
    MetricContract,
    MetricDefineByFieldParamsContract,
    MetricDefineType,
)
from knowflow_analytics.semantic.translator import SemanticTranslator, _ReleaseIndexes


def _release(*, metric: MetricSpec, extra_metrics: tuple[MetricSpec, ...] = ()) -> SemanticRelease:
    return SemanticRelease(
        id="release_rows",
        project_id="p",
        spec_hash="rows-v1",
        models=(ModelSpec(id="events", name="事件", schema_name="public", table="events"),),
        fields=(
            FieldSpec(
                id="events.amount",
                model_id="events",
                name="金额",
                column="amount",
                data_type="numeric",
                kind=FieldKind.MEASURE,
            ),
            FieldSpec(
                id="events.kind",
                model_id="events",
                name="类型",
                column="kind",
                data_type="text",
                kind=FieldKind.DIMENSION,
            ),
        ),
        metrics=(metric, *extra_metrics),
        datasets=(
            DatasetSpec(
                id="events_scope",
                name="事件分析",
                model_ids=("events",),
                metric_ids=(metric.id, *(item.id for item in extra_metrics)),
            ),
        ),
    )


def _row_count(**over) -> MetricSpec:
    return MetricSpec(
        id="events_count",
        name="事件数量",
        model_id="events",
        kind=MetricKind.ATOMIC,
        aggregation=Aggregation.COUNT,
        define_type="FIELD",
        **over,
    )


def test_a_row_count_metric_needs_no_column() -> None:
    """数的是行，不是某一列的取值——所以它没有 field_id，这不是缺失。"""

    metric = _row_count()

    assert metric.field_id is None
    assert metric.aggregation is Aggregation.COUNT


@pytest.mark.parametrize(
    "aggregation",
    [Aggregation.SUM, Aggregation.AVG, Aggregation.MAX, Aggregation.COUNT_DISTINCT],
    ids=lambda item: item.value,
)
def test_only_a_plain_count_may_omit_its_column(aggregation: Aggregation) -> None:
    """SUM 什么？AVG 什么？没有列的聚合只有一种有意义：数行。"""

    with pytest.raises(ValueError):
        MetricSpec(
            id="events_bad",
            name="坏指标",
            model_id="events",
            kind=MetricKind.ATOMIC,
            aggregation=aggregation,
            define_type="FIELD",
        )


def test_the_compiler_reads_count_star_as_a_row_count() -> None:
    """建模侧写 `COUNT(*)`，编译出来就是行数指标，不是一条看不懂的派生公式。"""

    contract = MetricContract(
        id="events_count",
        name="事件数量",
        biz_name="events_count",
        model_id="events",
        metric_define_type=MetricDefineType.FIELD,
        metric_define_by_field_params=MetricDefineByFieldParamsContract(
            expr="COUNT(*)",
            fields=(),
        ),
    )
    catalog = _catalog_with(contract)

    release = compile_semantic_catalog(catalog)
    metric = next(item for item in release.metrics if item.id == "events_count")

    assert metric.kind is MetricKind.ATOMIC
    assert metric.aggregation is Aggregation.COUNT
    assert metric.field_id is None


def test_it_translates_to_count_star() -> None:
    release = _release(metric=_row_count())
    translator = SemanticTranslator()

    physical = translator.translate(
        release=release,
        query=SemanticQuery(dataset_id="events_scope", metric_ids=("events_count",)),
    )

    assert "COUNT(*)" in physical.sql


def test_a_metric_filter_counts_ones_because_a_star_cannot_go_inside_case() -> None:
    """带口径的行数：`COUNT(CASE WHEN … THEN * END)` 不是合法 SQL，数 1 才是。

    同一次查询里两个口径不同的指标同框时，口径必须下推进聚合函数内部，否则一个
    指标的过滤会连带砍掉另一个（实测占比恒等于 1.0）。
    """

    filtered = _row_count(
        filters=(FixedFilter(field_id="events.kind", operator=FilterOperator.EQ, value="click"),),
    )
    plain = MetricSpec(
        id="events_amount",
        name="金额",
        model_id="events",
        kind=MetricKind.ATOMIC,
        field_id="events.amount",
        aggregation=Aggregation.SUM,
        define_type="FIELD",
    )
    release = _release(metric=filtered, extra_metrics=(plain,))
    translator = SemanticTranslator()

    physical = translator.translate(
        release=release,
        query=SemanticQuery(
            dataset_id="events_scope",
            metric_ids=("events_count", "events_amount"),
        ),
    )

    assert "THEN 1 END" in physical.sql
    assert "THEN * END" not in physical.sql


def test_a_row_count_is_not_fanout_safe() -> None:
    """一对多连接把每行复制三份，数出来就是三倍。只有 COUNT DISTINCT 不受影响。"""

    release = _release(metric=_row_count())
    indexes = _ReleaseIndexes(release, None, ())

    assert indexes.metric_is_fanout_safe(_row_count()) is False


def _catalog_with(metric: MetricContract):
    from knowflow_analytics.modeling.catalog_contracts import (
        ModelContract,
        ModelDetailContract,
        ModelFieldContract,
        SemanticCatalog,
    )

    return SemanticCatalog(
        project_id="p",
        revision_id="rev",
        models=(
            ModelContract(
                id="events",
                name="事件",
                biz_name="events",
                database_id="db",
                model_detail=ModelDetailContract(
                    query_type="table_query",
                    table_query="public.events",
                    fields=(
                        ModelFieldContract(field_name="amount", data_type="numeric"),
                        ModelFieldContract(field_name="kind", data_type="text"),
                    ),
                ),
            ),
        ),
        metrics=(metric,),
    )


def _sales_release_with_row_count(sales_release):
    """把销售夹具的订单计数换成行数指标，模拟一张没有主标识的表。"""

    from knowflow_analytics.contracts import AnalysisTopicRouteSpec

    metrics = tuple(
        item.model_copy(
            update={
                "field_id": None,
                "aggregation": Aggregation.COUNT,
                "formula": None,
                "expression_sources": (),
            }
        )
        if item.id == "order_count"
        else item
        for item in sales_release.metrics
    )
    routes = (
        AnalysisTopicRouteSpec(
            dataset_id="sales_dataset",
            root_model_id="orders",
            default_count_metric_id="order_count",
        ),
    )
    return sales_release.model_copy(update={"metrics": metrics, "analysis_topic_routes": routes})


def test_count_star_in_textual_s2sql_binds_to_the_row_count(sales_release) -> None:
    """受治理的 `COUNT(*)` 仍然只绑定默认计数——这次那个默认计数数的就是行。"""

    import knowflow_analytics.query.parser  # noqa: F401  先加载 query 包，避免循环导入
    from knowflow_analytics.semantic.s2sql_translator import S2SqlSemanticTranslator

    release = _sales_release_with_row_count(sales_release)

    translated = S2SqlSemanticTranslator().translate(
        release=release,
        dataset_id="sales_dataset",
        corrected_s2sql='SELECT COUNT(*) FROM "销售经营"',
    )

    assert "COUNT(*)" in translated.physical_query.sql
    assert "order_count" in translated.metric_ids
