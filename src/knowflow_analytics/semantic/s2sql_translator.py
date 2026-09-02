from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import sqlglot
from sqlglot import exp

from knowflow_analytics.contracts import (
    Aggregation,
    DatasetSpec,
    FilterOperator,
    FixedFilter,
    MetricKind,
    MetricSpec,
    OutputColumn,
    PhysicalQuery,
    QueryAggregationOverride,
    QueryFilter,
    QueryMeasureFilter,
    QueryMetricFilter,
    QueryOrder,
    SemanticQuery,
    SemanticQueryType,
    SemanticRelease,
    SortDirection,
)
from knowflow_analytics.errors import TranslationError
from knowflow_analytics.execution.dialect import SqlDialect, render_physical_sql
from knowflow_analytics.modeling.analysis_topics import route_relation_ids_for_models
from knowflow_analytics.modeling.semantic_expression import render_semantic_expression
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.s2sql_ast import textual_query_type, validate_textual_s2sql
from knowflow_analytics.query.symbols import ResolvedSemanticSymbol, SemanticSymbolTable
from knowflow_analytics.semantic.join_planner import JoinPlanner
from knowflow_analytics.semantic.translator import (
    METRIC_TIME_AXIS_COLUMN,
    METRIC_TIME_COLUMN,
    SemanticTranslator,
    _deduplicate_fixed_filters,
    _fixed_filter_signature,
    _guard_divisions,
    _InlineLiteralBuilder,
    _ParameterBuilder,
    _quote,
    _reject_collapsed_non_additive_dimensions,
    _ReleaseIndexes,
)


@dataclass(frozen=True)
class S2SqlTranslation:
    physical_query: PhysicalQuery
    query_type: SemanticQueryType
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    corrected_s2sql: str
    parser_trace: tuple[str, ...]
    audit_query: SemanticQuery
    audit_complete: bool = True


@dataclass(frozen=True)
class _RatioCall:
    projection_index: int
    operator: str
    metric_id: str


@dataclass
class _QueryStatement:
    release: SemanticRelease
    dataset: DatasetSpec
    corrected_s2sql: str
    query_type: SemanticQueryType
    tree: exp.Query | None = None
    symbols: SemanticSymbolTable | None = None
    semantic_tokens: dict[str, ResolvedSemanticSymbol] = field(default_factory=dict)
    field_tokens: dict[str, str] = field(default_factory=dict)
    metric_ids: list[str] = field(default_factory=list)
    dimension_ids: list[str] = field(default_factory=list)
    projected_metric_ids: list[str] = field(default_factory=list)
    projected_dimension_ids: list[str] = field(default_factory=list)
    aggregation_overrides: dict[str, Aggregation] = field(default_factory=dict)
    bare_metric_ids: set[str] = field(default_factory=set)
    ratio_calls: list[_RatioCall] = field(default_factory=list)
    mixed_metric_filter_scopes: bool = False
    metric_time_axes: tuple[str, ...] = ()
    physical_query: PhysicalQuery | None = None
    # 列级白名单与行级过滤随语句走：这条链路在五个地方各建一次 _ReleaseIndexes，
    # 漏掉任何一个都是一条绕过权限的路径（RATIO 的自连接 CTE 就在其中之一）。
    visible_element_ids: frozenset[str] | None = None
    row_filters: Mapping[str, tuple[FixedFilter, ...]] = field(default_factory=dict)
    # 数据源的执行方言。整条链路上只有最后落成物理 SQL 的两处会读它——中间所有
    # sqlglot 调用处理的都是内部 S2SQL，那层必须固定在 postgres 记法上。
    dialect: SqlDialect = SqlDialect.POSTGRES


class _Parser(Protocol):
    name: str

    def parse(self, statement: _QueryStatement) -> None: ...


class _NoOpParser:
    def __init__(self, name: str) -> None:
        self.name = name

    def parse(self, statement: _QueryStatement) -> None:
        del statement


class _SqlQueryParser:
    name = "SqlQueryParser"

    def parse(self, statement: _QueryStatement) -> None:
        tree = validate_textual_s2sql(statement.corrected_s2sql)
        statement.tree = tree.copy()
        statement.symbols = SemanticSymbolTable.from_release(
            statement.release,
            dataset_id=statement.dataset.id,
        )
        self._validate_dataset_table(statement)
        _validate_set_operation_shape(statement.tree, statement.symbols)
        _rewrite_order_by_metric_projection(statement.tree)
        aliases = _alias_semantic_pairs(statement.tree, statement.symbols)
        branches = _set_query_selects(statement.tree)
        for projection_index, projection in enumerate(branches[0].expressions):
            functions = [
                item
                for item in projection.find_all(exp.Anonymous)
                if item.name.upper() in {"RATIO_ROLL", "RATIO_OVER", "RATIO_TO_TOTAL"}
            ]
            if len(functions) > 1 or (functions and len(branches) > 1):
                raise _invalid(
                    "one ratio expression is allowed per non-set projection",
                    code="S2SQL_RATIO_SHAPE_INVALID",
                )
            if functions:
                resolved = _semantic_pairs(functions[0], statement.symbols, aliases)
                metrics = [item for item in resolved if item.kind == "metric"]
                if len(metrics) != 1:
                    raise _invalid(
                        "ratio expression requires exactly one governed metric",
                        code="S2SQL_RATIO_METRIC_REQUIRED",
                    )
                statement.ratio_calls.append(
                    _RatioCall(
                        projection_index=projection_index,
                        operator=functions[0].name.upper(),
                        metric_id=metrics[0].id,
                    )
                )
        for select in _set_query_selects(statement.tree):
            for projection in select.expressions:
                ratio_scope_dimensions = _ratio_scope_dimension_ids(
                    projection,
                    statement.symbols,
                )
                for resolved in _semantic_pairs(projection, statement.symbols, aliases):
                    if resolved.kind == "dimension" and resolved.id in ratio_scope_dimensions:
                        continue
                    target = (
                        statement.projected_metric_ids
                        if resolved.kind == "metric"
                        else statement.projected_dimension_ids
                    )
                    if resolved.id not in target:
                        target.append(resolved.id)
        token_by_element: dict[tuple[str, str], str] = {}
        assert statement.symbols is not None
        for column in list(statement.tree.find_all(exp.Column)):
            if _is_projection_alias_reference(column, aliases, statement.symbols):
                continue
            resolved = statement.symbols.resolve_first(column.name)
            aggregation = _column_aggregation(column)
            normalized_wrapper: exp.Sum | None = None
            if resolved.kind == "metric" and aggregation is not None:
                # LLM 手滑把计数指标写成 SUM(件数):SUM(每行记 1 的计数)在单层
                # 聚合里就是 COUNT;照单全收会让物理层把指标展开成 SUM(物理 id),
                # 把「多少件」算成 id 之和(实测 2 → 3 静默错答)。2026-08-27
                # 评审合同要求审计覆盖与物理 AST 都回到治理 COUNT 口径。
                if aggregation is Aggregation.SUM:
                    governed = next(
                        (m.aggregation for m in statement.release.metrics if m.id == resolved.id),
                        None,
                    )
                    if governed in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}:
                        parent = column.parent
                        if not isinstance(parent, exp.Sum) or parent.this is not column:
                            raise _invalid(
                                "count metric SUM wrapper must contain only the metric",
                                code="S2SQL_COUNT_METRIC_WRAPPER_INVALID",
                            )
                        ancestor = parent.parent
                        while ancestor is not None and not isinstance(ancestor, exp.Select):
                            if isinstance(ancestor, exp.AggFunc):
                                raise _invalid(
                                    "count metric cannot be nested in another aggregate",
                                    code="S2SQL_COUNT_METRIC_WRAPPER_INVALID",
                                )
                            ancestor = ancestor.parent
                        aggregation = governed
                        normalized_wrapper = parent
                statement.aggregation_overrides.setdefault(resolved.id, aggregation)
            key = (resolved.kind, resolved.id)
            token = token_by_element.get(key)
            if token is None:
                token = f"__kf_semantic_{len(token_by_element)}"
                token_by_element[key] = token
                statement.semantic_tokens[token] = resolved
                target = (
                    statement.metric_ids if resolved.kind == "metric" else statement.dimension_ids
                )
                if resolved.id not in target:
                    target.append(resolved.id)
            replacement = exp.column(token, quoted=True)
            column.replace(replacement)
            if normalized_wrapper is not None:
                normalized_wrapper.replace(_aggregate_expression(replacement.copy(), aggregation))

    @staticmethod
    def _validate_dataset_table(statement: _QueryStatement) -> None:
        assert statement.tree is not None
        assert statement.symbols is not None
        cte_names = {_normalize(item.alias_or_name) for item in statement.tree.find_all(exp.CTE)}
        tables = list(statement.tree.find_all(exp.Table))
        dataset_tables = 0
        for table in tables:
            if table.db or table.catalog:
                raise _invalid("S2SQL cannot reference a physical schema or catalog")
            if statement.symbols.is_dataset(table.name):
                dataset_tables += 1
                continue
            if _normalize(table.name) not in cte_names:
                raise _invalid(f"S2SQL references an unknown logical table: {table.name}")
        if dataset_tables < 1:
            raise _invalid("S2SQL must query the selected governed DataSet")


class _DimExpressionParser:
    name = "DimExpressionParser"

    def parse(self, statement: _QueryStatement) -> None:
        assert statement.tree is not None
        dimensions = {item.id: item for item in statement.release.dimensions}
        fields = {item.id: item for item in statement.release.fields}
        for token, resolved in tuple(statement.semantic_tokens.items()):
            if resolved.kind != "dimension":
                continue
            dimension = dimensions[resolved.id]
            if dimension.metric_time_axis:
                # 逻辑轴与锚点轴共用 field_id,走 _field_token 会撞同一个令牌。
                # 它的值由内层按轴 UNION ALL 展开时产出,不来自任何单一物理列。
                statement.field_tokens.setdefault(METRIC_TIME_COLUMN, dimension.field_id)
                replacement = exp.column(METRIC_TIME_COLUMN, quoted=True)
            elif dimension.expression is None:
                replacement = _field_token_expression(statement, dimension.field_id)
            else:
                fields_by_name = {
                    fields[field_id].column.casefold(): field_id
                    for field_id in dimension.expression_field_ids
                }
                rendered = render_semantic_expression(
                    dimension.expression,
                    resolve_column=lambda name, fields_by_name=fields_by_name: _field_token_sql(
                        statement,
                        fields_by_name[name.casefold()],
                    ),
                    code="INVALID_DIMENSION_EXPRESSION",
                )
                replacement = sqlglot.parse_one(rendered, read="postgres")
            _replace_token(statement.tree, token, replacement)


class _DefaultDimValueParser:
    name = "DefaultDimValueParser"

    def parse(self, statement: _QueryStatement) -> None:
        assert statement.tree is not None
        target_selects = list(_set_query_selects(statement.tree))
        inspected_selects = list(statement.tree.find_all(exp.Select))
        if isinstance(statement.tree, exp.Select):
            inspected_selects.insert(0, statement.tree)
        # 逐个维度释放,不是全有全无。上游 ``DefaultDimValueParser`` 是
        # ``if (!isEmpty(whereFields)) return``——WHERE 里出现任何列(哪怕打在
        # 指标上、与该维度无关)就丢掉全部默认值,用户一问「北京地区销售额」,
        # 「只算有效订单」这类口径就静默消失,返回含废单的金额。这里刻意与
        # 上游分歧:只释放用户真正约束了的那个维度。出现在投影里也算释放,
        # 否则「各地区销售额」会被默认值锁死在单个地区。
        constrained_dimension_ids = set(statement.projected_dimension_ids)
        for select in dict.fromkeys(inspected_selects):
            where = select.args.get("where")
            if where is None:
                continue
            for column in where.find_all(exp.Column):
                resolved = statement.semantic_tokens.get(column.name)
                if resolved is not None and resolved.kind == "dimension":
                    constrained_dimension_ids.add(resolved.id)
        dimensions = {item.id: item for item in statement.release.dimensions}
        for dimension_id in statement.dataset.dimension_ids:
            dimension = dimensions[dimension_id]
            if not dimension.default_values:
                continue
            if dimension.id in constrained_dimension_ids:
                continue
            token = next(
                (
                    key
                    for key, resolved in statement.semantic_tokens.items()
                    if resolved.kind == "dimension" and resolved.id == dimension.id
                ),
                None,
            )
            if token is None:
                token = f"__kf_semantic_{len(statement.semantic_tokens)}"
                statement.semantic_tokens[token] = ResolvedSemanticSymbol(
                    kind="dimension",
                    id=dimension.id,
                    name=dimension.name,
                )
                statement.dimension_ids.append(dimension.id)
            condition = exp.In(
                this=exp.column(token, quoted=True),
                expressions=[exp.Literal.string(item) for item in dimension.default_values],
            )
            for select in target_selects:
                _append_where(select, condition.copy())


class _MetricExpressionParser:
    name = "MetricExpressionParser"

    def parse(self, statement: _QueryStatement) -> None:
        assert statement.tree is not None
        assert statement.symbols is not None
        metrics = {item.id: item for item in statement.release.metrics}
        # 「裸」= 该指标引用不在任何聚合函数内。展开时只给裸引用补治理聚合。
        # RATIO_* 里的指标由 _ratio_metric_aggregate 自行按治理聚合展开,
        # 这里再补一次会翻出 SUM(SUM(...)) 双重聚合。
        ratio_metric_ids = {call.metric_id for call in statement.ratio_calls}
        for token, resolved in statement.semantic_tokens.items():
            if resolved.kind != "metric" or resolved.id in ratio_metric_ids:
                continue
            metric = metrics[resolved.id]
            for column in statement.tree.find_all(exp.Column):
                if column.name != token:
                    continue
                if metric.aggregation in {
                    Aggregation.COUNT,
                    Aggregation.COUNT_DISTINCT,
                } and _inside_where(column):
                    raise _invalid(
                        "count metric cannot be used in WHERE; use HAVING for aggregate filters",
                        code="S2SQL_COUNT_METRIC_WHERE_INVALID",
                    )
                if _column_aggregation(column) is None:
                    statement.bare_metric_ids.add(resolved.id)
        # 口径判定必须早于展开:_metric_expression 要据此决定是否下推 CASE WHEN。
        indexes = _ReleaseIndexes(
            statement.release, statement.visible_element_ids, statement.row_filters
        )
        selected = tuple(
            metrics[resolved.id]
            for resolved in statement.semantic_tokens.values()
            if resolved.kind == "metric" and resolved.id in metrics
        )
        # 逻辑轴被引用时,内层按轴 UNION ALL 展开,每个指标只统计自己那根轴的行。
        if METRIC_TIME_COLUMN in statement.field_tokens:
            axes = {
                metric.agg_time_dimension_id or statement.dataset.default_time_dimension_id
                for metric in selected
            }
            axes.discard(None)
            if len(axes) > 1:
                statement.metric_time_axes = tuple(sorted(axes))
        statement.mixed_metric_filter_scopes = (
            len(
                {
                    _fixed_filter_signature(filter_set)
                    for metric in selected
                    for filter_set in indexes.metric_leaf_filter_sets(metric)
                }
            )
            > 1
        )
        for token, resolved in tuple(statement.semantic_tokens.items()):
            if resolved.kind != "metric":
                continue
            replacement = _metric_expression(statement, resolved.id, ())
            if metrics[resolved.id].kind is MetricKind.DERIVED:
                # LLM 习惯写 SUM("指标")。原子指标那层 SUM 就是治理聚合(裸列在
                # 内);派生公式自带聚合后的依赖,再包一层要么嵌套聚合报错,要么
                # ——此前的真实行为——依赖退化成裸列逐行算再求和:退款率算成
                # 「逐行 退款/收入 之和」,实测 0.1 对 0.0333(正确口径)。
                _replace_derived_token(statement.tree, token, replacement)
            else:
                _replace_token(statement.tree, token, replacement)
        _restore_missing_group_by(
            statement,
            force_aggregate=any(
                metrics[metric_id].aggregation in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}
                for metric_id in statement.bare_metric_ids
                if metric_id in metrics
            ),
        )
        _apply_detail_dimension_distinct(statement)

        for function in list(statement.tree.find_all(exp.Anonymous)):
            if function.name.upper() != "COUNT_DISTINCT" or len(function.expressions) != 1:
                continue
            function.replace(
                exp.Count(this=exp.Distinct(expressions=[function.expressions[0].copy()]))
            )

        count_stars = [
            item for item in statement.tree.find_all(exp.Count) if isinstance(item.this, exp.Star)
        ]
        bound_count_stars = [
            item
            for item in count_stars
            if _count_star_reads_governed_dataset(item, statement.symbols)
        ]
        if bound_count_stars:
            count_metric_id = _default_count_metric_id(statement.release, statement.dataset)
            if count_metric_id is None:
                # Reviewed QueryScope contract (2026-08-27): COUNT(*) denotes
                # only an explicit, governed count grain. A no-PK metric scope
                # may answer its published metrics, but neither a unique COUNT
                # candidate nor physical row count may invent entity semantics.
                raise _invalid(
                    "query scope has no governed default count metric",
                    code="S2SQL_DEFAULT_COUNT_METRIC_REQUIRED",
                )
            if count_metric_id not in statement.metric_ids:
                statement.metric_ids.append(count_metric_id)
            if any(_inside_select_projection(item) for item in bound_count_stars) and (
                count_metric_id not in statement.projected_metric_ids
            ):
                statement.projected_metric_ids.append(count_metric_id)
            metric = metrics[count_metric_id]
            assert metric.aggregation is not None
            statement.aggregation_overrides.setdefault(count_metric_id, metric.aggregation)
            raw = _metric_expression(statement, count_metric_id, ())
            replacement = _aggregate_expression(raw, metric.aggregation)
            for item in bound_count_stars:
                item.replace(replacement.copy())


class _MetricRatioParser:
    name = "MetricRatioParser"

    def parse(self, statement: _QueryStatement) -> None:
        if not statement.ratio_calls:
            return
        assert statement.tree is not None
        period_modes = {
            item.operator
            for item in statement.ratio_calls
            if item.operator in {"RATIO_ROLL", "RATIO_OVER"}
        }
        if len(period_modes) > 1:
            raise _invalid(
                "RATIO_OVER and RATIO_ROLL cannot be combined",
                code="S2SQL_RATIO_MIXED_MODES",
            )
        for call in statement.ratio_calls:
            if call.operator == "RATIO_TO_TOTAL":
                self._apply_total_ratio(statement, call)
        period_calls = [
            item for item in statement.ratio_calls if item.operator in {"RATIO_ROLL", "RATIO_OVER"}
        ]
        if period_calls:
            self._apply_period_ratio(statement, period_calls, next(iter(period_modes)))

    @staticmethod
    def _apply_total_ratio(statement: _QueryStatement, call: _RatioCall) -> None:
        assert statement.tree is not None
        select = _set_query_selects(statement.tree)[0]
        function = _ratio_function(select.expressions[call.projection_index], call.operator)
        aggregate = _ratio_metric_aggregate(statement, call, function)
        aggregate_sql = aggregate.sql(dialect="postgres")
        if len(function.expressions) == 1:
            if not _ratio_group_dimension_ids(statement, select):
                raise _invalid(
                    "group share requires at least one governed GROUP BY dimension",
                    code="S2SQL_RATIO_GROUP_REQUIRED",
                )
            expression_sql = (
                f"CAST(({aggregate_sql}) AS DOUBLE PRECISION) "
                f"/ NULLIF(SUM({aggregate_sql}) OVER (), 0)"
            )
        else:
            dimension = function.expressions[1]
            value = function.expressions[2]
            if not _is_ratio_scope_dimension(statement, dimension):
                raise _invalid(
                    "subset share requires one governed dimension",
                    code="S2SQL_RATIO_SCOPE_INVALID",
                )
            if not isinstance(value, (exp.Literal, exp.Boolean)):
                raise _invalid(
                    "subset share requires one literal dimension value",
                    code="S2SQL_RATIO_SCOPE_INVALID",
                )
            expression_sql = (
                f"CAST({aggregate_sql} FILTER (WHERE "
                f"{dimension.sql(dialect='postgres')} = {value.sql(dialect='postgres')}) "
                f"AS DOUBLE PRECISION) "
                f"/ NULLIF({aggregate_sql}, 0)"
            )
        replacement = sqlglot.parse_one(expression_sql, read="postgres")
        function.replace(replacement)

    @staticmethod
    def _apply_period_ratio(
        statement: _QueryStatement,
        calls: list[_RatioCall],
        mode: str,
    ) -> None:
        assert statement.tree is not None
        if (
            not isinstance(statement.tree, exp.Select)
            or statement.tree.args.get("with_") is not None
        ):
            raise _invalid(
                "period ratio requires one SELECT without an existing CTE",
                code="S2SQL_RATIO_SHAPE_INVALID",
            )
        select = statement.tree
        group = select.args.get("group")
        time_groups = _ratio_time_groups(statement, group)
        if len(time_groups) != 1:
            raise _invalid(
                "period ratio requires exactly one governed time grouping",
                code="S2SQL_RATIO_TIME_REQUIRED",
            )
        time_group, period = time_groups[0]
        group_expressions = tuple(group.expressions) if group is not None else ()
        base = select.copy()
        base.set("order", None)
        base.set("limit", None)
        projection_aliases: list[str] = []
        # 与比率并列的聚合列（「按年看净金额和它的同比」）只从当前期取值，
        # 不参与自连接对齐——把聚合值当对齐键，只会匹配到两期金额恰好
        # 相等的行，等于悄悄丢数据。
        aggregate_projections: set[int] = set()
        ratio_by_projection = {item.projection_index: item for item in calls}
        for index, projection in enumerate(tuple(base.expressions)):
            call = ratio_by_projection.get(index)
            if call is not None:
                function = _ratio_function(projection, call.operator)
                aggregate = _ratio_metric_aggregate(statement, call, function)
                alias = f"__kf_ratio_value_{index}"
                base.expressions[index].replace(exp.alias_(aggregate, alias, quoted=True))
                projection_aliases.append(alias)
                continue
            expression = projection.this if isinstance(projection, exp.Alias) else projection
            if not any(expression.sql() == item.sql() for item in group_expressions):
                if expression.find(exp.AggFunc) is None:
                    raise _invalid(
                        "period ratio projections must be grouped dimensions, "
                        "aggregates, or ratio metrics",
                        code="S2SQL_RATIO_SHAPE_INVALID",
                    )
                aggregate_projections.add(index)
            alias = projection.alias or f"__kf_ratio_group_{index}"
            base.expressions[index].replace(exp.alias_(expression.copy(), alias, quoted=True))
            projection_aliases.append(alias)

        time_projection = next(
            (
                index
                for index, projection in enumerate(select.expressions)
                if (projection.this if isinstance(projection, exp.Alias) else projection).sql()
                == time_group.sql()
            ),
            None,
        )
        if time_projection is None:
            raise _invalid(
                "period ratio time group must be projected",
                code="S2SQL_RATIO_TIME_REQUIRED",
            )
        interval = _ratio_interval(mode, period)
        current_alias = "__kf_current"
        previous_alias = "__kf_previous"
        time_alias = projection_aliases[time_projection]
        current_time = f"{_quote(current_alias)}.{_quote(time_alias)}"
        previous_time = f"{current_time} - INTERVAL '{interval}'"
        if mode == "RATIO_OVER" and period == "WEEK":
            previous_time = f"DATE_TRUNC('week', {previous_time})"
        join_parts = [f"{_quote(previous_alias)}.{_quote(time_alias)} = {previous_time}"]
        for index, alias in enumerate(projection_aliases):
            if (
                index == time_projection
                or index in ratio_by_projection
                or index in aggregate_projections
            ):
                continue
            join_parts.append(
                f"{_quote(previous_alias)}.{_quote(alias)} IS NOT DISTINCT FROM "
                f"{_quote(current_alias)}.{_quote(alias)}"
            )
        outer_projections: list[str] = []
        for index, projection in enumerate(select.expressions):
            call = ratio_by_projection.get(index)
            output_alias = projection.alias or projection_aliases[index]
            if call is None:
                outer_projections.append(
                    f"{_quote(current_alias)}.{_quote(projection_aliases[index])} "
                    f"AS {_quote(output_alias)}"
                )
                continue
            value = f"{_quote(current_alias)}.{_quote(projection_aliases[index])}"
            previous = f"{_quote(previous_alias)}.{_quote(projection_aliases[index])}"
            outer_projections.append(
                f"COALESCE(CAST((({value}) - ({previous})) AS DOUBLE PRECISION) "
                f"/ NULLIF(({previous}), 0), 0) "
                f"AS {_quote(output_alias)}"
            )
        ratio_sql = (
            f"WITH {_quote('__kf_ratio_base')} AS ({base.sql(dialect='postgres')}) "
            f"SELECT {', '.join(outer_projections)} "
            f"FROM {_quote('__kf_ratio_base')} AS {_quote(current_alias)} "
            f"LEFT JOIN {_quote('__kf_ratio_base')} AS {_quote(previous_alias)} "
            f"ON {' AND '.join(join_parts)} "
            # 时序升序：同比/环比是趋势，图和表都按时间从早到晚读。
            f"ORDER BY {_quote(current_alias)}.{_quote(time_alias)} ASC"
        )
        statement.tree = sqlglot.parse_one(ratio_sql, read="postgres")


class _OntologyQueryParser:
    name = "OntologyQueryParser"

    def parse(self, statement: _QueryStatement) -> None:
        assert statement.tree is not None
        if isinstance(statement.tree, exp.SetOperation):
            self._parse_set_operation(statement)
            return
        indexes = _ReleaseIndexes(
            statement.release, statement.visible_element_ids, statement.row_filters
        )
        metrics = [
            indexes.metric_for_dataset(statement.dataset, item) for item in statement.metric_ids
        ]
        dimensions = [
            indexes.dimension_for_dataset(statement.dataset, item)
            for item in statement.dimension_ids
        ]
        required_models = {
            indexes.fields[field_id].model_id for field_id in statement.field_tokens.values()
        }
        metric_models = {
            model_id for metric in metrics for model_id in indexes.metric_model_ids(metric)
        }
        required_models.update(metric_models)
        required_models.update(item.model_id for item in dimensions)
        if not required_models:
            # 一个受治理字段都没解析出来(如 SELECT 1)。对齐上游 SqlBuilder 在
            # dataModels 为空时抛治理错误:默认码 TRANSLATION_FAILED 等于告诉调用方
            # 「翻译挂了」,而真实情况是「这个问题没落到任何受治理字段上」。
            raise TranslationError(
                "query resolved to no governed field",
                code="EMPTY_ONTOLOGY_PROJECTION",
            )
        if len(metric_models) > 1:
            raise TranslationError(
                "V0 does not combine metrics from different fact models",
                code="CROSS_FACT_METRICS_UNSUPPORTED",
            )
        anchor_model_id = next(iter(metric_models or sorted(required_models)))
        dataset_models = set(statement.dataset.model_ids)
        planner = JoinPlanner(
            tuple(
                relation
                for relation in statement.release.relations
                if relation.left_model_id in dataset_models
                and relation.right_model_id in dataset_models
            )
        )
        fanout_safe = (
            statement.query_type is SemanticQueryType.AGGREGATE
            and bool(metrics)
            and all(indexes.metric_is_fanout_safe(metric) for metric in metrics)
        )
        bound_route = route_relation_ids_for_models(
            statement.release,
            dataset_id=statement.dataset.id,
            required_model_ids=required_models,
        )
        if bound_route is None:
            relation_plan = planner.plan(
                anchor_model_id=anchor_model_id,
                required_model_ids=required_models,
                has_metrics=bool(metrics),
                fanout_safe=fanout_safe,
            )
        else:
            anchor_model_id, relation_ids = bound_route
            if metric_models and metric_models != {anchor_model_id}:
                raise TranslationError(
                    "analysis topic metrics do not belong to its fact root",
                    code="ANALYSIS_TOPIC_FACT_ROOT_MISMATCH",
                )
            relation_plan = planner.plan_explicit(
                anchor_model_id=anchor_model_id,
                relation_ids=relation_ids,
                required_model_ids=required_models,
                has_metrics=bool(metrics),
                fanout_safe=fanout_safe,
            )
        translator = SemanticTranslator()
        aliases = translator._aliases(anchor_model_id, relation_plan)
        parameters = _ParameterBuilder({})
        anchor = indexes.models[anchor_model_id]
        from_sql = (
            f"{translator._model_source_sql(anchor, indexes, parameters)} "
            f"AS {_quote(aliases[anchor_model_id])}"
        )
        for edge in relation_plan:
            from_sql += translator._join_sql(edge, indexes, aliases, parameters)

        fixed_filter_sets = tuple(
            filter_set
            for metric in metrics
            for filter_set in indexes.metric_leaf_filter_sets(metric)
        )
        # 混合口径已在指标展开时下推进各自的聚合函数,这里不能再进共享 WHERE。
        fixed_filters = (
            ()
            if statement.mixed_metric_filter_scopes
            else (fixed_filter_sets[0] if fixed_filter_sets else ())
        )
        fixed_conditions: list[str] = []
        for fixed_filter in fixed_filters:
            fixed_conditions.append(
                translator._fixed_filter_sql(
                    fixed_filter,
                    indexes,
                    aliases,
                    parameters,
                    timezone=statement.dataset.timezone,
                )
            )

        result_limit = _result_limit(statement.tree, statement.dataset, statement.query_type)
        _parameterize_literals(statement.tree, parameters)
        statement.tree.set("limit", exp.Limit(expression=exp.Literal.number(result_limit + 1)))
        columns = _output_columns(statement)

        inner_table = "__kf_dataset"
        assert statement.symbols is not None
        for table in list(statement.tree.find_all(exp.Table)):
            if statement.symbols.is_dataset(table.name):
                table.replace(
                    exp.Table(
                        this=exp.to_identifier(inner_table, quoted=True),
                        alias=table.args.get("alias"),
                    )
                )
        where_sql = (
            " WHERE " + " AND ".join(f"({condition})" for condition in fixed_conditions)
            if fixed_conditions
            else ""
        )
        projections = [
            f"{indexes.field_sql(field_id, aliases)} AS {_quote(token)}"
            for token, field_id in statement.field_tokens.items()
            if token != METRIC_TIME_COLUMN
        ]
        if statement.metric_time_axes:
            # 每根轴一个分支:对齐时间列取该轴的物理列,轴标记供外层 CASE WHEN
            # 区分。分支数是轴数不是指标数,行数按轴数放大而非笛卡尔。
            branches = [
                "SELECT "
                + ", ".join(
                    [
                        *projections,
                        f"{indexes.field_sql(indexes.dimensions[axis_id].field_id, aliases)}"
                        f" AS {_quote(METRIC_TIME_COLUMN)}",
                        f"{parameters.add(axis_id)} AS {_quote(METRIC_TIME_AXIS_COLUMN)}",
                    ]
                )
                + f" FROM {from_sql}{where_sql}"
                for axis_id in statement.metric_time_axes
            ]
            inner_sql = " UNION ALL ".join(branches)
        else:
            inner_sql = f"SELECT {', '.join(projections)} FROM {from_sql}{where_sql}"
        inner_query = sqlglot.parse_one(inner_sql, read="postgres")
        ontology_cte = exp.CTE(
            this=inner_query,
            alias=exp.TableAlias(this=exp.to_identifier(inner_table, quoted=True)),
        )
        with_clause = statement.tree.args.get("with_")
        if with_clause is None:
            statement.tree.set("with_", exp.With(expressions=[ontology_cte]))
        else:
            with_clause.set("expressions", [ontology_cte, *with_clause.expressions])
        sql = render_physical_sql(statement.tree, statement.dialect)
        sql = re.sub(r"%\((p\d+)\)s", r":\1", sql)
        statement.physical_query = PhysicalQuery(
            release_id=statement.release.id,
            dataset_id=statement.dataset.id,
            sql=sql,
            parameters=parameters.values,
            columns=columns,
            relation_ids=tuple(edge.relation.id for edge in relation_plan),
            result_limit=result_limit,
        )

    @staticmethod
    def _parse_set_operation(statement: _QueryStatement) -> None:
        """Translate every SetOperationList branch with its own ontology route."""

        assert statement.tree is not None
        assert statement.symbols is not None
        branches = _set_query_selects(statement.tree)
        original = validate_textual_s2sql(statement.corrected_s2sql)
        original_branches = _set_query_selects(original)
        if len(branches) != len(original_branches):
            raise _invalid(
                "set operation branch structure changed during translation",
                code="S2SQL_SET_OPERATION_SHAPE_MISMATCH",
            )
        symbols = SemanticSymbolTable.from_release(
            statement.release,
            dataset_id=statement.dataset.id,
        )
        aliases = _alias_semantic_pairs(original, symbols)
        indexes = _ReleaseIndexes(
            statement.release, statement.visible_element_ids, statement.row_filters
        )
        parameters = _ParameterBuilder({})
        ontology_ctes: list[exp.CTE] = []
        relation_ids: list[str] = []
        for index, (branch, original_branch) in enumerate(
            zip(branches, original_branches, strict=True)
        ):
            branch_metric_ids = tuple(
                dict.fromkeys(
                    item.id
                    for item in _semantic_pairs(original_branch, symbols, aliases)
                    if item.kind == "metric"
                )
            )
            branch_field_tokens = {
                column.name: statement.field_tokens[column.name]
                for column in branch.find_all(exp.Column)
                if column.name in statement.field_tokens
            }
            inner_query, branch_relations = _ontology_inner_for_scope(
                statement,
                indexes=indexes,
                parameters=parameters,
                field_tokens=branch_field_tokens,
                metric_ids=branch_metric_ids,
            )
            inner_table = f"__kf_dataset_{index}"
            for table in list(branch.find_all(exp.Table)):
                if statement.symbols.is_dataset(table.name):
                    table.replace(
                        exp.Table(
                            this=exp.to_identifier(inner_table, quoted=True),
                            alias=table.args.get("alias"),
                        )
                    )
            ontology_ctes.append(
                exp.CTE(
                    this=inner_query,
                    alias=exp.TableAlias(this=exp.to_identifier(inner_table, quoted=True)),
                )
            )
            relation_ids.extend(branch_relations)

        result_limit = _result_limit(statement.tree, statement.dataset, statement.query_type)
        _parameterize_literals(statement.tree, parameters)
        statement.tree.set("limit", exp.Limit(expression=exp.Literal.number(result_limit + 1)))
        columns = _output_columns(statement)
        with_clause = statement.tree.args.get("with_")
        if with_clause is None:
            statement.tree.set("with_", exp.With(expressions=ontology_ctes))
        else:
            with_clause.set("expressions", [*ontology_ctes, *with_clause.expressions])
        sql = re.sub(
            r"%\((p\d+)\)s", r":\1", render_physical_sql(statement.tree, statement.dialect)
        )
        statement.physical_query = PhysicalQuery(
            release_id=statement.release.id,
            dataset_id=statement.dataset.id,
            sql=sql,
            parameters=parameters.values,
            columns=columns,
            relation_ids=tuple(dict.fromkeys(relation_ids)),
            result_limit=result_limit,
        )


def _ontology_inner_for_scope(
    statement: _QueryStatement,
    *,
    indexes: _ReleaseIndexes,
    parameters: _ParameterBuilder,
    field_tokens: dict[str, str],
    metric_ids: tuple[str, ...],
) -> tuple[exp.Query, tuple[str, ...]]:
    metrics = [indexes.metric_for_dataset(statement.dataset, item) for item in metric_ids]
    required_models = {indexes.fields[field_id].model_id for field_id in field_tokens.values()}
    metric_models = {
        model_id for metric in metrics for model_id in indexes.metric_model_ids(metric)
    }
    required_models.update(metric_models)
    if not required_models:
        raise TranslationError("set operation branch has no resolvable semantic model")
    if len(metric_models) > 1:
        raise TranslationError(
            "set operation branch combines different fact models",
            code="CROSS_FACT_METRICS_UNSUPPORTED",
        )
    anchor_model_id = next(iter(metric_models or sorted(required_models)))
    dataset_models = set(statement.dataset.model_ids)
    planner = JoinPlanner(
        tuple(
            relation
            for relation in statement.release.relations
            if relation.left_model_id in dataset_models
            and relation.right_model_id in dataset_models
        )
    )
    fanout_safe = bool(metrics) and all(indexes.metric_is_fanout_safe(metric) for metric in metrics)
    bound_route = route_relation_ids_for_models(
        statement.release,
        dataset_id=statement.dataset.id,
        required_model_ids=required_models,
    )
    if bound_route is None:
        relation_plan = planner.plan(
            anchor_model_id=anchor_model_id,
            required_model_ids=required_models,
            has_metrics=bool(metrics),
            fanout_safe=fanout_safe,
        )
    else:
        anchor_model_id, relation_ids = bound_route
        if metric_models and metric_models != {anchor_model_id}:
            raise TranslationError(
                "analysis topic metrics do not belong to its fact root",
                code="ANALYSIS_TOPIC_FACT_ROOT_MISMATCH",
            )
        relation_plan = planner.plan_explicit(
            anchor_model_id=anchor_model_id,
            relation_ids=relation_ids,
            required_model_ids=required_models,
            has_metrics=bool(metrics),
            fanout_safe=fanout_safe,
        )
    translator = SemanticTranslator()
    aliases = translator._aliases(anchor_model_id, relation_plan)
    anchor = indexes.models[anchor_model_id]
    from_sql = (
        f"{translator._model_source_sql(anchor, indexes, parameters)} "
        f"AS {_quote(aliases[anchor_model_id])}"
    )
    for edge in relation_plan:
        from_sql += translator._join_sql(edge, indexes, aliases, parameters)
    fixed_filter_sets = tuple(
        filter_set for metric in metrics for filter_set in indexes.metric_leaf_filter_sets(metric)
    )
    # 与主查询一致:混合口径已在指标展开时下推,不再进本分支的共享 WHERE。
    fixed_conditions = [
        translator._fixed_filter_sql(
            fixed_filter,
            indexes,
            aliases,
            parameters,
            timezone=statement.dataset.timezone,
        )
        for fixed_filter in (
            ()
            if statement.mixed_metric_filter_scopes
            else (fixed_filter_sets[0] if fixed_filter_sets else ())
        )
    ]
    inner_select = ", ".join(
        f"{indexes.field_sql(field_id, aliases)} AS {_quote(token)}"
        for token, field_id in field_tokens.items()
    )
    inner_sql = f"SELECT {inner_select} FROM {from_sql}"
    if fixed_conditions:
        inner_sql += " WHERE " + " AND ".join(f"({condition})" for condition in fixed_conditions)
    return (
        sqlglot.parse_one(inner_sql, read="postgres"),
        tuple(edge.relation.id for edge in relation_plan),
    )


class S2SqlSemanticTranslator:
    """Translate textual S2SQL through the governed parser stages.

    Parity sources:
    ``launchers/standalone/.../META-INF/spring.factories`` QueryParser order,
    ``SqlQueryParser``, ``DimExpressionParser``, ``MetricExpressionParser`` and
    ``DefaultSemanticTranslator.mergeOntologyQuery`` at the pinned commit.
    """

    parser_registry = (
        "SqlVariableParser",
        "StructQueryParser",
        "SqlQueryParser",
        "DefaultDimValueParser",
        "DimExpressionParser",
        "MetricExpressionParser",
        "MetricRatioParser",
        "OntologyQueryParser",
    )

    def __init__(self) -> None:
        self._parsers: tuple[_Parser, ...] = (
            _NoOpParser("SqlVariableParser"),
            _NoOpParser("StructQueryParser"),
            _SqlQueryParser(),
            _DefaultDimValueParser(),
            _DimExpressionParser(),
            _MetricExpressionParser(),
            _MetricRatioParser(),
            _OntologyQueryParser(),
        )

    def translate(
        self,
        *,
        release: SemanticRelease,
        dataset_id: str,
        corrected_s2sql: str,
        visible_element_ids: frozenset[str] | None = None,
        row_filters: Mapping[str, tuple[FixedFilter, ...]] | None = None,
        dialect: SqlDialect = SqlDialect.POSTGRES,
    ) -> S2SqlTranslation:
        try:
            dataset = next(item for item in release.datasets if item.id == dataset_id)
        except StopIteration as exc:
            raise TranslationError("unknown dataset", code="UNKNOWN_DATASET") from exc
        statement = _QueryStatement(
            release=release,
            dataset=dataset,
            corrected_s2sql=corrected_s2sql,
            query_type=textual_query_type(corrected_s2sql),
            visible_element_ids=visible_element_ids,
            row_filters=dict(row_filters or {}),
            dialect=dialect,
        )
        trace: list[str] = []
        for parser in self._parsers:
            parser.parse(statement)
            trace.append(parser.name)
        metrics_by_id = {item.id: item for item in release.metrics}
        if statement.query_type is SemanticQueryType.DETAIL:
            # 计数指标的「原始列」是标识(id),行级取值没有业务意义。裸指标列
            # 会被判成 detail 并退化成物理 id 列——「销量最高的商品」翻成
            # ORDER BY id 取行,界面上标着指标名的数字其实是主键值(实测)。
            # 与物理层 detail_metric_sql 的护栏保持同一契约。
            for metric_id in statement.metric_ids:
                governed = metrics_by_id.get(metric_id)
                if governed is not None and governed.aggregation in (
                    Aggregation.COUNT,
                    Aggregation.COUNT_DISTINCT,
                ):
                    raise TranslationError(
                        "count metrics have no row-level value in a detail query",
                        code="COUNT_METRIC_IN_DETAIL_QUERY",
                    )
        if statement.query_type is SemanticQueryType.AGGREGATE:
            # 半可加守卫原先只装在结构化路径,而客户实际走的是这条自然语言
            # 路径:同一个「会员余额」,Playground 拒答,换成问句就放行并生成
            # 跨日期的 SUM(balance)(实测)。两条路径共用同一个判定函数,避免
            # 再漂移出单边护栏——守卫装在没人走的门上等于没装。
            _reject_collapsed_non_additive_dimensions(
                _ReleaseIndexes(release),
                query_type=statement.query_type,
                grouped_dimension_ids=statement.projected_dimension_ids,
                governed_metrics=tuple(
                    metric
                    for metric in (
                        metrics_by_id.get(metric_id) for metric_id in statement.metric_ids
                    )
                    if metric is not None
                ),
                effective_aggregations=statement.aggregation_overrides,
            )
        if statement.physical_query is None:
            raise TranslationError("textual S2SQL translation produced no physical query")
        semantic_query, audit_complete = _semantic_query_evidence(statement)
        return S2SqlTranslation(
            physical_query=statement.physical_query,
            query_type=statement.query_type,
            metric_ids=semantic_query.metric_ids,
            dimension_ids=semantic_query.dimension_ids,
            corrected_s2sql=statement.corrected_s2sql,
            parser_trace=tuple(trace),
            audit_query=semantic_query,
            audit_complete=audit_complete,
        )


def _set_query_selects(tree: exp.Query) -> tuple[exp.Select, ...]:
    """Return only the leaf SELECT operands of one SQL set expression.

    Parity source: JSQLParser ``SetOperationList.getSelects``. CTE bodies are not
    set operands and therefore must not participate in branch-shape validation.
    """

    if isinstance(tree, exp.Select):
        return (tree,)
    if isinstance(tree, exp.SetOperation):
        left = tree.this
        right = tree.expression
        if not isinstance(left, exp.Query) or not isinstance(right, exp.Query):
            raise _invalid("set operation operands must be SELECT queries")
        return (*_set_query_selects(left), *_set_query_selects(right))
    raise _invalid("S2SQL query shape is not supported")


def _validate_set_operation_shape(
    tree: exp.Query,
    symbols: SemanticSymbolTable,
) -> None:
    branches = _set_query_selects(tree)
    if len(branches) == 1:
        return
    width = len(branches[0].expressions)
    if width < 1 or any(len(branch.expressions) != width for branch in branches[1:]):
        raise _invalid(
            "set operation branches must project the same number of columns",
            code="S2SQL_SET_OPERATION_SHAPE_MISMATCH",
        )
    aliases = _alias_semantic_pairs(tree, symbols)
    expected = tuple(
        _set_projection_kind(item, symbols, aliases) for item in branches[0].expressions
    )
    for branch in branches[1:]:
        actual = tuple(_set_projection_kind(item, symbols, aliases) for item in branch.expressions)
        if actual != expected:
            raise _invalid(
                "set operation branches must use compatible governed projection types",
                code="S2SQL_SET_OPERATION_SHAPE_MISMATCH",
            )


def _set_projection_kind(
    projection: exp.Expression,
    symbols: SemanticSymbolTable,
    aliases: dict[str, tuple[ResolvedSemanticSymbol, ...]],
) -> str:
    resolved = _semantic_pairs(projection, symbols, aliases)
    kinds = {item.kind for item in resolved}
    if len(kinds) == 1:
        return next(iter(kinds))
    if not kinds and isinstance(
        projection.this if isinstance(projection, exp.Alias) else projection,
        exp.Literal,
    ):
        return "literal"
    return "calculation"


def _ratio_function(projection: exp.Expression, operator: str) -> exp.Anonymous:
    candidates = []
    expression = projection.this if isinstance(projection, exp.Alias) else projection
    if isinstance(expression, exp.Anonymous) and expression.name.upper() == operator:
        candidates.append(expression)
    candidates.extend(
        item
        for item in expression.find_all(exp.Anonymous)
        if item.name.upper() == operator and item not in candidates
    )
    valid_arity = len(candidates) == 1 and (
        len(candidates[0].expressions) == 1
        or (operator == "RATIO_TO_TOTAL" and len(candidates[0].expressions) == 3)
    )
    if not valid_arity:
        raise _invalid(
            "ratio function requires one metric or metric/dimension/value arguments",
            code="S2SQL_RATIO_SHAPE_INVALID",
        )
    return candidates[0]


def _ratio_scope_dimension_ids(
    projection: exp.Expression,
    symbols: SemanticSymbolTable,
) -> set[str]:
    results: set[str] = set()
    for function in projection.find_all(exp.Anonymous):
        if function.name.upper() != "RATIO_TO_TOTAL" or len(function.expressions) != 3:
            continue
        dimension = function.expressions[1]
        if not isinstance(dimension, exp.Column):
            continue
        try:
            resolved = symbols.resolve_first(dimension.name)
        except SemanticParsingError:
            continue
        if resolved.kind == "dimension":
            results.add(resolved.id)
    return results


def _ratio_group_dimension_ids(
    statement: _QueryStatement,
    select: exp.Select,
) -> set[str]:
    group = select.args.get("group")
    if group is None:
        return set()
    dimensions_by_field = {item.field_id: item.id for item in statement.release.dimensions}
    return {
        dimensions_by_field[field_id]
        for expression in group.expressions
        for column in expression.find_all(exp.Column)
        if (field_id := statement.field_tokens.get(column.name)) in dimensions_by_field
    }


def _is_ratio_scope_dimension(
    statement: _QueryStatement,
    expression: exp.Expression,
) -> bool:
    if not isinstance(expression, exp.Column):
        return False
    field_id = statement.field_tokens.get(expression.name)
    return field_id in {item.field_id for item in statement.release.dimensions}


def _ratio_metric_aggregate(
    statement: _QueryStatement,
    call: _RatioCall,
    function: exp.Anonymous,
) -> exp.Expression:
    metric = next(item for item in statement.release.metrics if item.id == call.metric_id)
    argument = function.expressions[0].copy()
    pre_aggregated = isinstance(argument, exp.AggFunc) or argument.find(exp.AggFunc) is not None
    if metric.aggregation is not None:
        # The metric position stays a bare governed reference: the governed
        # aggregation is applied here.  An argument that already aggregates
        # (``RATIO_TO_TOTAL(SUM("净收入"), …)``) would be wrapped a second time
        # into ``SUM(SUM(…))``, which every governed stage accepts and only
        # PostgreSQL rejects - at execution time, past the parser retry that can
        # still produce a correct candidate.
        if pre_aggregated:
            raise _invalid(
                "ratio metric argument must be a governed metric, not an aggregate expression",
                code="S2SQL_RATIO_METRIC_PRE_AGGREGATED",
            )
        statement.aggregation_overrides.setdefault(metric.id, metric.aggregation)
        return _aggregate_expression(argument, metric.aggregation)
    if pre_aggregated:
        return argument
    raise _invalid(
        "ratio metric has no governed aggregate expression",
        code="S2SQL_RATIO_METRIC_REQUIRED",
    )


def _ratio_time_groups(
    statement: _QueryStatement,
    group: exp.Group | None,
) -> tuple[tuple[exp.Expression, str], ...]:
    if group is None:
        return ()
    time_field_ids = {
        item.field_id for item in statement.release.dimensions if item.semantic_type == "time"
    }
    results: list[tuple[exp.Expression, str]] = []
    for expression in group.expressions:
        if not any(
            statement.field_tokens.get(column.name) in time_field_ids
            for column in expression.find_all(exp.Column)
        ):
            continue
        trunc = (
            expression
            if isinstance(expression, exp.TimestampTrunc)
            else expression.find(exp.TimestampTrunc)
        )
        if trunc is None:
            period = "DAY"
        else:
            unit = trunc.args.get("unit")
            period = str(getattr(unit, "name", None) or getattr(unit, "this", "")).upper()
        if period not in {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}:
            raise _invalid(
                "period ratio uses an unsupported time grain",
                code="S2SQL_RATIO_TIME_REQUIRED",
            )
        results.append((expression, period))
    return tuple(results)


def _ratio_interval(mode: str, period: str) -> str:
    if mode == "RATIO_ROLL":
        return {
            "DAY": "1 day",
            "WEEK": "7 days",
            "MONTH": "1 month",
            "QUARTER": "3 months",
            "YEAR": "1 year",
        }[period]
    return {
        "DAY": "7 days",
        "WEEK": "1 month",
        "MONTH": "1 year",
        "QUARTER": "1 year",
        "YEAR": "1 year",
    }[period]


def _apply_detail_dimension_distinct(statement: _QueryStatement) -> None:
    """明细查询只投影维度时强制去重,与结构化路径同一条规则。

    结构化路径在 ``translator.py`` 里是 ``"SELECT DISTINCT " if dimensions and
    not metrics``,文本路径此前完全照搬 LLM 写的 SELECT——同一个语义查询,LLM
    写了 DISTINCT 给 2 行,没写给 3 行(明细表 join 后重复),而 ``SemanticQuery``
    没有 distinct 字段,两条 SQL 投影成同一个语义查询,评测报告里所有语义字段
    都一致却行数不同,看不出差在哪。

    去不去重是确定性决策,不该让 LLM 每次现推。想看逐行的问法应当带上主键
    维度——那时投影里有唯一列,去重不改变结果。
    """

    tree = statement.tree
    if statement.query_type is not SemanticQueryType.DETAIL:
        return
    if statement.projected_metric_ids or not statement.projected_dimension_ids:
        return
    for select in _set_query_selects(tree) if isinstance(tree, exp.Query) else ():
        if select.args.get("group") or next(select.find_all(exp.AggFunc), None) is not None:
            continue
        select.set("distinct", exp.Distinct())


def _metric_axis_scoped(
    statement: _QueryStatement,
    expression: exp.Expression,
    metric: MetricSpec,
) -> exp.Expression:
    """逻辑时间轴展开后,把指标限制在自己声明的那根轴上。

    与结构化路径同一契约:不展开时原样返回,现有行为零变化。
    """

    if not statement.metric_time_axes:
        return expression
    declared = metric.agg_time_dimension_id or statement.dataset.default_time_dimension_id
    if declared is None or declared not in statement.metric_time_axes:
        return expression
    predicate = exp.EQ(
        this=exp.column(METRIC_TIME_AXIS_COLUMN, quoted=True),
        expression=exp.Literal.string(declared),
    )
    return exp.Case(ifs=[exp.If(this=predicate, true=expression)])


def _metric_scoped_expression(
    statement: _QueryStatement,
    expression: exp.Expression,
    filters: tuple[FixedFilter, ...],
) -> exp.Expression:
    """把指标自己的固定过滤下推进聚合函数内部。

    只在同一次查询里出现多种口径时生效(``mixed_metric_filter_scopes``);口径
    一致时仍走内层共享 WHERE,语义等价且更省。共享 WHERE 无法表达多口径——
    一个指标的过滤会连带砍掉另一个,实测「即时预订占比」恒等于 1.0。

    谓词用 ``__kf_field_N`` 令牌列表达(外层树的列形态),值渲染成字面量后由
    既有的 ``_parameterize_literals`` 统一参数化。算子映射与时间归一化复用
    ``SemanticTranslator._fixed_filter_sql``,不另写一份。
    """

    if not filters or not statement.mixed_metric_filter_scopes:
        return expression
    translator = SemanticTranslator()
    indexes = _ReleaseIndexes(
            statement.release, statement.visible_element_ids, statement.row_filters
        )
    literals = _InlineLiteralBuilder({})
    conditions = [
        translator._fixed_filter_sql(
            item,
            indexes,
            {},
            literals,
            timezone=statement.dataset.timezone,
            field_sql=_field_token_sql(statement, item.field_id),
        )
        for item in filters
    ]
    predicate = sqlglot.parse_one(" AND ".join(conditions), read="postgres")
    return exp.Case(ifs=[exp.If(this=predicate, true=expression)])


def _metric_expression(
    statement: _QueryStatement,
    metric_id: str,
    stack: tuple[str, ...],
    inherited_filters: tuple[FixedFilter, ...] = (),
    *,
    force_aggregate: bool = False,
) -> exp.Expression:
    if metric_id in stack:
        raise TranslationError("cyclic metric expression", code="INVALID_METRIC_FORMULA")
    metrics = {item.id: item for item in statement.release.metrics}
    metric = metrics[metric_id]
    scope = _deduplicate_fixed_filters((*inherited_filters, *metric.filters))
    if metric.kind is MetricKind.ATOMIC:
        raw = _metric_axis_scoped(
            statement,
            _metric_scoped_expression(
                statement,
                _field_token_expression(statement, metric.field_id or ""),
                scope,
            ),
            metric,
        )
        # 上游 MetricExpressionParser 把指标替换成「治理聚合(表达式)」而不是裸
        # 物理列;指标定义期已强制表达式自带聚合,所以展开后必然含聚合函数。
        # 我们此前在明细形态下退化成 field_id,计数指标的 field 恰是主键,
        # 「销量最高的商品」被翻成 ORDER BY id 取行(实测)。已被外层聚合包裹的
        # 保持裸列,否则会翻出 COUNT(COUNT(id)) 嵌套聚合。
        if metric.aggregation is not None and (
            force_aggregate or metric_id in statement.bare_metric_ids
        ):
            return _aggregate_expression(raw, metric.aggregation)
        return raw
    if metric.define_type == "FIELD":
        sources = {item.name.casefold(): item for item in metric.expression_sources}
        tree = sqlglot.parse_one(metric.formula or "", read="postgres")
        for column in list(tree.find_all(exp.Column)):
            source = sources.get(column.name.casefold())
            if source is None:
                raise TranslationError(
                    f"unknown FIELD metric expression source: {column.name}",
                    code="INVALID_FIELD_METRIC_EXPRESSION",
                )
            column.replace(_field_token_expression(statement, source.field_id))
        return tree
    if metric.define_type == "MEASURE":
        # Exact pinned MetricExpressionParser.java:110-114 behavior; do not
        # silently substitute the previously corrected per-measure expansion.
        # 用 transform 而不是 column.replace:公式是单个裸度量名(表达式度量的单
        # 引用形态)时整棵树就是那个 Column,对根 replace 静默失败,裸名直接漏进
        # 物理 SQL(实测 UndefinedColumn)。退化形态编译成 ATOMIC 不走这里,所以
        # 生产一直没炸;transform 对根同样生效。
        sources = {item.name.casefold(): item for item in metric.expression_sources}
        fields_by_id = {item.id: item for item in statement.release.fields}

        def _resolve_raw(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.Column):
                return node
            source = sources.get(node.name.casefold())
            if source is None:
                raise TranslationError(
                    f"unknown MEASURE metric expression source: {node.name}",
                    code="INVALID_MEASURE_METRIC_EXPRESSION",
                )
            if source.expression:
                # 表达式度量:此前这里只认 field_id,度量自带的表达式被静默丢掉
                # ——「双倍额 = net_amount * 2」翻出来是裸 SUM(net_amount),数字
                # 对半错(实测 300 对 600)。与结构化路径同语义:按
                # expression_field_ids 把表达式里的列换成字段令牌。
                by_column = {
                    fields_by_id[field_id].column.casefold(): field_id
                    for field_id in source.expression_field_ids
                    if field_id in fields_by_id
                }

                def _expression_columns(inner: exp.Expression) -> exp.Expression:
                    if isinstance(inner, exp.Column):
                        field_id = by_column.get(inner.name.casefold())
                        if field_id is None:
                            raise TranslationError(
                                f"unknown measure expression field: {inner.name}",
                                code="INVALID_MEASURE_EXPRESSION",
                            )
                        return _field_token_expression(statement, field_id)
                    return inner

                return sqlglot.parse_one(source.expression, read="postgres").transform(
                    _expression_columns
                )
            return _field_token_expression(statement, source.field_id)

        raw_tree = sqlglot.parse_one(metric.formula or "", read="postgres").transform(_resolve_raw)

        def _resolve_aggregated(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Column):
                source = sources[node.name.casefold()]
                return _aggregate_expression(
                    _metric_scoped_expression(
                        statement,
                        raw_tree.copy(),
                        _deduplicate_fixed_filters((*scope, *source.filters)),
                    ),
                    source.aggregation,
                )
            return node

        return sqlglot.parse_one(metric.formula or "", read="postgres").transform(
            _resolve_aggregated
        )
    expression = metric.formula or ""
    for dependency_id in re.findall(r"\{([A-Za-z0-9_.:-]+)\}", expression):
        # 派生公式作用在「聚合后的依赖」上——与结构化路径同一契约。依赖不是
        # 树里的 token,bare_metric_ids 判不到它们,不强制就退化成裸列逐行算。
        dependency = _metric_expression(
            statement, dependency_id, (*stack, metric_id), scope, force_aggregate=True
        )
        expression = expression.replace(
            "{" + dependency_id + "}",
            f"({dependency.sql(dialect='postgres')})",
        )
    tree = sqlglot.parse_one(expression, read="postgres")
    # 与结构化路径同一防护:0 分母出 NULL,不让客户问数直接吃数据库报错。
    _guard_divisions(tree)
    return tree


def _aggregate_expression(
    expression: exp.Expression,
    aggregation: Aggregation | None,
) -> exp.Expression:
    if aggregation is Aggregation.COUNT_DISTINCT:
        return exp.Count(this=exp.Distinct(expressions=[expression]))
    constructors: dict[Aggregation, type[exp.AggFunc]] = {
        Aggregation.SUM: exp.Sum,
        Aggregation.COUNT: exp.Count,
        Aggregation.AVG: exp.Avg,
        Aggregation.MIN: exp.Min,
        Aggregation.MAX: exp.Max,
    }
    constructor = constructors.get(aggregation)
    if constructor is None:
        raise TranslationError("metric has no supported default aggregation")
    return constructor(this=expression)


def _column_aggregation(column: exp.Column) -> Aggregation | None:
    parent = column.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.Sum):
            return Aggregation.SUM
        if isinstance(parent, exp.Avg):
            return Aggregation.AVG
        if isinstance(parent, exp.Min):
            return Aggregation.MIN
        if isinstance(parent, exp.Max):
            return Aggregation.MAX
        if isinstance(parent, exp.Count):
            return (
                Aggregation.COUNT_DISTINCT
                if isinstance(parent.this, exp.Distinct)
                else Aggregation.COUNT
            )
        if isinstance(parent, exp.Anonymous) and parent.name.upper() == "COUNT_DISTINCT":
            return Aggregation.COUNT_DISTINCT
        parent = parent.parent
    return None


def _default_count_metric_id(
    release: SemanticRelease,
    dataset: DatasetSpec,
) -> str | None:
    route = next(
        (item for item in release.analysis_topic_routes if item.dataset_id == dataset.id),
        None,
    )
    if route is None or route.default_count_metric_id is None:
        return None
    metrics = {item.id: item for item in release.metrics}
    metric = metrics.get(route.default_count_metric_id)
    if (
        metric is None
        or metric.id not in dataset.metric_ids
        or metric.model_id != route.root_model_id
        or metric.aggregation not in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}
    ):
        raise _invalid(
            "analysis-topic count metric is invalid",
            code="S2SQL_DEFAULT_COUNT_METRIC_INVALID",
        )
    return metric.id


def _restore_missing_group_by(
    statement: _QueryStatement,
    *,
    force_aggregate: bool = False,
) -> None:
    """展开出聚合函数后回补 GROUP BY。

    对齐上游 ``SqlReplaceHelper``:替换完成后 ``if hasAggregateFunction ->
    addMissingGroupby``,把剩余的裸列全部并入 GROUP BY。少了这一步,补上的聚合
    会因为缺 GROUP BY 直接是非法 SQL。
    """

    tree = statement.tree
    if tree is None or next(tree.find_all(exp.AggFunc), None) is None:
        return
    # QueryTypeParser runs on authoritative textual S2SQL before metric
    # expansion. A bare governed COUNT has no row-level meaning, so an existing
    # GROUP BY must still flip the post-expansion type. SUM/AVG metrics retain
    # the reviewed detail-query behavior when no grouping dimension is added.
    if force_aggregate:
        statement.query_type = SemanticQueryType.AGGREGATE
    selects = list(tree.find_all(exp.Select))
    if isinstance(tree, exp.Select) and tree not in selects:
        selects.insert(0, tree)
    for select in selects:
        if select.args.get("group"):
            continue
        if not any(_nearest_select(item) is select for item in select.find_all(exp.AggFunc)):
            continue
        grouped: list[exp.Expression] = []
        for projection in select.expressions:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Column):
                grouped.append(inner.copy())
        if not grouped:
            continue
        select.set("group", exp.Group(expressions=grouped))
        statement.query_type = SemanticQueryType.AGGREGATE


def _field_token_expression(statement: _QueryStatement, field_id: str) -> exp.Column:
    return exp.column(_field_token(statement, field_id), quoted=True)


def _field_token_sql(statement: _QueryStatement, field_id: str) -> str:
    return _field_token_expression(statement, field_id).sql(dialect="postgres")


def _field_token(statement: _QueryStatement, field_id: str) -> str:
    for token, existing in statement.field_tokens.items():
        if existing == field_id:
            return token
    token = f"__kf_field_{len(statement.field_tokens)}"
    statement.field_tokens[token] = field_id
    return token


def _replace_derived_token(tree: exp.Expression, token: str, replacement: exp.Expression) -> None:
    """替换派生指标 token;若被聚合函数直接包裹,连同该聚合一起替换。

    ``SUM("派生")`` 里那层 SUM 对派生指标是多余包装:公式已经作用在聚合后的
    依赖上。保留它会变成嵌套聚合。只剥「直接且唯一实参」的包裹;其它形态
    (表达式内部引用等)按普通列替换,宁可数据库报嵌套聚合也不静默猜。
    """

    for column in list(tree.find_all(exp.Column)):
        if column.name != token:
            continue
        parent = column.parent
        if isinstance(parent, exp.AggFunc) and parent.this is column:
            parent.replace(replacement.copy())
        else:
            column.replace(replacement.copy())


def _replace_token(tree: exp.Expression, token: str, replacement: exp.Expression) -> None:
    for column in list(tree.find_all(exp.Column)):
        if column.name == token:
            column.replace(replacement.copy())


def _append_where(tree: exp.Select, condition: exp.Expression) -> None:
    existing = tree.args.get("where")
    if existing is None:
        tree.set("where", exp.Where(this=condition))
    else:
        existing.set("this", exp.and_(existing.this, condition))


def _is_syntactic_position(literal: exp.Literal) -> bool:
    """该字面量是语法位置而不是值,不能参数化。

    位置式 GROUP BY 1 / ORDER BY 2 里的序数指的是 SELECT 列表的第几列。
    参数化之后 PostgreSQL 对 ``ORDER BY $1`` 既不报错也不排序(PG16 实测),
    配上 LIMIT 就是静默错的 Top-N;``GROUP BY $1`` 则直接报
    "column must appear in the GROUP BY clause"。窗口帧的偏移量同理。
    """

    parent = literal.parent
    if isinstance(parent, exp.Group):
        return True
    if isinstance(parent, exp.Ordered) and parent.this is literal:
        return True
    return isinstance(parent, exp.WindowSpec)


def _parameterize_literals(tree: exp.Query, parameters: _ParameterBuilder) -> None:
    for literal in list(tree.find_all(exp.Literal)):
        parent = literal.parent
        if isinstance(parent, (exp.Limit, exp.Fetch, exp.Interval)):
            continue
        if _is_syntactic_position(literal):
            continue
        value: Any = literal.this
        if not literal.is_string:
            value = float(value) if "." in value else int(value)
        placeholder = parameters.add(value)
        literal.replace(exp.Var(this=placeholder))


def _result_limit(
    tree: exp.Query,
    dataset: DatasetSpec,
    query_type: SemanticQueryType,
) -> int:
    limit = tree.args.get("limit")
    if isinstance(limit, exp.Fetch):
        value = limit.args.get("count")
    elif isinstance(limit, exp.Limit):
        value = limit.expression
    else:
        value = None
    if value is None:
        result = (
            dataset.max_limit if query_type is SemanticQueryType.DETAIL else dataset.default_limit
        )
    elif isinstance(value, exp.Literal) and value.is_int:
        result = int(value.this)
    elif isinstance(value, exp.Neg) and isinstance(value.this, exp.Literal) and value.this.is_int:
        result = -int(value.this.this)
    else:
        raise TranslationError(
            "limit must be a positive integer",
            code="QUERY_LIMIT_EXCEEDED",
        )
    if result < 1 or result > dataset.max_limit:
        raise TranslationError(
            f"limit exceeds dataset maximum {dataset.max_limit}",
            code="QUERY_LIMIT_EXCEEDED",
        )
    return result


def _explicit_limit(tree: exp.Query) -> int | None:
    limit = tree.args.get("limit")
    if isinstance(limit, exp.Fetch):
        value = limit.args.get("count")
    elif isinstance(limit, exp.Limit):
        value = limit.expression
    else:
        return None
    return int(value.this) if isinstance(value, exp.Literal) and value.is_int else None


def _output_columns(statement: _QueryStatement) -> tuple[OutputColumn, ...]:
    assert statement.tree is not None
    metrics = {item.id: item for item in statement.release.metrics}
    dimensions = {item.id: item for item in statement.release.dimensions}
    original = validate_textual_s2sql(statement.corrected_s2sql)
    original_select = _set_query_selects(original)[0]
    translated_select = _set_query_selects(statement.tree)[0]
    symbols = SemanticSymbolTable.from_release(
        statement.release,
        dataset_id=statement.dataset.id,
    )
    aliases = _alias_semantic_pairs(original, symbols)
    semantic_ids_by_projection: list[tuple[tuple[str, str], ...]] = []
    for index, _projection in enumerate(translated_select.expressions):
        semantic_ids: list[tuple[str, str]] = []
        if index < len(original_select.expressions):
            original_projection = original_select.expressions[index]
            for resolved in _semantic_pairs(original_projection, symbols, aliases):
                pair = (resolved.kind, resolved.id)
                if pair not in semantic_ids:
                    semantic_ids.append(pair)
            count = original_projection.find(exp.Count)
            if isinstance(original_projection, exp.Count):
                count = original_projection
            if (
                count is not None
                and isinstance(count.this, exp.Star)
                and _count_star_reads_governed_dataset(count, symbols)
            ):
                count_metric_id = _default_count_metric_id(
                    statement.release,
                    statement.dataset,
                )
                count_pair = ("metric", count_metric_id) if count_metric_id is not None else None
                if count_pair is not None and count_pair not in semantic_ids:
                    semantic_ids.append(count_pair)
        semantic_ids_by_projection.append(tuple(semantic_ids))

    single_counts = Counter(items[0] for items in semantic_ids_by_projection if len(items) == 1)
    # 比率投影的位置由解析阶段确定：同一查询里 SUM(指标) 与 RATIO_OVER(指标)
    # 引用同一个指标，只看表达式区分不出哪列是比率。
    ratio_indexes = {call.projection_index for call in statement.ratio_calls}
    results: list[OutputColumn] = []
    used_ids: set[str] = set()
    for index, projection in enumerate(translated_select.expressions):
        semantic_ids = semantic_ids_by_projection[index]
        original_projection = original_select.expressions[index]
        if (
            len(semantic_ids) == 1
            and single_counts[semantic_ids[0]] == 1
            and _is_direct_semantic_projection(original_projection)
        ):
            kind, element_id = semantic_ids[0]
            name = projection.alias or (
                metrics[element_id].name if kind == "metric" else dimensions[element_id].name
            )
            results.append(OutputColumn(element_id=element_id, name=name, kind=kind))
            used_ids.add(element_id)
        else:
            element_id = projection.alias.strip() if projection.alias else ""
            if not element_id or len(element_id) > 256 or element_id in used_ids:
                element_id = f"expression:{index}"
            # 只引用维度且不含聚合的表达式（DATE_TRUNC('month', "下单日期")）
            # 是派生维度——SQL 里它也确实落在 GROUP BY 上。分成 dimension 让
            # 下游能把它当分组轴，而不是当成一条数值系列。
            derived_dimension = (
                bool(semantic_ids)
                and all(pair[0] == "dimension" for pair in semantic_ids)
                and original_projection.find(exp.AggFunc) is None
            )
            if index in ratio_indexes:
                kind_value = "ratio"
            elif derived_dimension:
                kind_value = "dimension"
            else:
                kind_value = "calculation"
            results.append(
                OutputColumn(
                    element_id=element_id,
                    name=projection.alias or f"计算列{index + 1}",
                    kind=kind_value,
                    time_grain=(
                        _projection_time_grain(original_projection)
                        if derived_dimension
                        else None
                    ),
                )
            )
            used_ids.add(element_id)
    return tuple(results)


def _projection_time_grain(projection: exp.Expression) -> str | None:
    """派生时间列的 DATE_TRUNC 粒度；不是时间截断则返回 None。"""

    expression = projection.this if isinstance(projection, exp.Alias) else projection
    # postgres 方言把 DATE_TRUNC 解析成 TimestampTrunc（与 _ratio_time_groups 一致）。
    trunc = (
        expression
        if isinstance(expression, (exp.TimestampTrunc, exp.DateTrunc))
        else expression.find(exp.TimestampTrunc, exp.DateTrunc)
    )
    if trunc is None:
        return None
    unit = trunc.args.get("unit")
    grain = str(getattr(unit, "name", None) or getattr(unit, "this", "")).upper()
    return grain if grain in {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"} else None


def _is_direct_semantic_projection(projection: exp.Expression) -> bool:
    expression = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(expression, (exp.Column, exp.AggFunc)) or (
        isinstance(expression, exp.Anonymous)
        and expression.name.upper() in {"COUNT_DISTINCT", "TOPN"}
    )


def _rewrite_order_by_metric_projection(tree: exp.Query) -> None:
    """Port SqlQueryParser.rewriteOrderBy for aggregate metric projections."""

    order = tree.args.get("order")
    if order is None:
        return
    select = _set_query_selects(tree)[0]
    projection_aliases = _select_projection_aliases(select)
    aggregate_projection_by_field: dict[str, exp.Expression] = {}
    for projection in select.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if not any(True for _ in expression.find_all(exp.Func)):
            continue
        columns = list(expression.find_all(exp.Column))
        for column in columns:
            aggregate_projection_by_field.setdefault(_normalize(column.name), expression)
    for ordered in order.expressions:
        expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
        if not isinstance(expression, exp.Column):
            continue
        if _normalize(expression.name) in projection_aliases:
            continue
        replacement = aggregate_projection_by_field.get(_normalize(expression.name))
        if replacement is not None:
            expression.replace(replacement.copy())


def _is_projection_alias_reference(
    column: exp.Column,
    aliases: dict[str, tuple[ResolvedSemanticSymbol, ...]],
    symbols: SemanticSymbolTable,
) -> bool:
    """Distinguish an output-label reference from a governed semantic column.

    Projection aliases and semantic members share SQLGlot's ``Column`` node shape.
    Their names can also legitimately be identical, so a global name lookup is not
    enough: in ``SUM(metric) AS metric`` the inner column is still the governed
    metric.  An alias is consumed here only where the AST makes it a reference:

    * a bare output alias in this SELECT's ORDER BY/GROUP BY clause; or
    * a column read by a SELECT whose inputs are CTE/derived outputs rather than the
      governed DataSet itself.

    This preserves textual S2SQL and its parser order; it only makes the existing
    alias-vs-semantic binding decision at the correct AST location.
    """

    alias_name = _normalize(column.name)
    if alias_name not in aliases:
        return False
    select = column.find_ancestor(exp.Select)
    if select is None:
        return False

    parent = column.parent
    if isinstance(parent, exp.Ordered):
        parent = parent.parent
    if isinstance(parent, (exp.Order, exp.Group)) and alias_name in _select_projection_aliases(
        select
    ):
        return True

    reads_governed_dataset = any(
        table.find_ancestor(exp.Select) is select and symbols.is_dataset(table.name)
        for table in select.find_all(exp.Table)
    )
    return not reads_governed_dataset and alias_name in _visible_derived_aliases(select)


def _select_projection_aliases(select: exp.Select) -> set[str]:
    return {_normalize(projection.alias) for projection in select.expressions if projection.alias}


def _visible_derived_aliases(select: exp.Select) -> set[str]:
    """Aliases exported by the CTE/subquery inputs of exactly one SELECT."""

    direct_table_names = {
        _normalize(table.name)
        for table in select.find_all(exp.Table)
        if table.find_ancestor(exp.Select) is select
    }
    root: exp.Expression = select
    while root.parent is not None:
        root = root.parent

    visible: set[str] = set()
    for cte in root.find_all(exp.CTE):
        if _normalize(cte.alias_or_name) not in direct_table_names:
            continue
        source = cte.this if isinstance(cte.this, exp.Select) else cte.this.find(exp.Select)
        if source is not None:
            visible.update(_select_projection_aliases(source))
    for subquery in select.find_all(exp.Subquery):
        if subquery.find_ancestor(exp.Select) is not select:
            continue
        source = (
            subquery.this
            if isinstance(subquery.this, exp.Select)
            else subquery.this.find(exp.Select)
        )
        if source is not None:
            visible.update(_select_projection_aliases(source))
    return visible


def _alias_semantic_pairs(
    tree: exp.Query,
    symbols: SemanticSymbolTable,
) -> dict[str, tuple[ResolvedSemanticSymbol, ...]]:
    aliases: dict[str, tuple[ResolvedSemanticSymbol, ...]] = {}
    selects = [*tree.find_all(exp.Select)]
    if isinstance(tree, exp.Select):
        selects.insert(0, tree)
    for select in reversed(tuple(dict.fromkeys(selects))):
        for projection in select.expressions:
            if not projection.alias:
                continue
            aliases[_normalize(projection.alias)] = _semantic_pairs(
                projection.this if isinstance(projection, exp.Alias) else projection,
                symbols,
                aliases,
            )
    return aliases


def _semantic_pairs(
    expression: exp.Expression,
    symbols: SemanticSymbolTable,
    aliases: dict[str, tuple[ResolvedSemanticSymbol, ...]],
) -> tuple[ResolvedSemanticSymbol, ...]:
    results: list[ResolvedSemanticSymbol] = []
    columns = list(expression.find_all(exp.Column))
    if isinstance(expression, exp.Column):
        columns.insert(0, expression)
    for column in columns:
        alias_items = (
            aliases.get(_normalize(column.name))
            if _is_projection_alias_reference(column, aliases, symbols)
            else None
        )
        if alias_items is not None:
            for item in alias_items:
                if (item.kind, item.id) not in {
                    (existing.kind, existing.id) for existing in results
                }:
                    results.append(item)
            continue
        try:
            resolved = symbols.resolve_first(column.name)
        except SemanticParsingError:
            continue
        if (resolved.kind, resolved.id) not in {
            (existing.kind, existing.id) for existing in results
        }:
            results.append(resolved)
    return tuple(results)


def _semantic_query_evidence(
    statement: _QueryStatement,
) -> tuple[SemanticQuery, bool]:
    """Build an output-only audit projection after textual translation.

    This DTO supports the existing inspector and golden-suite UI. It is never an
    input to a Corrector or Translator, so calculations remain authoritative in
    ``corrected_s2sql`` even when QueryStructReq cannot represent their output slot.

    The projection is deliberately narrower than S2SQL: OR branches, subquery
    predicates and unresolvable references have no QueryFilter representation.
    Returning a completeness flag keeps that gap visible, because the projection
    also drives the user-facing interpretation, where a silently dropped filter
    reads as "no filter was applied".
    """

    original_tree = validate_textual_s2sql(statement.corrected_s2sql)
    original = _set_query_selects(original_tree)[0]
    symbols = SemanticSymbolTable.from_release(
        statement.release,
        dataset_id=statement.dataset.id,
    )
    filters: list[QueryFilter] = []
    measure_filters: list[QueryMeasureFilter] = []
    metric_filters: list[QueryMetricFilter] = []
    evidence_selects = tuple(
        dict.fromkeys(
            (
                original,
                *original_tree.find_all(exp.Select),
            )
        )
    )
    complete = True
    for select in evidence_selects:
        where = select.args.get("where")
        if where is not None:
            predicates, where_complete = _and_predicate_evidence(where.this)
            complete = complete and where_complete
            for predicate in predicates:
                try:
                    parsed = _evidence_predicate(predicate, symbols)
                except SemanticParsingError:
                    complete = False
                    continue
                if parsed is None:
                    complete = False
                    continue
                resolved, operator, value = parsed
                if resolved.kind == "dimension":
                    query_filter = QueryFilter(
                        dimension_id=resolved.id,
                        operator=operator,
                        value=value,
                    )
                    if query_filter not in filters:
                        filters.append(query_filter)
                else:
                    measure_filter = QueryMeasureFilter(
                        metric_id=resolved.id,
                        operator=operator,
                        value=value,
                    )
                    if measure_filter not in measure_filters:
                        measure_filters.append(measure_filter)
        having = select.args.get("having")
        if having is not None:
            predicates, having_complete = _and_predicate_evidence(having.this)
            complete = complete and having_complete
            for predicate in predicates:
                try:
                    parsed = _evidence_predicate(predicate, symbols)
                except SemanticParsingError:
                    complete = False
                    continue
                if parsed is None or parsed[0].kind != "metric":
                    complete = False
                    continue
                resolved, operator, value = parsed
                metric_filter = QueryMetricFilter(
                    metric_id=resolved.id,
                    operator=operator,
                    value=value,
                )
                if metric_filter not in metric_filters:
                    metric_filters.append(metric_filter)
    for function in original_tree.find_all(exp.Anonymous):
        if function.name.upper() != "RATIO_TO_TOTAL" or len(function.expressions) != 3:
            continue
        dimension_arg = function.expressions[1]
        if not isinstance(dimension_arg, exp.Column):
            continue
        resolved = symbols.resolve_first(dimension_arg.name)
        if resolved.kind != "dimension":
            continue
        ratio_filter = QueryFilter(
            dimension_id=resolved.id,
            operator=FilterOperator.EQ,
            value=_literal_value(function.expressions[2]),
        )
        if ratio_filter not in filters:
            filters.append(ratio_filter)
    order_by = _evidence_orders(
        original,
        symbols,
        default_count_metric_id=_default_count_metric_id(
            statement.release,
            statement.dataset,
        ),
    )
    projected_metrics = tuple(statement.projected_metric_ids or statement.metric_ids)
    projected_dimensions = tuple(
        statement.projected_dimension_ids or (() if projected_metrics else statement.dimension_ids)
    )
    evidence = SemanticQuery(
        dataset_id=statement.dataset.id,
        query_type=statement.query_type,
        metric_ids=projected_metrics,
        aggregation_overrides=tuple(
            QueryAggregationOverride(metric_id=metric_id, aggregation=aggregation)
            for metric_id, aggregation in statement.aggregation_overrides.items()
            if metric_id in projected_metrics
        ),
        dimension_ids=projected_dimensions,
        filters=tuple(filters),
        measure_filters=tuple(measure_filters),
        metric_filters=tuple(metric_filters),
        order_by=order_by,
        limit=_explicit_limit(original_tree),
    )
    return evidence, complete


def _evidence_orders(
    tree: exp.Query,
    symbols: SemanticSymbolTable,
    *,
    default_count_metric_id: str | None,
) -> tuple[QueryOrder, ...]:
    order = tree.args.get("order")
    if order is None:
        return ()
    aliases = {
        projection.alias: projection.this
        for projection in tree.expressions
        if isinstance(projection, exp.Alias) and projection.alias
    }
    results: list[QueryOrder] = []
    for item in order.expressions:
        expression = item.this if isinstance(item, exp.Ordered) else item
        if isinstance(expression, exp.Column) and expression.name in aliases:
            expression = aliases[expression.name]
        columns = list(expression.find_all(exp.Column))
        if isinstance(expression, exp.Column):
            columns.insert(0, expression)
        unique = []
        for column in columns:
            try:
                resolved = symbols.resolve_first(column.name)
            except SemanticParsingError:
                continue
            if resolved.id not in {entry.id for entry in unique}:
                unique.append(resolved)
        element_id: str | None = unique[0].id if len(unique) == 1 else None
        if element_id is None and default_count_metric_id is not None:
            count = expression.find(exp.Count)
            if isinstance(expression, exp.Count):
                count = expression
            if (
                count is not None
                and isinstance(count.this, exp.Star)
                and _count_star_reads_governed_dataset(count, symbols)
            ):
                element_id = default_count_metric_id
        if element_id is None:
            continue
        results.append(
            QueryOrder(
                element_id=element_id,
                direction=(
                    SortDirection.DESC
                    if isinstance(item, exp.Ordered) and item.args.get("desc")
                    else SortDirection.ASC
                ),
            )
        )
    return tuple(results)


def _evidence_predicate(
    predicate: exp.Expression,
    symbols: SemanticSymbolTable,
) -> tuple[ResolvedSemanticSymbol, FilterOperator, Any] | None:
    if isinstance(predicate, exp.Paren):
        return _evidence_predicate(predicate.this, symbols)
    negated = isinstance(predicate, exp.Not)
    target = predicate.this if negated else predicate
    negated = negated or bool(target.args.get("negate"))
    if isinstance(target, exp.In):
        if target.args.get("query") is not None:
            return None
        resolved = _resolve_predicate_symbol(target.this, symbols)
        return (
            resolved,
            FilterOperator.NOT_IN if negated else FilterOperator.IN,
            tuple(_literal_value(item) for item in target.expressions),
        )
    if isinstance(target, exp.Between) and not negated:
        return (
            _resolve_predicate_symbol(target.this, symbols),
            FilterOperator.BETWEEN,
            (_literal_value(target.args["low"]), _literal_value(target.args["high"])),
        )
    comparisons: dict[type[exp.Expression], FilterOperator] = {
        exp.EQ: FilterOperator.EQ,
        exp.NEQ: FilterOperator.NE,
        exp.GT: FilterOperator.GT,
        exp.GTE: FilterOperator.GTE,
        exp.LT: FilterOperator.LT,
        exp.LTE: FilterOperator.LTE,
        exp.Like: FilterOperator.LIKE,
    }
    operator = comparisons.get(type(target))
    if operator is not None:
        return (
            _resolve_predicate_symbol(target.this, symbols),
            operator,
            _literal_value(target.expression),
        )
    if isinstance(target, exp.Is):
        operator = FilterOperator.IS_NOT_NULL if negated else FilterOperator.IS_NULL
        return _resolve_predicate_symbol(target.this, symbols), operator, None
    return None


def _resolve_predicate_symbol(
    expression: exp.Expression,
    symbols: SemanticSymbolTable,
) -> ResolvedSemanticSymbol:
    columns = list(expression.find_all(exp.Column))
    if isinstance(expression, exp.Column):
        columns.insert(0, expression)
    for column in columns:
        try:
            return symbols.resolve_first(column.name)
        except SemanticParsingError:
            continue
    raise _invalid("predicate does not reference a governed semantic field")


def _and_predicates(expression: exp.Expression) -> tuple[exp.Expression, ...]:
    predicates, _complete = _and_predicate_evidence(expression)
    return predicates


def _and_predicate_evidence(
    expression: exp.Expression,
) -> tuple[tuple[exp.Expression, ...], bool]:
    """Split a conjunction into audit-representable predicates.

    A QueryFilter list is an implicit AND, so an OR branch has no faithful
    projection and is dropped. The second element reports that drop so callers
    can mark the projection incomplete rather than present it as filter-free.
    """

    if isinstance(expression, exp.And):
        left, left_complete = _and_predicate_evidence(expression.this)
        right, right_complete = _and_predicate_evidence(expression.expression)
        return (*left, *right), (left_complete and right_complete)
    if isinstance(expression, exp.Or):
        return (), False
    return (expression,), True


def _literal_value(expression: exp.Expression) -> Any:
    if isinstance(expression, exp.Paren):
        return _literal_value(expression.this)
    if isinstance(expression, exp.Cast):
        return _literal_value(expression.this)
    if isinstance(expression, exp.Neg):
        value = _literal_value(expression.this)
        return -value
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return expression.this
        return float(expression.this) if "." in expression.this else int(expression.this)
    if isinstance(expression, exp.Boolean):
        return bool(expression.this)
    if isinstance(expression, exp.Null):
        return None
    raise _invalid("predicate value must be a literal")


def _inside_select_projection(expression: exp.Expression) -> bool:
    parent = expression.parent
    child: exp.Expression = expression
    while parent is not None:
        if isinstance(parent, exp.Select):
            return child in parent.expressions
        child = parent
        parent = parent.parent
    return False


def _count_star_reads_governed_dataset(
    count: exp.Count,
    symbols: SemanticSymbolTable,
) -> bool:
    """Bind COUNT(*) only at a SELECT that directly reads the logical DataSet.

    A COUNT over CTE or subquery rows is a derived row count. Replacing it with
    the fact-root count metric leaks a physical field into a scope that does not
    project that field and changes the query meaning.
    """

    select = _nearest_select(count)
    if select is None:
        return False
    return any(
        symbols.is_dataset(table.name) and _nearest_select(table) is select
        for table in select.find_all(exp.Table)
    )


def _nearest_select(expression: exp.Expression) -> exp.Select | None:
    parent = expression.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return parent
        parent = parent.parent
    return None


def _inside_where(expression: exp.Expression) -> bool:
    parent = expression.parent
    while parent is not None and not isinstance(parent, exp.Select):
        if isinstance(parent, exp.Where):
            return True
        parent = parent.parent
    return False


def _normalize(value: str) -> str:
    return value.strip().casefold()


def _invalid(message: str, *, code: str = "LLM_S2SQL_AST_INVALID") -> SemanticParsingError:
    return SemanticParsingError(message, code=code)
