from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowflow_analytics.errors import SemanticValidationError

#: 上游 ``Constants.DAY_FORMAT``。时间列未声明书写格式时按它渲染。
DEFAULT_DATE_FORMAT = "yyyy-MM-dd"


def _validate_postgresql_identifier(value: str, *, label: str) -> None:
    if "\x00" in value or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{label} must be a valid PostgreSQL identifier")


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldKind(StrEnum):
    IDENTIFIER = "identifier"
    DIMENSION = "dimension"
    TIME = "time"
    MEASURE = "measure"
    FIELD = "field"


class Aggregation(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class MetricKind(StrEnum):
    ATOMIC = "atomic"
    DERIVED = "derived"


class SemanticQueryType(StrEnum):
    """Logical query shape.

    Mirrors the governed ``QueryType`` and the structured query request
    contract.

    The latter also keeps dimension and metric filters as separate fields.
    """

    DETAIL = "detail"
    AGGREGATE = "aggregate"


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class JoinType(StrEnum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"


class FilterOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    LIKE = "like"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class QueryRuleType(StrEnum):
    """Governed query-rule types."""

    ADD_DATE = "ADD_DATE"
    ADD_SELECT = "ADD_SELECT"


class QueryRuleMode(StrEnum):
    """Governed query-rule modes."""

    BEFORE = "BEFORE"
    RECENT = "RECENT"
    EXIST = "EXIST"


class QueryRuleSpec(FrozenModel):
    """Immutable, release-bound query rules consumed at query time.

    The pinned project exposes CRUD but no runtime consumer. KnowFlow consumes
    semantic IDs only and uses deterministic priority ordering.
    """

    id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    priority: int = Field(default=1, ge=0, le=3)
    rule_type: QueryRuleType
    mode: QueryRuleMode
    parameters: tuple[str | int, ...] = ()
    outputs: tuple[str, ...] = ()
    enabled: bool = True

    @model_validator(mode="after")
    def validate_rule_shape(self) -> QueryRuleSpec:
        if self.rule_type is QueryRuleType.ADD_DATE:
            if (
                self.mode not in {QueryRuleMode.BEFORE, QueryRuleMode.RECENT}
                or len(self.parameters) != 1
                or isinstance(self.parameters[0], bool)
                or not isinstance(self.parameters[0], int)
                or not 1 <= self.parameters[0] <= 3_650
                or self.outputs
            ):
                raise ValueError("ADD_DATE requires one bounded day count and no outputs")
        elif (
            self.mode is not QueryRuleMode.EXIST
            or not self.parameters
            or not self.outputs
            or any(not isinstance(item, str) for item in self.parameters)
        ):
            raise ValueError("ADD_SELECT requires EXIST semantic inputs and outputs")
        if len(self.parameters) != len(set(self.parameters)):
            raise ValueError("query rule parameters must be unique")
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("query rule outputs must be unique")
        return self


class FixedFilter(FrozenModel):
    field_id: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None


def _without_retired_drill_down(value: object) -> object:
    """下钻白名单退役后,存量 Release/Revision 的 spec 侧照常加载。

    退役时只给 catalog 合同(ModelContract/MetricContract)加了退役键丢弃,
    spec 侧漏了——37 个存量 revision 的 semantic_spec 全部带着
    drill_down_dimensions 键,extra="forbid" 直接拒载,所有 revision 端点
    INTERNAL_ERROR。与 upstreamCommit 退役同一处理:读取时对称丢弃。
    """

    if isinstance(value, dict) and (
        "drill_down_dimensions" in value or "drillDownDimensions" in value
    ):
        return {
            key: item
            for key, item in value.items()
            if key not in {"drill_down_dimensions", "drillDownDimensions"}
        }
    return value


def _without_retired_folders(value: object) -> object:
    """分析主题目录退役后,存量 Release/Revision 照常加载。

    ``folders`` 声明后从未被消费:全仓只有字段定义一处,前端也只写空数组。
    它是一个假装存在的能力,留着迟早有人往里塞东西。与 drill_down 退役同一
    处理——``extra="forbid"`` 下必须读取时对称丢弃,否则存量 revision 全部拒载。
    """

    if isinstance(value, dict) and "folders" in value:
        return {key: item for key, item in value.items() if key != "folders"}
    return value


class ModelSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    biz_name: str | None = Field(default=None, min_length=1, max_length=256)
    query_type: Literal["table_query", "sql_query"] = "table_query"
    table: str | None = Field(default=None, min_length=1, max_length=256)
    schema_name: str | None = Field(default="public", min_length=1, max_length=256)
    db_type: str | None = Field(default=None, max_length=128)
    sql_query: str | None = Field(default=None, max_length=100_000)
    filter_sql: str | None = Field(default=None, max_length=20_000)
    filters: tuple[FixedFilter, ...] = ()
    sql_variables: tuple[dict[str, Any], ...] = ()
    description: str = Field(default="", max_length=2_000)
    aliases: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: object) -> object:
        return _without_retired_drill_down(value)

    @model_validator(mode="after")
    def validate_physical_identifiers(self) -> ModelSpec:
        if self.query_type == "table_query":
            if self.table is None or self.schema_name is None or self.sql_query is not None:
                raise ValueError("table_query requires schema_name/table and no sql_query")
            for label, value in (("table", self.table), ("schema_name", self.schema_name)):
                _validate_postgresql_identifier(value, label=label)
            if self.sql_variables:
                raise ValueError("table_query cannot define sql_variables")
        elif self.sql_query is None or self.table is not None:
            raise ValueError("sql_query requires sql_query and no table")
        return self


class FieldSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    column: str = Field(min_length=1, max_length=256)
    data_type: str = Field(default="text", min_length=1, max_length=128)
    kind: FieldKind = FieldKind.FIELD
    identifier_type: Literal["primary", "foreign"] | None = None
    dimension_type: (
        Literal[
            "categorical",
            "time",
            "partition_time",
            "primary_key",
            "foreign_key",
        ]
        | None
    ) = None
    semantic_expr: str | None = Field(default=None, min_length=1, max_length=2_000)
    unit: str | None = Field(default=None, max_length=128)
    default_aggregation: Aggregation | None = None
    description: str = Field(default="", max_length=2_000)
    aliases: tuple[str, ...] = ()
    nullable: bool = True
    # The AI model-schema result does not choose these
    # flags. ModelConverter deterministically enables the matching resource from
    # filedType. They remain explicit here so a human can change the generated
    # ModelDetail before publication.
    create_dimension: bool = False
    create_metric: bool = False

    @model_validator(mode="after")
    def validate_column(self) -> FieldSpec:
        _validate_postgresql_identifier(self.column, label="column")
        if self.identifier_type is not None and self.kind is not FieldKind.IDENTIFIER:
            raise ValueError("identifier_type requires an identifier field")
        if self.dimension_type is not None and self.kind not in {
            FieldKind.DIMENSION,
            FieldKind.TIME,
        }:
            raise ValueError("dimension_type requires a dimension field")
        if self.unit is not None and self.kind is not FieldKind.MEASURE:
            raise ValueError("unit requires a measure field")
        if self.default_aggregation is not None and self.kind is not FieldKind.MEASURE:
            raise ValueError("default_aggregation requires a measure field")
        if self.create_dimension and self.kind not in {
            FieldKind.IDENTIFIER,
            FieldKind.DIMENSION,
            FieldKind.TIME,
        }:
            raise ValueError("create_dimension requires an identifier or dimension field")
        if self.create_metric and self.kind is not FieldKind.MEASURE:
            raise ValueError("create_metric requires a measure field")
        if self.create_dimension and self.create_metric:
            raise ValueError("a field cannot create both a dimension and a metric")
        return self


class RelationCondition(FrozenModel):
    left_field_id: str = Field(min_length=1, max_length=128)
    right_field_id: str = Field(min_length=1, max_length=128)


class RelationSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    left_model_id: str = Field(min_length=1, max_length=128)
    right_model_id: str = Field(min_length=1, max_length=128)
    join_type: JoinType = JoinType.LEFT
    cardinality: Cardinality
    conditions: tuple[RelationCondition, ...] = Field(min_length=1)


class TimeGranularity(StrEnum):
    """受治理的时间粒度，对应 DTO 的 timeGranularity。"""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class NonAdditiveDimension(FrozenModel):
    """沿某个维度不可相加的度量声明（半可加度量）。

    余额、库存、MAU 这类度量按门店可加、按时间不可加：把某账户 90 天的每日
    余额相加没有业务含义，用户要的是期末那一刻的值。翻译层据此先沿该维度
    取窗口值再在其余维度聚合；缺少该声明时这类问题会返回一个看起来正常的
    错误数字。对齐 dbt ``non_additive_dimension``。
    """

    dimension_id: str = Field(min_length=1, max_length=128)
    # 期初取 MIN、期末取 MAX；其余聚合会重新引入被禁止的相加。
    window_choice: Literal[Aggregation.MIN, Aggregation.MAX] = Aggregation.MAX
    # 先按这些维度分组再取窗口值，例如按用户各自取期末余额后再求和。
    window_groupings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_groupings(self) -> NonAdditiveDimension:
        if len(self.window_groupings) != len(set(self.window_groupings)):
            raise ValueError("non-additive window groupings must be unique")
        if self.dimension_id in self.window_groupings:
            raise ValueError("non-additive dimension cannot also be a window grouping")
        return self


class DimensionSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    model_id: str = Field(min_length=1, max_length=128)
    field_id: str = Field(min_length=1, max_length=128)
    aliases: tuple[str, ...] = ()
    description: str = Field(default="", max_length=2_000)
    semantic_type: Literal["categorical", "time", "identifier"] = "categorical"
    # 物理数据类型(ARRAY/MAP/JSON 等)。对齐上游 PromptHelper 的 DATATYPE '..':
    # 缺了它,模型会在数组/JSON 列上生成普通 = 比较。
    data_type: str | None = Field(default=None, max_length=64)
    # 时间列在库里的书写格式(上游 Dimension.dateFormat,默认 yyyy-MM-dd)。
    # 分区时间列常是 int(20260802)或 varchar,绑定 date 对象会被 PG 直接拒绝;
    # 渲染过滤字面量和告知模型都要用它。只对时间维度有意义。
    date_format: str | None = Field(default=None, max_length=64)
    expression: str | None = Field(default=None, min_length=1, max_length=2_000)
    expression_field_ids: tuple[str, ...] = ()
    default_values: tuple[str, ...] = ()
    # DimensionTimeTypeParams.timeGranularity 早已存在，此前编译时被丢弃。
    # 它声明底层列的真实粒度：数据只到天时，按小时分组的问句必须被识别为
    # 不可满足，而不是让翻译层静默按不存在的粒度分组。
    time_granularity: TimeGranularity | None = None
    # 逻辑时间轴:分组/过滤时每个指标解析到自己声明的 agg_time_dimension,
    # 而不是共用一根物理时间列。编译期在「同一数据集内指标声明了多根轴」时
    # 合成,建模者不编写它——建模表单与前端因此无需同步。
    #
    # 它存在的理由是静默错数:跨月支付很常见,「本月收入和订单数」共用一根轴
    # 时,实测 2000 笔半年数据首月订单数偏差 -123 笔、末月 +95 笔,趋势图上看
    # 不出任何异常。MetricFlow 的 metric_time 与 Cube 的 multi-fact view 都把
    # 这件事放在查询期解决,不交给建模者手工绕开。
    metric_time_axis: bool = False

    @model_validator(mode="after")
    def validate_time_granularity(self) -> DimensionSpec:
        if self.time_granularity is not None and self.semantic_type != "time":
            raise ValueError("time granularity requires a time dimension")
        return self

    @model_validator(mode="after")
    def validate_expression_fields(self) -> DimensionSpec:
        if self.expression is None and self.expression_field_ids:
            raise ValueError("dimension expression fields require an expression")
        if self.expression is not None and not self.expression_field_ids:
            raise ValueError("computed dimension expression requires referenced fields")
        if len(self.expression_field_ids) != len(set(self.expression_field_ids)):
            raise ValueError("dimension expression fields must be unique")
        return self


class MetricExpressionSource(FrozenModel):
    """One FIELD or MEASURE token used by a metric expression."""

    name: str = Field(min_length=1, max_length=256)
    field_id: str = Field(min_length=1, max_length=128)
    expression: str | None = Field(default=None, min_length=1, max_length=2_000)
    expression_field_ids: tuple[str, ...] = ()
    aggregation: Aggregation | None = None
    raw_filter_sql: str | None = Field(default=None, max_length=2_000)
    filters: tuple[FixedFilter, ...] = ()
    alias: str | None = Field(default=None, max_length=2_000)
    unit: str | None = Field(default=None, max_length=128)
    is_create_metric: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def validate_expression(self) -> MetricExpressionSource:
        if self.expression is None and self.expression_field_ids:
            raise ValueError("metric source expression fields require an expression")
        if self.expression is not None and not self.expression_field_ids:
            raise ValueError("metric source expression requires referenced fields")
        if len(self.expression_field_ids) != len(set(self.expression_field_ids)):
            raise ValueError("metric source expression fields must be unique")
        return self


class HierarchySpec(FrozenModel):
    """同一把尺子上由粗到细的一组维度，例如「行政区划」= 省 > 市 > 区。

    模型看到的维度是一张扁平表，「省」和「市」之间没有任何关系。用户问「按地区
    看」时它只能在几个都像的维度里猜一个；问「再细一点」时它不知道下一级是什么。
    层级把这层关系显式化。

    这不是下钻白名单：白名单限制「允许按哪些维度分组」（70 个真实指标里 0 个配过），
    层级描述「哪些维度是同一把尺子的刻度」，两者回答的是不同问题。
    """

    id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = ()
    description: str = Field(default="", max_length=2_000)
    # 由粗到细的维度 id。少于两级不构成层级。
    levels: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_levels(self) -> HierarchySpec:
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("hierarchy levels must be unique")
        return self


class MetricSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    model_id: str = Field(min_length=1, max_length=128)
    kind: MetricKind = MetricKind.ATOMIC
    field_id: str | None = None
    aggregation: Aggregation | None = None
    formula: str | None = Field(default=None, max_length=2_000)
    define_type: Literal["FIELD", "MEASURE", "METRIC"] | None = None
    raw_filter_sql: str | None = Field(default=None, max_length=20_000)
    filters: tuple[FixedFilter, ...] = ()
    aliases: tuple[str, ...] = ()
    description: str = Field(default="", max_length=2_000)
    unit: str | None = Field(default=None, max_length=64)
    format: str | None = Field(default=None, max_length=128)
    requires_explicit_time: bool = False
    # 该指标沿哪条时间轴聚合。同一模型上「收入」按支付时间、「订单数」按下单
    # 时间是常态，此前只有数据集级一个默认值，所有指标共用——不报错，只会给出
    # 一个看起来正常的错数字。这是全链路唯一会静默出错的地方，所以做成一级字段
    # 而不是塞进 ext：模型和表单都要能看见它、被要求填它。
    agg_time_dimension_id: str | None = Field(default=None, max_length=128)
    non_additive_dimension: NonAdditiveDimension | None = None
    expression_sources: tuple[MetricExpressionSource, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: object) -> object:
        return _without_retired_drill_down(value)

    def is_additive_along(self, dimension_id: str) -> bool:
        """该指标能否沿这个维度直接相加。未声明时与补齐前行为一致。"""

        return (
            self.non_additive_dimension is None
            or self.non_additive_dimension.dimension_id != dimension_id
        )

    @model_validator(mode="after")
    def validate_definition(self) -> MetricSpec:
        if self.kind is MetricKind.ATOMIC:
            if (
                self.field_id is None
                or self.aggregation is None
                or self.formula is not None
                or self.expression_sources
            ):
                raise ValueError("atomic metric requires field_id and aggregation only")
        elif self.formula is None or self.field_id is not None or self.aggregation is not None:
            raise ValueError("derived metric requires formula only")
        if self.expression_sources and self.define_type not in {"FIELD", "MEASURE"}:
            raise ValueError("expression sources require a FIELD or MEASURE metric")
        if (
            self.define_type in {"FIELD", "MEASURE"}
            and self.kind is MetricKind.DERIVED
            and not self.expression_sources
        ):
            raise ValueError("derived FIELD or MEASURE metric requires expression sources")
        source_names = [item.name.casefold() for item in self.expression_sources]
        if len(source_names) != len(set(source_names)):
            raise ValueError("metric expression source names must be unique")
        if self.non_additive_dimension is not None:
            # 派生指标的可加性由其依赖指标决定，这里二次声明会与依赖冲突。
            if self.kind is not MetricKind.ATOMIC:
                raise ValueError("only an atomic metric can declare a non-additive dimension")
            # 只有本身会沿维度相加的聚合才需要这个声明；MIN/MAX/COUNT_DISTINCT
            # 不随维度相加，声明它只会制造两处等价语义。
            if self.aggregation not in {Aggregation.SUM, Aggregation.COUNT}:
                raise ValueError("a non-additive dimension only applies to an additive aggregation")
        if self.agg_time_dimension_id is not None and self.kind is not MetricKind.ATOMIC:
            # 派生指标的时间轴由其依赖的原子指标决定；在这里再声明一次只会与
            # 依赖冲突，且无法判断以哪个为准。
            raise ValueError("only an atomic metric can declare an aggregation time dimension")
        return self


class DatasetTimeDefaultConfig(FrozenModel):
    """Dataset time defaults projected into the query runtime."""

    unit: int = Field(ge=1, le=10_000)
    period: Literal["DAY", "WEEK", "MONTH", "QUARTER", "YEAR"] = "DAY"
    time_mode: Literal["LAST", "RECENT", "CURRENT"] = "LAST"


class AnalysisTopicPathSpec(FrozenModel):
    """One explicit, root-relative model path owned by an analysis topic.

    Reviewed accuracy decision: the DataSet keeps the member scope, and the
    frozen path only removes runtime join-path guessing.
    """

    target_model_id: str = Field(min_length=1, max_length=128)
    relation_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    # Scope-local canonical-name qualification uses the governed model display
    # name. Keep the same maximum as ModelSpec.name so every valid model can be
    # represented without truncation or unstable hashes.
    prefix: str | None = Field(default=None, min_length=1, max_length=256)


class AnalysisTopicRouteSpec(FrozenModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    root_model_id: str = Field(min_length=1, max_length=128)
    # Reviewed accuracy decision. A bare COUNT(*) would otherwise be a
    # row-count expression; KnowFlow binds it to this human-confirmed metric so
    # the fact grain and fixed business filters stay governed.
    default_count_metric_id: str | None = Field(default=None, min_length=1, max_length=128)
    paths: tuple[AnalysisTopicPathSpec, ...] = Field(default=(), max_length=100)
    ai_context: str = Field(default="", max_length=4_000)

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: object) -> object:
        return _without_retired_folders(value)

    @property
    def id(self) -> str:
        return self.dataset_id

    @model_validator(mode="after")
    def validate_unique_targets(self) -> AnalysisTopicRouteSpec:
        targets = [item.target_model_id for item in self.paths]
        if len(targets) != len(set(targets)):
            raise ValueError("analysis topic paths must have unique target models")
        if self.root_model_id in targets:
            raise ValueError("analysis topic root must not have a path to itself")
        return self


class SemanticContextEntry(FrozenModel):
    """One reviewed, release-bound business-context fact.

    Context may help the final textual S2SQL parser understand already-governed
    semantics, but it never creates metrics, dimensions, joins, filters or aliases.
    Source labels remain explicit so database facts, profile evidence, reviewed
    documents and human conventions are not flattened into indistinguishable prose.
    """

    id: str = Field(min_length=1, max_length=128)
    target_type: Literal["project", "model", "metric", "dimension", "query_scope"]
    target_id: str = Field(min_length=1, max_length=128)
    kind: Literal["definition", "convention", "scope", "exception", "time_policy"]
    text: str = Field(min_length=1, max_length=4_000)
    source_type: Literal[
        "database_comment",
        "profile_evidence",
        "knowledge_document",
        "human_convention",
        "catalog_description",
    ]
    # Provenance is content-addressed and opaque. URLs, paths and access tokens
    # are deliberately rejected so catalog metadata cannot become a credential
    # exfiltration channel when a revision is returned through the BFF.
    source_ref: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        max_length=71,
    )

    @model_validator(mode="after")
    def require_document_provenance(self) -> SemanticContextEntry:
        if self.source_type == "knowledge_document" and self.source_ref is None:
            raise ValueError("knowledge-document context requires a source reference")
        if self.source_type != "knowledge_document" and self.source_ref is not None:
            raise ValueError("only knowledge-document context may carry a source reference")
        return self


class DatasetSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    biz_name: str | None = Field(default=None, min_length=1, max_length=256)
    model_ids: tuple[str, ...] = Field(min_length=1)
    metric_ids: tuple[str, ...] = ()
    dimension_ids: tuple[str, ...] = ()
    default_limit: int = Field(default=100, ge=1, le=10_000)
    max_limit: int = Field(default=1_000, ge=1, le=100_000)
    aliases: tuple[str, ...] = ()
    description: str = Field(default="", max_length=2_000)
    default_time_dimension_id: str | None = None
    default_time_days: int | None = Field(default=None, ge=1, le=3_650)
    detail_time_default: DatasetTimeDefaultConfig | None = None
    aggregate_time_default: DatasetTimeDefaultConfig | None = None
    timezone: str = Field(default="UTC", min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_limits(self) -> DatasetSpec:
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit cannot exceed max_limit")
        if self.default_time_days is not None and self.default_time_dimension_id is None:
            raise ValueError("default_time_days requires a governed default time dimension")
        if (
            self.detail_time_default is not None or self.aggregate_time_default is not None
        ) and self.default_time_dimension_id is None:
            raise ValueError("dataset time defaults require a governed time dimension")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA time zone") from exc
        return self


class TermSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2_000)
    aliases: tuple[str, ...] = ()
    dataset_ids: tuple[str, ...] = ()
    metric_ids: tuple[str, ...] = ()
    dimension_ids: tuple[str, ...] = ()


class DimensionValueSpec(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    dimension_id: str = Field(min_length=1, max_length=128)
    value: str | int | float | bool
    display_name: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = Field(default=(), max_length=100)
    enabled: bool = True

    @model_validator(mode="after")
    def aliases_are_bounded_and_unambiguous(self) -> DimensionValueSpec:
        if not self.display_name.strip():
            raise ValueError("dimension value display name cannot be blank")
        normalized: set[str] = set()
        for alias in self.aliases:
            if not alias.strip() or len(alias) > 256:
                raise ValueError("dimension value aliases must contain 1 to 256 characters")
            key = alias.strip().casefold()
            if key in normalized:
                raise ValueError("dimension value aliases must be unique")
            normalized.add(key)
        return self


class SemanticRelease(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    spec_hash: str = Field(min_length=1, max_length=128)
    models: tuple[ModelSpec, ...]
    fields: tuple[FieldSpec, ...]
    relations: tuple[RelationSpec, ...] = ()
    dimensions: tuple[DimensionSpec, ...] = ()
    hierarchies: tuple[HierarchySpec, ...] = ()
    metrics: tuple[MetricSpec, ...] = ()
    datasets: tuple[DatasetSpec, ...]
    terms: tuple[TermSpec, ...] = ()
    dimension_values: tuple[DimensionValueSpec, ...] = ()
    semantic_context: tuple[SemanticContextEntry, ...] = ()
    analysis_topic_routes: tuple[AnalysisTopicRouteSpec, ...] = ()
    query_rules: tuple[QueryRuleSpec, ...] = ()
    # Versioned modeling catalog. Query components consume
    # the normalized projection above; publishing persists both in one immutable
    # release so the modeling contract can be reloaded without reconstructing it
    # from SQL or benchmark fixtures.
    modeling_catalog: dict[str, Any] | None = None
    revision_id: str | None = None
    index_snapshot_id: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> SemanticRelease:
        indexes = {
            "model": _unique_index(self.models, "model"),
            "field": _unique_index(self.fields, "field"),
            "dimension": _unique_index(self.dimensions, "dimension"),
            "metric": _unique_index(self.metrics, "metric"),
            "dataset": _unique_index(self.datasets, "dataset"),
            "relation": _unique_index(self.relations, "relation"),
            "term": _unique_index(self.terms, "term"),
            "dimension_value": _unique_index(self.dimension_values, "dimension value"),
            "analysis_topic_route": _unique_index(
                self.analysis_topic_routes, "analysis topic route"
            ),
            "query_rule": _unique_index(self.query_rules, "query rule"),
        }
        models = indexes["model"]
        fields = indexes["field"]
        dimensions = indexes["dimension"]
        metrics = indexes["metric"]

        for field in self.fields:
            _require(models, field.model_id, f"field {field.id} model")
        for model in self.models:
            for fixed_filter in model.filters:
                filter_field = _require(
                    fields,
                    fixed_filter.field_id,
                    f"model {model.id} fixed filter",
                )
                if filter_field.model_id != model.id:
                    raise SemanticValidationError(
                        f"model {model.id} fixed filter belongs to another model"
                    )
        for dimension in self.dimensions:
            _require(models, dimension.model_id, f"dimension {dimension.id} model")
            field = _require(fields, dimension.field_id, f"dimension {dimension.id} field")
            if field.model_id != dimension.model_id:
                raise SemanticValidationError(
                    f"dimension {dimension.id} field belongs to another model"
                )
            for expression_field_id in dimension.expression_field_ids:
                expression_field = _require(
                    fields,
                    expression_field_id,
                    f"dimension {dimension.id} expression field",
                )
                if expression_field.model_id != dimension.model_id:
                    raise SemanticValidationError(
                        f"dimension {dimension.id} expression field belongs to another model"
                    )
        for metric in self.metrics:
            _require(models, metric.model_id, f"metric {metric.id} model")
            if metric.field_id is not None:
                field = _require(fields, metric.field_id, f"metric {metric.id} field")
                if field.model_id != metric.model_id:
                    raise SemanticValidationError(
                        f"metric {metric.id} field belongs to another model"
                    )
            for fixed_filter in metric.filters:
                filter_field = _require(
                    fields,
                    fixed_filter.field_id,
                    f"metric {metric.id} fixed filter",
                )
                if filter_field.model_id != metric.model_id:
                    raise SemanticValidationError(
                        f"metric {metric.id} fixed filter belongs to another model"
                    )
            for source in metric.expression_sources:
                source_field = _require(
                    fields,
                    source.field_id,
                    f"metric {metric.id} expression source",
                )
                if source_field.model_id != metric.model_id:
                    raise SemanticValidationError(
                        f"metric {metric.id} expression source belongs to another model"
                    )
                for expression_field_id in source.expression_field_ids:
                    expression_field = _require(
                        fields,
                        expression_field_id,
                        f"metric {metric.id} source expression field",
                    )
                    if expression_field.model_id != metric.model_id:
                        raise SemanticValidationError(
                            f"metric {metric.id} source expression field belongs to another model"
                        )
                for fixed_filter in source.filters:
                    filter_field = _require(
                        fields,
                        fixed_filter.field_id,
                        f"metric {metric.id} expression source filter",
                    )
                    if filter_field.model_id != metric.model_id:
                        raise SemanticValidationError(
                            f"metric {metric.id} expression source filter belongs to another model"
                        )
            if metric.kind is MetricKind.DERIVED and metric.define_type not in {
                "FIELD",
                "MEASURE",
            }:
                for dependency_id in re.findall(r"\{([A-Za-z0-9_.:-]+)\}", metric.formula or ""):
                    dependency = _require(
                        metrics,
                        dependency_id,
                        f"derived metric {metric.id} dependency",
                    )
                    if dependency.model_id != metric.model_id:
                        raise SemanticValidationError(
                            f"derived metric {metric.id} dependency belongs to another model"
                        )
        for relation in self.relations:
            _require(models, relation.left_model_id, f"relation {relation.id} left model")
            _require(models, relation.right_model_id, f"relation {relation.id} right model")
            if relation.left_model_id == relation.right_model_id:
                raise SemanticValidationError(f"relation {relation.id} cannot self-join in V0")
            for condition in relation.conditions:
                left = _require(fields, condition.left_field_id, "relation left field")
                right = _require(fields, condition.right_field_id, "relation right field")
                if (
                    left.model_id != relation.left_model_id
                    or right.model_id != relation.right_model_id
                ):
                    raise SemanticValidationError(
                        f"relation {relation.id} condition does not match relation models"
                    )
        for dataset in self.datasets:
            for model_id in dataset.model_ids:
                _require(models, model_id, f"dataset {dataset.id} model")
            for metric_id in dataset.metric_ids:
                metric = _require(metrics, metric_id, f"dataset {dataset.id} metric")
                if metric.model_id not in dataset.model_ids:
                    raise SemanticValidationError(
                        f"dataset {dataset.id} metric belongs to an excluded model"
                    )
            for dimension_id in dataset.dimension_ids:
                dimension = _require(dimensions, dimension_id, f"dataset {dataset.id} dimension")
                if dimension.model_id not in dataset.model_ids:
                    raise SemanticValidationError(
                        f"dataset {dataset.id} dimension belongs to an excluded model"
                    )
            if dataset.default_time_dimension_id is not None:
                dimension = _require(
                    dimensions,
                    dataset.default_time_dimension_id,
                    f"dataset {dataset.id} default time dimension",
                )
                if dimension.id not in dataset.dimension_ids:
                    raise SemanticValidationError(
                        f"dataset {dataset.id} default time dimension is not exposed"
                    )
                if dimension.semantic_type != "time":
                    raise SemanticValidationError(
                        f"dataset {dataset.id} default time dimension is not temporal"
                    )
        relation_ids = set(indexes["relation"])
        for route in self.analysis_topic_routes:
            dataset = _require(indexes["dataset"], route.dataset_id, "analysis topic route dataset")
            if route.root_model_id not in dataset.model_ids:
                raise SemanticValidationError(
                    f"analysis topic {route.dataset_id} root is outside its dataset"
                )
            if route.default_count_metric_id is not None:
                count_metric = _require(
                    metrics,
                    route.default_count_metric_id,
                    f"analysis topic {route.dataset_id} default count metric",
                )
                if count_metric.id not in dataset.metric_ids:
                    raise SemanticValidationError(
                        f"analysis topic {route.dataset_id} default count metric is not exposed"
                    )
                if count_metric.model_id != route.root_model_id:
                    raise SemanticValidationError(
                        f"analysis topic {route.dataset_id} default count metric is outside root"
                    )
                if count_metric.aggregation not in {
                    Aggregation.COUNT,
                    Aggregation.COUNT_DISTINCT,
                }:
                    raise SemanticValidationError(
                        f"analysis topic {route.dataset_id} default count metric is not a count"
                    )
            for path in route.paths:
                if path.target_model_id not in dataset.model_ids:
                    raise SemanticValidationError(
                        f"analysis topic {route.dataset_id} target is outside its dataset"
                    )
                unknown_relations = set(path.relation_ids) - relation_ids
                if unknown_relations:
                    raise SemanticValidationError(
                        f"analysis topic {route.dataset_id} references unknown relations: "
                        f"{sorted(unknown_relations)}"
                    )
        for term in self.terms:
            for dataset_id in term.dataset_ids:
                _require(indexes["dataset"], dataset_id, f"term {term.id} dataset")
            for metric_id in term.metric_ids:
                _require(metrics, metric_id, f"term {term.id} metric")
            for dimension_id in term.dimension_ids:
                _require(dimensions, dimension_id, f"term {term.id} dimension")
        for dimension_value in self.dimension_values:
            _require(
                dimensions,
                dimension_value.dimension_id,
                f"dimension value {dimension_value.id} dimension",
            )
        for rule in self.query_rules:
            dataset = _require(indexes["dataset"], rule.dataset_id, f"query rule {rule.id} dataset")
            if rule.rule_type is QueryRuleType.ADD_SELECT:
                for dimension_id in (*rule.parameters, *rule.outputs):
                    if dimension_id not in dataset.dimension_ids:
                        raise SemanticValidationError(
                            f"query rule {rule.id} dimension is outside its dataset"
                        )
            elif dataset.default_time_dimension_id is None:
                raise SemanticValidationError(
                    f"query rule {rule.id} requires a default time dimension"
                )
        _validate_derived_metrics(metrics)
        return self


class QueryFilter(FrozenModel):
    dimension_id: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None


class QueryMeasureFilter(FrozenModel):
    """Row-level predicate over one governed atomic measure (SQL WHERE)."""

    metric_id: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None


class QueryMetricFilter(FrozenModel):
    """Aggregate predicate over one governed metric expression (SQL HAVING).

    Parity source: ``QueryStructReq.java::buildHavingClause`` converts
    ``metricFilters`` into the HAVING clause. ``QueryMeasureFilter`` is the
    accuracy-preserving row-level counterpart for governed atomic measures.
    """

    metric_id: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None


class QueryOrder(FrozenModel):
    element_id: str = Field(min_length=1, max_length=128)
    direction: SortDirection = SortDirection.DESC


class QueryAggregationOverride(FrozenModel):
    """Select an aggregate projection for one governed atomic metric.

    The metric remains the published semantic object; this is the
    structured-query aggregator contract. The aggregation may equal the published
    default because query type is derived from the presence of aggregate projections,
    as in ``QueryTypeParser.java``.
    """

    metric_id: str = Field(min_length=1, max_length=128)
    aggregation: Aggregation


class SemanticQuery(FrozenModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    query_type: SemanticQueryType = SemanticQueryType.AGGREGATE
    metric_ids: tuple[str, ...] = ()
    aggregation_overrides: tuple[QueryAggregationOverride, ...] = ()
    dimension_ids: tuple[str, ...] = ()
    filters: tuple[QueryFilter, ...] = ()
    measure_filters: tuple[QueryMeasureFilter, ...] = ()
    metric_filters: tuple[QueryMetricFilter, ...] = ()
    order_by: tuple[QueryOrder, ...] = ()
    limit: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def require_projection(self) -> SemanticQuery:
        if not self.metric_ids and not self.dimension_ids:
            raise ValueError("query requires at least one metric or dimension")
        override_ids = [item.metric_id for item in self.aggregation_overrides]
        if len(override_ids) != len(set(override_ids)):
            raise ValueError("query aggregation override metric ids must be unique")
        if not set(override_ids).issubset(self.metric_ids):
            raise ValueError("query aggregation overrides require selected metrics")
        if self.query_type is SemanticQueryType.DETAIL:
            if self.aggregation_overrides:
                raise ValueError("detail query cannot override metric aggregation")
            if self.metric_filters:
                raise ValueError("detail query cannot contain aggregate metric filters")
        return self


class OutputColumn(FrozenModel):
    element_id: str
    name: str
    # ratio 是期间比/占比这类比率列：值域与普通聚合不同（可负、可超 100%），
    # 下游据此决定百分比展示，不能与 calculation 混为一谈。
    kind: Literal["metric", "dimension", "calculation", "ratio"]
    # DATE_TRUNC 派生时间列的粒度。结果值是 timestamptz，按年分组也会带出
    # 「2026-01-01T00:00:00+08:00」，展示时应收敛到该粒度。
    time_grain: Literal["DAY", "WEEK", "MONTH", "QUARTER", "YEAR"] | None = None


class PhysicalQuery(FrozenModel):
    release_id: str
    dataset_id: str
    sql: str
    parameters: dict[str, Any]
    columns: tuple[OutputColumn, ...]
    relation_ids: tuple[str, ...] = ()
    applied_defaults: tuple[str, ...] = ()
    result_limit: int = Field(default=100, ge=1, le=100_000)


class QueryResult(FrozenModel):
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    row_count: int
    truncated: bool = False


def _unique_index(items: tuple[Any, ...], label: str) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for item in items:
        if item.id in index:
            raise SemanticValidationError(f"duplicate {label} id: {item.id}")
        index[item.id] = item
    return index


def _require(index: dict[str, Any], item_id: str, label: str) -> Any:
    item = index.get(item_id)
    if item is None:
        raise SemanticValidationError(f"unknown {label}: {item_id}")
    return item


def _validate_derived_metrics(metrics: dict[str, MetricSpec]) -> None:
    references = {
        metric.id: set(re.findall(r"\{([A-Za-z0-9_.:-]+)\}", metric.formula or ""))
        for metric in metrics.values()
        if metric.kind is MetricKind.DERIVED and metric.define_type not in {"FIELD", "MEASURE"}
    }
    for metric_id, dependencies in references.items():
        if not dependencies:
            raise SemanticValidationError(f"derived metric {metric_id} has no dependencies")
        for dependency in dependencies:
            if dependency not in metrics:
                raise SemanticValidationError(
                    f"derived metric {metric_id} references unknown metric {dependency}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(metric_id: str) -> None:
        if metric_id in visiting:
            raise SemanticValidationError(f"metric dependency cycle includes {metric_id}")
        if metric_id in visited:
            return
        visiting.add(metric_id)
        for dependency in references.get(metric_id, set()):
            visit(dependency)
        visiting.remove(metric_id)
        visited.add(metric_id)

    for metric_id in references:
        visit(metric_id)
