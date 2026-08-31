from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    DimensionSpec,
    FilterOperator,
    FixedFilter,
    JoinType,
    MetricKind,
    MetricSpec,
    ModelSpec,
    OutputColumn,
    PhysicalQuery,
    SemanticQuery,
    SemanticQueryType,
    SemanticRelease,
)
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.modeling.analysis_topics import route_relation_ids_for_models
from knowflow_analytics.modeling.semantic_expression import render_semantic_expression
from knowflow_analytics.modeling.sql_model import compile_sql_model_source
from knowflow_analytics.modeling.type_system import aggregation_accepts_type, render_time_bound
from knowflow_analytics.semantic.join_planner import JoinPlanner, PlannedRelation

_FORMULA_REF_RE = re.compile(r"\{([A-Za-z0-9_.:-]+)\}")


@dataclass
class _ParameterBuilder:
    values: dict[str, Any]

    def add(self, value: Any) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f":{name}"


class _InlineLiteralBuilder(_ParameterBuilder):
    """把值渲染成 SQL 字面量而不是占位符。

    文本路径在指标展开阶段还没有 ``_ParameterBuilder``,而外层树的字面量随后
    统一由 ``_parameterize_literals`` 参数化。用这个替身跑同一个
    ``_fixed_filter_sql``,可以让两条路径共用一套过滤渲染语义(日期归一化、
    类型处理都在里面),而不是各写一份然后漂移——本轮修的半可加守卫和维度
    默认值就是同一件事两份实现漂移出来的。
    """

    def add(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, (int, float, Decimal)):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"


METRIC_TIME_COLUMN = "__kf_metric_time"
METRIC_TIME_AXIS_COLUMN = "__kf_axis"
METRIC_TIME_AXIS_SOURCE = "__kf_axis_src"


def _metric_time_axis_plan(
    indexes: _ReleaseIndexes,
    *,
    query: SemanticQuery,
    governed_metrics: tuple[MetricSpec, ...],
    dataset: DatasetSpec,
) -> tuple[str, ...] | None:
    """本次查询是否使用逻辑时间轴,以及要展开哪几根物理轴。

    只有当查询真的引用了 ``metric_time_axis`` 维度时才展开;否则保持单表扫描,
    现有行为完全不变。展开的轴取自选中指标声明的 ``agg_time_dimension_id``,
    未声明的指标落到数据集默认轴——它们本来就按那根轴统计。
    """

    referenced = {*query.dimension_ids, *(item.dimension_id for item in query.filters)}
    if not any(
        (dimension := indexes.dimensions.get(item)) is not None and dimension.metric_time_axis
        for item in referenced
    ):
        return None
    axes: set[str] = set()
    for metric in _expand_atomic_dependencies(indexes, governed_metrics):
        declared = metric.agg_time_dimension_id or dataset.default_time_dimension_id
        if declared is not None:
            axes.add(declared)
    return tuple(sorted(axes)) if len(axes) > 1 else None


class SemanticTranslator:
    """Translate a governed semantic query without exposing joins to the LLM."""

    def translate(self, *, release: SemanticRelease, query: SemanticQuery) -> PhysicalQuery:
        indexes = _ReleaseIndexes(release)
        dataset = indexes.dataset(query.dataset_id)
        metrics = [indexes.metric_for_dataset(dataset, item) for item in query.metric_ids]
        measure_filter_metrics = [
            (item, indexes.metric_for_dataset(dataset, item.metric_id))
            for item in query.measure_filters
        ]
        metric_filter_metrics = [
            (item, indexes.metric_for_dataset(dataset, item.metric_id))
            for item in query.metric_filters
        ]
        requested_aggregations = {
            item.metric_id: item.aggregation for item in query.aggregation_overrides
        }
        effective_aggregations = {
            metric.id: indexes.effective_metric_aggregation(
                metric,
                requested_aggregations.get(metric.id),
            )
            for metric in dict.fromkeys((*metrics, *(item[1] for item in metric_filter_metrics)))
        }
        dimensions = [indexes.dimension_for_dataset(dataset, item) for item in query.dimension_ids]
        filters = [
            (item, indexes.dimension_for_dataset(dataset, item.dimension_id))
            for item in query.filters
        ]

        governed_metrics = tuple(
            dict.fromkeys(
                (
                    *metrics,
                    *(item[1] for item in measure_filter_metrics),
                    *(item[1] for item in metric_filter_metrics),
                )
            )
        )
        _reject_collapsed_non_additive_dimensions(
            indexes,
            query_type=query.query_type,
            grouped_dimension_ids=query.dimension_ids,
            governed_metrics=governed_metrics,
            effective_aggregations=effective_aggregations,
        )
        metric_models = {
            model_id for metric in governed_metrics for model_id in indexes.metric_model_ids(metric)
        }
        if len(metric_models) > 1:
            raise TranslationError(
                "V0 does not combine metrics from different fact models",
                code="CROSS_FACT_METRICS_UNSUPPORTED",
            )
        projection_models = {item.model_id for item in dimensions}
        projection_models.update(dimension.model_id for _, dimension in filters)
        required_models = metric_models | projection_models
        if not required_models:
            # 与文本路径同一治理码。结构化入口的空投影在 SemanticQuery 契约层
            # 就被拒了,这里是纵深防御:真走到时也要说「没落到受治理字段」,
            # 而不是默认码 TRANSLATION_FAILED 的「翻译挂了」。
            raise TranslationError(
                "query resolved to no governed field",
                code="EMPTY_ONTOLOGY_PROJECTION",
            )
        anchor_model_id = next(iter(metric_models or sorted(required_models)))
        dataset_model_ids = set(dataset.model_ids)
        scoped_relations = tuple(
            relation
            for relation in release.relations
            if relation.left_model_id in dataset_model_ids
            and relation.right_model_id in dataset_model_ids
        )
        fanout_safe = (
            query.query_type is SemanticQueryType.AGGREGATE and bool(governed_metrics)
        ) and all(
            indexes.metric_is_fanout_safe(
                metric,
                aggregation_override=effective_aggregations.get(metric.id),
            )
            for metric in governed_metrics
        )
        planner = JoinPlanner(scoped_relations)
        bound_route = route_relation_ids_for_models(
            release,
            dataset_id=dataset.id,
            required_model_ids=required_models,
        )
        if bound_route is None:
            relation_plan = planner.plan(
                anchor_model_id=anchor_model_id,
                required_model_ids=required_models,
                has_metrics=bool(governed_metrics),
                fanout_safe=fanout_safe,
            )
        else:
            route_root_model_id, route_relation_ids = bound_route
            if metric_models and metric_models != {route_root_model_id}:
                raise TranslationError(
                    "analysis topic metrics do not belong to its fact root",
                    code="ANALYSIS_TOPIC_FACT_ROOT_MISMATCH",
                )
            anchor_model_id = route_root_model_id
            relation_plan = planner.plan_explicit(
                anchor_model_id=anchor_model_id,
                relation_ids=route_relation_ids,
                required_model_ids=required_models,
                has_metrics=bool(governed_metrics),
                fanout_safe=fanout_safe,
            )

        aliases = self._aliases(anchor_model_id, relation_plan)
        projection_aliases = _projection_aliases((*query.dimension_ids, *query.metric_ids))
        parameters = _ParameterBuilder({})
        select_parts: list[str] = []
        group_parts: list[str] = []
        columns: list[OutputColumn] = []

        metric_filter_sets = tuple(
            filter_set
            for metric in governed_metrics
            for filter_set in indexes.metric_leaf_filter_sets(metric)
        )
        distinct_filter_sets = {
            _fixed_filter_signature(filter_set) for filter_set in metric_filter_sets
        }
        # 口径一致时沿用共享 WHERE:语义等价、更省,现有行为完全不变。口径不同
        # 时逐指标下推到聚合函数内部——放进共享 WHERE 会让一个指标的过滤连带
        # 砍掉另一个,实测「即时预订占比」恒等于 1.0:可执行、看着正常、是错的。
        mixed_filter_scopes = len(distinct_filter_sets) > 1
        common_metric_filters = (
            ()
            if mixed_filter_scopes
            else (metric_filter_sets[0] if metric_filter_sets else ())
        )

        def render_metric_scope(filters: tuple[FixedFilter, ...]) -> str:
            return " AND ".join(
                self._fixed_filter_sql(
                    item,
                    indexes,
                    aliases,
                    parameters,
                    timezone=dataset.timezone,
                )
                for item in filters
            )

        scope_renderer = render_metric_scope if mixed_filter_scopes else None
        axis_plan = _metric_time_axis_plan(
            indexes,
            query=query,
            governed_metrics=governed_metrics,
            dataset=dataset,
        )

        def axis_predicate_for(metric: MetricSpec) -> str | None:
            if axis_plan is None:
                return None
            declared = metric.agg_time_dimension_id or dataset.default_time_dimension_id
            if declared is None or declared not in axis_plan:
                return None
            return (
                f"{_quote(aliases[anchor_model_id])}.{_quote(METRIC_TIME_AXIS_COLUMN)}"
                f" = {parameters.add(declared)}"
            )

        for dimension_index, dimension in enumerate(dimensions):
            expression = (
                f"{_quote(aliases[anchor_model_id])}.{_quote(METRIC_TIME_COLUMN)}"
                if dimension.metric_time_axis and axis_plan is not None
                else indexes.dimension_sql(dimension, aliases)
            )
            select_parts.append(f"{expression} AS {_quote(projection_aliases[dimension_index])}")
            group_parts.append(expression)
            columns.append(
                OutputColumn(element_id=dimension.id, name=dimension.name, kind="dimension")
            )
        metric_aliases: dict[str, str] = {}
        for metric_index, metric in enumerate(metrics, start=len(dimensions)):
            if query.query_type is SemanticQueryType.DETAIL:
                expression = indexes.detail_metric_sql(metric, aliases)
            else:
                expression = indexes.metric_sql(
                    metric,
                    aliases,
                    aggregation_override=effective_aggregations[metric.id],
                    render_filters=scope_renderer,
                    axis_predicate=axis_predicate_for(metric),
                )
            physical_alias = projection_aliases[metric_index]
            select_parts.append(f"{expression} AS {_quote(physical_alias)}")
            metric_aliases[metric.id] = physical_alias
            columns.append(OutputColumn(element_id=metric.id, name=metric.name, kind="metric"))

        anchor = indexes.models[anchor_model_id]
        anchor_source = self._model_source_sql(anchor, indexes, parameters)
        if axis_plan is not None:
            # 每根轴一个分支,附带对齐时间列与轴标记。指标表达式仍引用
            # ``m0.<列>``,轴的区分交给 CASE WHEN——不必改字段解析。
            branches = []
            for axis_id in axis_plan:
                axis_dimension = indexes.dimensions[axis_id]
                axis_column = _quote(indexes.fields[axis_dimension.field_id].column)
                branches.append(
                    f"SELECT {_quote(METRIC_TIME_AXIS_SOURCE)}.*, "
                    f"{_quote(METRIC_TIME_AXIS_SOURCE)}.{axis_column} "
                    f"AS {_quote(METRIC_TIME_COLUMN)}, "
                    f"{parameters.add(axis_id)} AS {_quote(METRIC_TIME_AXIS_COLUMN)} "
                    f"FROM {anchor_source} AS {_quote(METRIC_TIME_AXIS_SOURCE)}"
                )
            anchor_source = "(" + " UNION ALL ".join(branches) + ")"
        sql_parts = [
            "SELECT DISTINCT " if dimensions and not metrics else "SELECT ",
            ", ".join(select_parts),
            " FROM ",
            anchor_source,
            f" AS {_quote(aliases[anchor_model_id])}",
        ]
        for edge in relation_plan:
            sql_parts.append(self._join_sql(edge, indexes, aliases, parameters))

        where_parts: list[str] = []
        for fixed_filter in common_metric_filters:
            where_parts.append(
                self._fixed_filter_sql(
                    fixed_filter,
                    indexes,
                    aliases,
                    parameters,
                    timezone=dataset.timezone,
                )
            )
        for query_filter, dimension in filters:
            field = indexes.fields[dimension.field_id]
            # 逻辑轴的值来自按轴展开的对齐列。用它的 field_id(指向锚点轴)会让
            # 两个指标又共用一根物理时间列——实测过滤会渲染成锚点轴的列。
            field_sql = (
                f"{_quote(aliases[anchor_model_id])}.{_quote(METRIC_TIME_COLUMN)}"
                if dimension.metric_time_axis and axis_plan is not None
                else indexes.dimension_sql(dimension, aliases)
            )
            value = _normalize_temporal_filter_value(
                query_filter.value,
                # 维度可以单独声明类型(建模者比自动探测更知道列里存的是什么),
                # 缺失时才回落物理列类型。
                data_type=dimension.data_type or field.data_type,
                timezone=dataset.timezone,
                temporal=dimension.semantic_type == "time",
                date_format=dimension.date_format,
            )
            where_parts.append(
                self._filter_sql(query_filter.operator, value, field_sql, parameters)
            )
        for query_filter, metric in measure_filter_metrics:
            field_sql = indexes.detail_metric_sql(metric, aliases)
            where_parts.append(
                self._filter_sql(
                    query_filter.operator,
                    query_filter.value,
                    field_sql,
                    parameters,
                )
            )
        if where_parts:
            sql_parts.append(" WHERE " + " AND ".join(f"({item})" for item in where_parts))
        if (
            query.query_type is SemanticQueryType.AGGREGATE
            and group_parts
            and (metrics or metric_filter_metrics)
        ):
            sql_parts.append(" GROUP BY " + ", ".join(group_parts))
        if metric_filter_metrics:
            having_parts = []
            for query_filter, metric in metric_filter_metrics:
                expression = indexes.metric_sql(
                    metric,
                    aliases,
                    aggregation_override=effective_aggregations[metric.id],
                    render_filters=scope_renderer,
                    axis_predicate=axis_predicate_for(metric),
                )
                having_parts.append(
                    self._filter_sql(
                        query_filter.operator,
                        query_filter.value,
                        expression,
                        parameters,
                    )
                )
            sql_parts.append(" HAVING " + " AND ".join(f"({item})" for item in having_parts))

        available_order = {item.id for item in metrics} | {item.id for item in dimensions}
        order_aliases = {
            **{
                dimension.id: projection_aliases[index]
                for index, dimension in enumerate(dimensions)
            },
            **metric_aliases,
        }
        if query.order_by:
            order_parts = []
            for item in query.order_by:
                if item.element_id not in available_order:
                    raise TranslationError(
                        f"order element is not projected: {item.element_id}",
                        code="INVALID_ORDER_ELEMENT",
                    )
                order_parts.append(
                    f"{_quote(order_aliases[item.element_id])} {item.direction.value.upper()}"
                )
            sql_parts.append(" ORDER BY " + ", ".join(order_parts))
        elif dimensions:
            sql_parts.append(
                " ORDER BY "
                + ", ".join(f"{_quote(order_aliases[item.id])} ASC" for item in dimensions)
            )

        applied_defaults: list[str] = []
        limit = query.limit
        if limit is None:
            limit = (
                dataset.max_limit
                if query.query_type is SemanticQueryType.DETAIL
                else dataset.default_limit
            )
            applied_defaults.append("limit")
        if limit > dataset.max_limit:
            raise TranslationError(
                f"limit exceeds dataset maximum {dataset.max_limit}",
                code="QUERY_LIMIT_EXCEEDED",
            )
        sql_parts.append(f" LIMIT {limit + 1}")
        sql = "".join(sql_parts)
        try:
            sqlglot.parse_one(sql, read="postgres")
        except sqlglot.errors.ParseError as exc:
            raise TranslationError("translator produced invalid PostgreSQL SQL") from exc

        return PhysicalQuery(
            release_id=release.id,
            dataset_id=dataset.id,
            sql=sql,
            parameters=parameters.values,
            columns=tuple(columns),
            relation_ids=tuple(edge.relation.id for edge in relation_plan),
            applied_defaults=tuple(applied_defaults),
            result_limit=limit,
        )

    @staticmethod
    def _aliases(
        anchor_model_id: str, relation_plan: tuple[PlannedRelation, ...]
    ) -> dict[str, str]:
        aliases = {anchor_model_id: "m0"}
        for edge in relation_plan:
            aliases.setdefault(edge.to_model_id, f"m{len(aliases)}")
            aliases.setdefault(edge.from_model_id, f"m{len(aliases)}")
        return aliases

    @staticmethod
    def _join_sql(
        edge: PlannedRelation,
        indexes: _ReleaseIndexes,
        aliases: dict[str, str],
        parameters: _ParameterBuilder,
    ) -> str:
        relation = edge.relation
        target = indexes.models[edge.to_model_id]
        join_type = relation.join_type
        if edge.from_model_id != relation.left_model_id:
            join_type = {
                JoinType.LEFT: JoinType.RIGHT,
                JoinType.RIGHT: JoinType.LEFT,
            }.get(join_type, join_type)
        join_keyword = {
            JoinType.LEFT: "LEFT JOIN",
            JoinType.INNER: "INNER JOIN",
            JoinType.RIGHT: "RIGHT JOIN",
            JoinType.FULL: "FULL JOIN",
        }[join_type]
        conditions = []
        for condition in relation.conditions:
            left = indexes.fields[condition.left_field_id]
            right = indexes.fields[condition.right_field_id]
            conditions.append(
                f"{indexes.field_sql(left.id, aliases)} = {indexes.field_sql(right.id, aliases)}"
            )
        return (
            f" {join_keyword} {SemanticTranslator._model_source_sql(target, indexes, parameters)}"
            f" AS {_quote(aliases[target.id])}"
            f" ON {' AND '.join(conditions)}"
        )

    @staticmethod
    def _model_source_sql(
        model: ModelSpec,
        indexes: _ReleaseIndexes,
        parameters: _ParameterBuilder,
    ) -> str:
        source = (
            compile_sql_model_source(model.sql_query or "", model.sql_variables)
            if model.query_type == "sql_query"
            else _qualified_table(model)
        )
        if not model.filters:
            return source
        predicates = []
        for fixed_filter in model.filters:
            field = indexes.fields[fixed_filter.field_id]
            predicates.append(
                SemanticTranslator._filter_sql(
                    fixed_filter.operator,
                    fixed_filter.value,
                    _quote(field.column),
                    parameters,
                )
            )
        return (
            f"(SELECT * FROM {source} WHERE "
            + " AND ".join(f"({item})" for item in predicates)
            + ")"
        )

    def _fixed_filter_sql(
        self,
        fixed_filter: FixedFilter,
        indexes: _ReleaseIndexes,
        aliases: dict[str, str],
        parameters: _ParameterBuilder,
        *,
        timezone: str,
        field_sql: str | None = None,
    ) -> str:
        """渲染一条固定过滤。

        ``field_sql`` 用于文本路径:那里的外层树引用的是 ``__kf_field_N`` 令牌列
        而不是物理列。把列的写法作为参数传入,两条路径就能共用同一套算子映射
        和时间归一化,而不是各写一份 FixedFilter 渲染再漂移。
        """

        field = indexes.fields[fixed_filter.field_id]
        return self._filter_sql(
            fixed_filter.operator,
            _normalize_temporal_filter_value(
                fixed_filter.value,
                data_type=field.data_type,
                timezone=timezone,
                temporal=_is_temporal_data_type(field.data_type),
            ),
            indexes.field_sql(field.id, aliases) if field_sql is None else field_sql,
            parameters,
        )

    @staticmethod
    def _filter_sql(
        operator: FilterOperator,
        value: Any,
        field_sql: str,
        parameters: _ParameterBuilder,
    ) -> str:
        binary = {
            FilterOperator.EQ: "=",
            FilterOperator.NE: "<>",
            FilterOperator.GT: ">",
            FilterOperator.GTE: ">=",
            FilterOperator.LT: "<",
            FilterOperator.LTE: "<=",
            FilterOperator.LIKE: "LIKE",
        }
        if value is None:
            if operator is FilterOperator.EQ:
                return f"{field_sql} IS NULL"
            if operator is FilterOperator.NE:
                return f"{field_sql} IS NOT NULL"
            if operator not in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
                raise TranslationError(
                    f"{operator.value} filter cannot compare with NULL",
                    code="INVALID_NULL_FILTER",
                )
        if operator in binary:
            return f"{field_sql} {binary[operator]} {parameters.add(value)}"
        if operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            if value is not None:
                raise TranslationError(f"{operator.value} filter cannot carry a value")
            return f"{field_sql} IS {'NOT ' if operator is FilterOperator.IS_NOT_NULL else ''}NULL"
        if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            if not isinstance(value, (list, tuple)) or not value:
                raise TranslationError(f"{operator.value} filter requires a non-empty list")
            if any(item is None for item in value):
                raise TranslationError(
                    f"{operator.value} filter cannot contain NULL",
                    code="INVALID_NULL_FILTER",
                )
            placeholders = ", ".join(parameters.add(item) for item in value)
            keyword = "NOT IN" if operator is FilterOperator.NOT_IN else "IN"
            return f"{field_sql} {keyword} ({placeholders})"
        if operator is FilterOperator.BETWEEN:
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise TranslationError("between filter requires exactly two values")
            if any(item is None for item in value):
                raise TranslationError(
                    "between filter cannot contain NULL",
                    code="INVALID_NULL_FILTER",
                )
            return f"{field_sql} BETWEEN {parameters.add(value[0])} AND {parameters.add(value[1])}"
        raise TranslationError(f"unsupported filter operator: {operator}")


class _ReleaseIndexes:
    def __init__(self, release: SemanticRelease) -> None:
        self.models = {item.id: item for item in release.models}
        self.fields = {item.id: item for item in release.fields}
        self.dimensions = {item.id: item for item in release.dimensions}
        self.metrics = {item.id: item for item in release.metrics}
        self.datasets = {item.id: item for item in release.datasets}

    def dataset(self, item_id: str) -> DatasetSpec:
        try:
            return self.datasets[item_id]
        except KeyError as exc:
            raise TranslationError(f"unknown dataset: {item_id}", code="UNKNOWN_DATASET") from exc

    def metric_for_dataset(self, dataset: DatasetSpec, item_id: str) -> MetricSpec:
        if item_id not in dataset.metric_ids:
            raise TranslationError(f"metric is outside dataset: {item_id}", code="UNKNOWN_METRIC")
        return self.metrics[item_id]

    def dimension_for_dataset(self, dataset: DatasetSpec, item_id: str) -> DimensionSpec:
        if item_id not in dataset.dimension_ids:
            raise TranslationError(
                f"dimension is outside dataset: {item_id}", code="UNKNOWN_DIMENSION"
            )
        return self.dimensions[item_id]

    def field_sql(self, field_id: str, aliases: dict[str, str]) -> str:
        field = self.fields[field_id]
        alias = aliases.get(field.model_id)
        if alias is None:
            raise TranslationError(f"field model was not planned: {field_id}")
        return f"{_quote(alias)}.{_quote(field.column)}"

    def metric_model_ids(
        self, metric: MetricSpec, _seen: frozenset[str] = frozenset()
    ) -> set[str]:
        # 环保护:自引用公式在这里比在 _compile_formula 先炸栈。UI 作不出环
        # (来源列表排除自身),但伪造/导入的 release 不该把服务打成 RecursionError。
        # 中性返回会让空 model_ids 先撞上 EMPTY_ONTOLOGY_PROJECTION 这个误导性
        # 错误,这里直接给准确的。
        if metric.id in _seen:
            raise TranslationError(
                f"cyclic derived metric formula: {metric.id}",
                code="INVALID_METRIC_FORMULA",
            )
        if metric.kind is MetricKind.ATOMIC:
            return {metric.model_id}
        if metric.define_type in {"FIELD", "MEASURE"}:
            return {metric.model_id}
        model_ids: set[str] = set()
        for dependency in _formula_references(metric.formula or ""):
            model_ids.update(
                self.metric_model_ids(self.metrics[dependency], _seen | {metric.id})
            )
        return model_ids

    def detail_metric_sql(self, metric: MetricSpec, aliases: dict[str, str]) -> str:
        if metric.kind is not MetricKind.ATOMIC or metric.field_id is None:
            raise TranslationError(
                "detail query requires an atomic metric",
                code="DETAIL_DERIVED_METRIC_UNSUPPORTED",
            )
        if metric.aggregation in (Aggregation.COUNT, Aggregation.COUNT_DISTINCT):
            # 计数指标的「原始列」是标识(id),行级取值没有业务意义:明细投影它
            # 会把「销量最高的商品」翻成 ORDER BY id 取行,界面上标着指标名的
            # 数字其实是主键值(实测)。没有合法解释,fail-closed。
            raise TranslationError(
                "count metrics have no row-level value in a detail query",
                code="COUNT_METRIC_IN_DETAIL_QUERY",
            )
        return self.field_sql(metric.field_id, aliases)

    def dimension_sql(
        self,
        dimension: DimensionSpec,
        aliases: dict[str, str],
    ) -> str:
        if dimension.expression is None:
            return self.field_sql(dimension.field_id, aliases)
        fields_by_name = {
            self.fields[field_id].column.casefold(): field_id
            for field_id in dimension.expression_field_ids
        }
        return render_semantic_expression(
            dimension.expression,
            resolve_column=lambda name: self.field_sql(fields_by_name[name.casefold()], aliases),
            code="INVALID_DIMENSION_EXPRESSION",
        )

    def effective_metric_aggregation(
        self,
        metric: MetricSpec,
        requested: Aggregation | None,
    ) -> Aggregation | None:
        if requested is None:
            return metric.aggregation
        if metric.kind is MetricKind.DERIVED:
            # BaseSemanticCorrector uses SUM as the textual S2SQL marker for a
            # derived metric with null defaultAgg. metric_sql expands the governed
            # formula and therefore must not wrap that aggregate formula again.
            if requested is Aggregation.SUM:
                return requested
            raise TranslationError(
                "derived metrics cannot override aggregation",
                code="DERIVED_AGGREGATION_OVERRIDE_UNSUPPORTED",
            )
        if metric.aggregation is Aggregation.COUNT_DISTINCT:
            if requested is not Aggregation.COUNT_DISTINCT:
                raise TranslationError(
                    "COUNT DISTINCT metrics cannot override aggregation",
                    code="COUNT_DISTINCT_AGGREGATION_OVERRIDE_FORBIDDEN",
                )
            return metric.aggregation
        if metric.aggregation is Aggregation.COUNT:
            # 计数指标的语义是「数行」。SUM(计数) 在单层聚合里等价于 COUNT,
            # 规范化;其它聚合(avg/min/max over 行数)没有单层聚合语义,直译只会
            # 对物理 id 列做算术,fail-closed(实测 SUM 直译出 id 之和的静默错答)。
            if requested in (Aggregation.SUM, Aggregation.COUNT):
                return metric.aggregation
            raise TranslationError(
                "count metrics cannot override aggregation",
                code="COUNT_AGGREGATION_OVERRIDE_FORBIDDEN",
            )
        field = self.fields[metric.field_id or ""]
        if not aggregation_accepts_type(requested, field.data_type):
            raise TranslationError(
                f"{requested.value} cannot aggregate field type {field.data_type}",
                code="INVALID_AGGREGATION_TYPE",
            )
        return requested

    def metric_is_fanout_safe(
        self,
        metric: MetricSpec,
        *,
        aggregation_override: Aggregation | None = None,
        _seen: frozenset[str] = frozenset(),
    ) -> bool:
        """COUNT DISTINCT is invariant when a one-to-many join duplicates fact rows."""
        if metric.kind is MetricKind.ATOMIC:
            aggregation = aggregation_override or metric.aggregation
            return aggregation is Aggregation.COUNT_DISTINCT
        if metric.id in _seen:
            return True  # 环:中性返回,报错交给 _compile_formula
        dependencies = _formula_references(metric.formula or "")
        return bool(dependencies) and all(
            self.metric_is_fanout_safe(self.metrics[dependency], _seen=_seen | {metric.id})
            for dependency in dependencies
        )

    def metric_leaf_filter_sets(
        self,
        metric: MetricSpec,
        inherited: tuple[FixedFilter, ...] = (),
        _seen: frozenset[str] = frozenset(),
    ) -> tuple[tuple[FixedFilter, ...], ...]:
        if metric.id in _seen:
            return ()  # 环:中性返回,报错交给 _compile_formula
        current = _deduplicate_fixed_filters((*inherited, *metric.filters))
        if metric.kind is MetricKind.ATOMIC:
            return (current,)
        if metric.define_type in {"FIELD", "MEASURE"}:
            if metric.define_type == "FIELD":
                return (current,)
            return tuple(
                _deduplicate_fixed_filters((*current, *source.filters))
                for source in metric.expression_sources
            )
        return tuple(
            leaf_filters
            for dependency in _formula_references(metric.formula or "")
            for leaf_filters in self.metric_leaf_filter_sets(
                self.metrics[dependency],
                current,
                _seen=_seen | {metric.id},
            )
        )

    def metric_sql(
        self,
        metric: MetricSpec,
        aliases: dict[str, str],
        *,
        aggregation_override: Aggregation | None = None,
        inherited_filters: tuple[FixedFilter, ...] = (),
        render_filters: Callable[[tuple[FixedFilter, ...]], str] | None = None,
        axis_predicate: str | None = None,
        formula_stack: tuple[str, ...] = (),
    ) -> str:
        """把指标编译成 SQL 表达式。

        ``render_filters`` 非空时,指标自己的固定过滤下推进聚合函数内部
        (``SUM(CASE WHEN 口径 THEN 列 END)``)而不是进共享 WHERE。同一次查询里
        两个口径不同的指标(「即时预订额」与「总预订额」)因此可以同框——放进
        共享 WHERE 会让前者的过滤连带砍掉后者,实测占比恒等于 1.0。

        ``inherited_filters`` 沿派生公式向下传递,与 ``metric_leaf_filter_sets``
        的累积方式保持一致,否则包一层派生就能绕过口径。
        """

        scope = _deduplicate_fixed_filters((*inherited_filters, *metric.filters))

        def scoped(expression: str, filters: tuple[FixedFilter, ...]) -> str:
            predicates: list[str] = []
            # 逻辑时间轴:数据源按轴 UNION ALL 展开后,每个指标只统计属于自己那
            # 根轴的行。谓词与固定口径合并进同一个 CASE WHEN,不额外包一层。
            if axis_predicate is not None:
                predicates.append(axis_predicate)
            if filters and render_filters is not None:
                predicates.append(render_filters(filters))
            if not predicates:
                return expression
            return f"CASE WHEN {' AND '.join(predicates)} THEN {expression} END"

        if metric.kind is MetricKind.ATOMIC:
            field = scoped(self.field_sql(metric.field_id or "", aliases), scope)
            aggregation = aggregation_override or metric.aggregation
            if aggregation is Aggregation.COUNT_DISTINCT:
                return f"COUNT(DISTINCT {field})"
            functions = {
                Aggregation.SUM: "SUM",
                Aggregation.COUNT: "COUNT",
                Aggregation.AVG: "AVG",
                Aggregation.MIN: "MIN",
                Aggregation.MAX: "MAX",
            }
            function = functions.get(aggregation)
            if function is None:
                raise TranslationError(f"unsupported aggregation: {aggregation}")
            return f"{function}({field})"
        if metric.define_type == "FIELD":
            source_fields = {
                source.name.casefold(): source.field_id for source in metric.expression_sources
            }
            return render_semantic_expression(
                metric.formula or "",
                resolve_column=lambda name: self.field_sql(source_fields[name.casefold()], aliases),
                code="INVALID_FIELD_METRIC_EXPRESSION",
            )
        if metric.define_type == "MEASURE":
            # Exact pinned MetricExpressionParser.java:110-114 behavior. Each
            # selected measure is replaced by its aggregator wrapped around the
            # complete metric expression, not around that measure alone.
            sources = {source.name.casefold(): source for source in metric.expression_sources}
            raw_sources: dict[str, str] = {}
            for name, source in sources.items():
                source_sql = self.field_sql(source.field_id, aliases)
                if source.expression is not None:
                    expression_fields = {
                        self.fields[field_id].column.casefold(): field_id
                        for field_id in source.expression_field_ids
                    }
                    source_sql = render_semantic_expression(
                        source.expression,
                        resolve_column=lambda column, fields=expression_fields: self.field_sql(
                            fields[column.casefold()], aliases
                        ),
                        code="INVALID_MEASURE_EXPRESSION",
                    )
                raw_sources[name] = source_sql
            raw_metric_expression = render_semantic_expression(
                metric.formula or "",
                resolve_column=lambda name: raw_sources[name.casefold()],
                code="INVALID_MEASURE_METRIC_EXPRESSION",
            )
            return render_semantic_expression(
                metric.formula or "",
                resolve_column=lambda name: _aggregate_sql(
                    scoped(
                        raw_metric_expression,
                        _deduplicate_fixed_filters(
                            (*scope, *sources[name.casefold()].filters)
                        ),
                    ),
                    sources[name.casefold()].aggregation,
                ),
                code="INVALID_MEASURE_METRIC_EXPRESSION",
            )
        return _compile_formula(
            metric,
            self,
            aliases,
            inherited_filters=scope,
            render_filters=render_filters,
            axis_predicate=axis_predicate,
            formula_stack=formula_stack,
        )


def _expand_atomic_dependencies(
    indexes: _ReleaseIndexes,
    metrics: tuple[MetricSpec, ...],
) -> tuple[MetricSpec, ...]:
    """Flatten derived metrics down to the atomic metrics they aggregate.

    A non-additive declaration can only live on an atomic metric
    (``MetricSpec`` forbids it on derived ones), while the query only names the
    derived metric. Without expanding, wrapping a semi-additive metric in any
    derived formula bypasses the guard entirely. ``metric_model_ids`` and
    ``metric_is_fanout_safe`` already expand the same way.
    """

    seen: set[str] = set()
    expanded: list[MetricSpec] = []

    def visit(metric: MetricSpec) -> None:
        if metric.id in seen:
            return
        seen.add(metric.id)
        if metric.kind is MetricKind.ATOMIC:
            expanded.append(metric)
            return
        for dependency in _formula_references(metric.formula or ""):
            resolved = indexes.metrics.get(dependency)
            if resolved is not None:
                visit(resolved)

    for metric in metrics:
        visit(metric)
    return tuple(expanded)


def _reject_collapsed_non_additive_dimensions(
    indexes: _ReleaseIndexes,
    *,
    query_type: SemanticQueryType,
    grouped_dimension_ids: Collection[str],
    governed_metrics: tuple[MetricSpec, ...],
    effective_aggregations: Mapping[str, Aggregation | None],
) -> None:
    """半可加度量被跨其不可加维度聚合时拒答。

    余额、库存这类度量按其他维度可加，但沿声明维度相加没有业务含义：把某账户
    90 天的每日余额相加会得到一个看起来正常的错误数字。此处只拦"相加"语义
    （SUM/COUNT/AVG）；用户显式要 MIN/MAX 时问题本身是良定义的（"历史最低
    余额"），不做拦截。

    与 metric_is_fanout_safe 一致，选择确定性拒答而不是自动改写：改写要引入
    窗口函数并改变结果形态，在没有用户确认时同样可能给出非预期的数字。
    """

    if query_type is not SemanticQueryType.AGGREGATE:
        return
    selected_dimension_ids = set(grouped_dimension_ids)
    for metric in _expand_atomic_dependencies(indexes, governed_metrics):
        declaration = metric.non_additive_dimension
        if declaration is None:
            continue
        if declaration.dimension_id in selected_dimension_ids:
            # 按该维度分组后每组内不再跨维度叠加。
            continue
        aggregation = effective_aggregations.get(metric.id) or metric.aggregation
        if aggregation in {Aggregation.MIN, Aggregation.MAX}:
            continue
        dimension = indexes.dimensions.get(declaration.dimension_id)
        dimension_name = dimension.name if dimension is not None else declaration.dimension_id
        raise TranslationError(
            f"指标「{metric.name}」不能沿「{dimension_name}」聚合；"
            f"请按「{dimension_name}」分组，或改用最大值/最小值",
            code="NON_ADDITIVE_DIMENSION_COLLAPSED",
        )


def _formula_references(formula: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_FORMULA_REF_RE.findall(formula)))


def _aggregate_sql(field_sql: str, aggregation: Aggregation | None) -> str:
    if aggregation is Aggregation.COUNT_DISTINCT:
        return f"COUNT(DISTINCT {field_sql})"
    functions = {
        Aggregation.SUM: "SUM",
        Aggregation.COUNT: "COUNT",
        Aggregation.AVG: "AVG",
        Aggregation.MIN: "MIN",
        Aggregation.MAX: "MAX",
    }
    function = functions.get(aggregation)
    if function is None:
        raise TranslationError(
            "measure expression source has no supported aggregation",
            code="INVALID_MEASURE_METRIC_EXPRESSION",
        )
    return f"{function}({field_sql})"


def _projection_aliases(element_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return PostgreSQL-safe aliases without relying on silent 63-byte truncation."""

    aliases: list[str] = []
    used: set[str] = set()
    for index, element_id in enumerate(element_ids):
        candidate = element_id
        if "\x00" in candidate or len(candidate.encode("utf-8")) > 63 or candidate in used:
            candidate = f"column_{index}"
        while candidate in used:
            candidate = f"column_{index}_{len(used)}"
        aliases.append(candidate)
        used.add(candidate)
    return tuple(aliases)


def _guard_divisions(tree: exp.Expression) -> None:
    """派生公式里的除法分母统一包 NULLIF。

    两条翻译路径必须同义:0 分母出 NULL 行,而不是 Playground 出 NULL、客户问数
    直接数据库 division by zero 报错(实测文本路径此前无防护)。
    """

    for division in tree.find_all(exp.Div):
        denominator = division.expression
        if isinstance(denominator, exp.Nullif):
            continue
        if isinstance(denominator, exp.Anonymous) and denominator.name.upper() == "NULLIF":
            continue
        division.set(
            "expression",
            exp.Nullif(this=denominator.copy(), expression=exp.Literal.number(0)),
        )


def _compile_formula(
    metric: MetricSpec,
    indexes: _ReleaseIndexes,
    aliases: dict[str, str],
    *,
    inherited_filters: tuple[FixedFilter, ...] = (),
    render_filters: Callable[[tuple[FixedFilter, ...]], str] | None = None,
    axis_predicate: str | None = None,
    formula_stack: tuple[str, ...] = (),
) -> str:
    """派生公式与文本路径同一展开方式:token 替换 + sqlglot。

    此前这里是 Python ast 白名单,只认 + - * /,是三方(建模校验/结构化翻译/文本
    翻译)里唯一的异类:建模接受 CASE WHEN 等富标量,客户走的文本路径也原样翻译
    (实测端到端可用),只有 Playground 报 INVALID_METRIC_FORMULA——建模者据此误
    以为公式存不了。对齐到建模接受集,删掉第二份实现;建模侧校验器仍是唯一的
    准入门(禁聚合/窗口/子查询、引用必须等于依赖集)。
    """

    if metric.id in formula_stack:
        raise TranslationError(
            f"cyclic derived metric formula: {metric.id}", code="INVALID_METRIC_FORMULA"
        )
    formula = metric.formula or ""
    expression = formula
    for reference in _formula_references(formula):
        dependency = indexes.metrics.get(reference)
        if dependency is None:
            raise TranslationError(
                f"invalid derived metric formula: {metric.id}", code="INVALID_METRIC_FORMULA"
            )
        dependency_sql = indexes.metric_sql(
            dependency,
            aliases,
            inherited_filters=inherited_filters,
            render_filters=render_filters,
            axis_predicate=axis_predicate,
            formula_stack=(*formula_stack, metric.id),
        )
        expression = expression.replace(f"{{{reference}}}", f"({dependency_sql})")
    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise TranslationError(
            f"invalid derived metric formula: {metric.id}", code="INVALID_METRIC_FORMULA"
        ) from exc
    _guard_divisions(tree)
    return tree.sql(dialect="postgres")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(model: ModelSpec) -> str:
    return f"{_quote(model.schema_name)}.{_quote(model.table)}"


def _deduplicate_fixed_filters(filters: tuple[FixedFilter, ...]) -> tuple[FixedFilter, ...]:
    unique: dict[tuple[str, str, str], FixedFilter] = {}
    for item in filters:
        unique[(item.field_id, item.operator.value, repr(item.value))] = item
    return tuple(unique[key] for key in sorted(unique))


def _fixed_filter_signature(filters: tuple[FixedFilter, ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple((item.field_id, item.operator.value, repr(item.value)) for item in filters)


def _is_temporal_data_type(data_type: str) -> bool:
    normalized = data_type.casefold()
    return "date" in normalized or "time" in normalized


def _normalize_temporal_filter_value(
    value: Any,
    *,
    data_type: str,
    timezone: str,
    temporal: bool,
    date_format: str | None = None,
) -> Any:
    if not temporal or value is None:
        return value
    if isinstance(value, (list, tuple)):
        normalized = tuple(
            _normalize_temporal_filter_value(
                item,
                data_type=data_type,
                timezone=timezone,
                temporal=True,
                date_format=date_format,
            )
            for item in value
        )
        return normalized if isinstance(value, tuple) else list(normalized)
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime) or not isinstance(value, date):
        return value
    normalized_type = data_type.casefold()
    if "timestamp with time zone" in normalized_type or "timestamptz" in normalized_type:
        return datetime.combine(value, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    if "timestamp" in normalized_type:
        return datetime.combine(value, time.min)
    # 分区时间列常是 int(20260802)或 varchar:PG 对 integer >= date /
    # varchar >= date 直接报 operator does not exist,按建模期录入的格式渲染成
    # 字符串后两类列都能比较(未定型字面量会被强制转换)。对齐上游 TimeCorrector
    # 用 dateFormat 格式化它自动补上的时间区间。
    return render_time_bound(value, data_type=data_type, date_format=date_format)
