from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowflow_analytics.contracts import (
    AnalysisTopicRouteSpec,
    Cardinality,
    DimensionValueSpec,
    QueryRuleMode,
    QueryRuleSpec,
    QueryRuleType,
    SemanticContextEntry,
    TermSpec,
)


def _without_keys(value: Any, retired: set[str]) -> Any:
    """从待校验的 payload 里丢掉已退役的键。"""

    if isinstance(value, dict) and retired & value.keys():
        return {key: item for key, item in value.items() if key not in retired}
    return value


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(item[:1].upper() + item[1:] for item in tail)


class CatalogContract(BaseModel):
    """Strict, loss-free representation of one governed catalog resource.

    ``extra="forbid"`` is deliberate: a catalog round-trips through the API and
    the database, and a silently dropped key would corrupt a published release.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        alias_generator=_to_camel,
    )


class ModelDefineType(StrEnum):
    SQL_QUERY = "sql_query"
    TABLE_QUERY = "table_query"


class IdentifierType(StrEnum):
    PRIMARY = "primary"
    FOREIGN = "foreign"


class ModelDimensionType(StrEnum):
    CATEGORICAL = "categorical"
    TIME = "time"
    PARTITION_TIME = "partition_time"
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"


class VariableValueType(StrEnum):
    STRING = "STRING"
    NUMBER = "NUMBER"
    EXPR = "EXPR"


class MetricDefineType(StrEnum):
    FIELD = "FIELD"
    MEASURE = "MEASURE"
    METRIC = "METRIC"


class SemanticColumnType(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    PARTITION_TIME = "partition_time"
    TIME = "time"
    CATEGORICAL = "categorical"


class AggOperator(StrEnum):
    NONE = "NONE"
    MAX = "MAX"
    MIN = "MIN"
    AVG = "AVG"
    SUM = "SUM"
    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"
    DISTINCT = "DISTINCT"
    TOPN = "TOPN"
    PERCENTILE = "PERCENTILE"
    RATIO_ROLL = "RATIO_ROLL"
    RATIO_OVER = "RATIO_OVER"
    UNKNOWN = "UNKNOWN"


class DatePeriod(StrEnum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    QUARTER = "QUARTER"
    YEAR = "YEAR"


class TimeMode(StrEnum):
    LAST = "LAST"
    RECENT = "RECENT"
    CURRENT = "CURRENT"


class SqlVariableContract(CatalogContract):
    name: str = Field(min_length=1, max_length=256)
    value_type: VariableValueType
    default_values: tuple[Any, ...] = ()


class SemanticColumnContract(CatalogContract):
    column_name: str = Field(min_length=1, max_length=256)
    data_type: str = Field(min_length=1, max_length=256)
    comment: str = Field(default="", max_length=4_000)
    # `filedType` is the wire spelling of this key; it is not a typo to fix.
    filed_type: SemanticColumnType
    name: str = Field(min_length=1, max_length=256)
    expr: str = Field(min_length=1, max_length=2_000)


class SemanticMetricContract(CatalogContract):
    """一条可聚合的度量，与列分类互不排斥。

    此前聚合方式是 ``filedType`` 单选枚举里的一个取值，``measure`` 要和
    ``categorical`` 抢同一个槽位——建模格式对照实验里，58 个真正可加的列只有
    20 个被判成度量。提成独立区块后是 56 个，键与时间维度的准确率不变。
    """

    column_name: str = Field(min_length=1, max_length=256)
    agg: AggOperator
    unit: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_aggregation(self) -> SemanticMetricContract:
        supported = {
            AggOperator.MAX,
            AggOperator.MIN,
            AggOperator.AVG,
            AggOperator.SUM,
            AggOperator.COUNT,
            AggOperator.COUNT_DISTINCT,
        }
        if self.agg not in supported:
            raise ValueError("semantic metric requires an M0 aggregate")
        return self


class ModelSchemaContract(CatalogContract):
    """Exact structured result the semantic modellers must return."""

    name: str = Field(min_length=1, max_length=256)
    biz_name: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    description: str = Field(default="", max_length=4_000)
    semantic_columns: tuple[SemanticColumnContract, ...]
    metrics: tuple[SemanticMetricContract, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def drop_metrics_on_undeclared_columns(cls, value: Any) -> Any:
        """丢掉指向未声明列的度量，而不是拒收整份 Schema。

        模型偶尔把一个可聚合的列只写进 metrics、漏掉 semanticColumns。硬拒会让
        一次小失误搞挂整个建模跑：重试用同样的输入，模型给出同样的输出，三次
        全烧掉——这正是聚合方式还在列上时 ``measure + NONE`` 那次事故的形状。
        转换阶段本来就会丢弃匹配不上物理字段的列，这里与它保持一致。
        """

        if not isinstance(value, dict):
            return value
        metrics = value.get("metrics") or value.get("semantic_metrics")
        columns = value.get("semanticColumns") or value.get("semantic_columns")
        if not isinstance(metrics, list) or not isinstance(columns, list):
            return value
        declared = {
            str(item.get("columnName") or item.get("column_name") or "").casefold()
            for item in columns
            if isinstance(item, dict)
        }
        kept = [
            item
            for item in metrics
            if not isinstance(item, dict)
            or str(item.get("columnName") or item.get("column_name") or "").casefold() in declared
        ]
        if len(kept) == len(metrics):
            return value
        return {**value, "metrics": kept}

    @model_validator(mode="after")
    def validate_columns(self) -> ModelSchemaContract:
        names = [item.column_name.casefold() for item in self.semantic_columns]
        if len(names) != len(set(names)):
            raise ValueError("ModelSchema semanticColumns must be unique")
        metric_columns = [item.column_name.casefold() for item in self.metrics]
        # 同一列两条度量是真正的矛盾，去重没有确定答案，必须拒收。
        if len(metric_columns) != len(set(metric_columns)):
            raise ValueError("ModelSchema metrics must be unique per column")
        return self


class ModelFieldContract(CatalogContract):
    field_name: str = Field(min_length=1, max_length=256)
    data_type: str = Field(min_length=1, max_length=256)


class IdentifierContract(CatalogContract):
    name: str = Field(min_length=1, max_length=256)
    type: IdentifierType
    biz_name: str = Field(min_length=1, max_length=256)
    is_create_dimension: int = Field(default=0, ge=0, le=1)


class DimensionTimeTypeParamsContract(CatalogContract):
    is_primary: str = "true"
    time_granularity: str = Field(default="day", min_length=1, max_length=64)


class ModelDimensionContract(CatalogContract):
    name: str = Field(min_length=1, max_length=256)
    type: ModelDimensionType
    expr: str = Field(min_length=1, max_length=2_000)
    date_format: str = Field(default="yyyy-MM-dd", min_length=1, max_length=128)
    data_type: str | None = Field(default=None, max_length=256)
    type_params: DimensionTimeTypeParamsContract | None = None
    is_create_dimension: int = Field(default=0, ge=0, le=1)
    biz_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4_000)


class MeasureContract(CatalogContract):
    name: str = Field(min_length=1, max_length=256)
    agg: str = Field(min_length=1, max_length=64)
    expr: str = Field(min_length=1, max_length=2_000)
    biz_name: str = Field(min_length=1, max_length=256)
    # 度量的业务口径（「已扣退款」这类）。此前这个合同没有描述字段，于是 AI 按
    # 提示词写出的口径在这一步被整条丢掉：维度传了 description、度量无处可放。
    # 后果是指标定义回落成名字本身，别名生成锚在一个空描述上、又被「不得编造与
    # 描述无关的业务含义」约束，只可能产出名字的同义变体——用户说的「销售额」
    # 永远匹配不上。口径是问数准确率的主导变量，不是可选元数据。
    description: str = Field(default="", max_length=4_000)
    is_create_metric: int = Field(default=0, ge=0, le=1)
    constraint: str | None = Field(default=None, max_length=2_000)
    alias: str | None = Field(default=None, max_length=2_000)
    unit: str | None = Field(default=None, max_length=128)


class ModelDetailContract(CatalogContract):
    query_type: ModelDefineType
    db_type: str | None = Field(default=None, max_length=128)
    sql_query: str | None = Field(default=None, max_length=100_000)
    table_query: str | None = Field(default=None, max_length=1_000)
    filter_sql: str | None = Field(default=None, max_length=20_000)
    identifiers: tuple[IdentifierContract, ...] = ()
    dimensions: tuple[ModelDimensionContract, ...] = ()
    measures: tuple[MeasureContract, ...] = ()
    fields: tuple[ModelFieldContract, ...] = ()
    sql_variables: tuple[SqlVariableContract, ...] = ()

    @model_validator(mode="after")
    def validate_source_and_fields(self) -> ModelDetailContract:
        if self.query_type is ModelDefineType.TABLE_QUERY:
            if not self.table_query or self.sql_query:
                raise ValueError("table_query models require only tableQuery")
            if self.sql_variables:
                raise ValueError("table_query models cannot define sqlVariables")
        elif not self.sql_query or self.table_query:
            raise ValueError("sql_query models require only sqlQuery")

        physical_fields = {item.field_name for item in self.fields}
        if len(physical_fields) != len(self.fields):
            raise ValueError("model fields must be unique")
        classifications: list[str] = []
        classifications.extend(item.biz_name for item in self.identifiers)
        # Dimension and measure bizName is a semantic identifier, not a
        # physical field. Only direct expressions participate in the one-field
        # classification check; computed expressions are validated by the
        # corresponding expression parser during catalog compilation.
        classifications.extend(
            item.expr for item in self.dimensions if item.expr in physical_fields
        )
        classifications.extend(item.expr for item in self.measures if item.expr in physical_fields)
        unknown = sorted(set(classifications) - physical_fields)
        if unknown:
            raise ValueError(f"classified fields are absent from fields: {unknown}")
        if len(set(classifications)) != len(classifications):
            raise ValueError("a physical field cannot have multiple model classifications")
        return self


class SchemaItemContract(CatalogContract):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    biz_name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=4_000)
    status: int | None = None
    type_enum: str | None = Field(default=None, max_length=128)
    sensitive_level: int = 0


class ModelContract(SchemaItemContract):
    database_id: str | None = Field(default=None, max_length=128)
    domain_id: str | None = Field(default=None, max_length=128)
    filter_sql: str | None = Field(default=None, max_length=20_000)
    is_open: int | None = Field(default=None, ge=0, le=1)
    alias: str | None = Field(default=None, max_length=2_000)
    source_type: str | None = Field(default=None, max_length=128)
    model_detail: ModelDetailContract
    viewers: tuple[str, ...] = ()
    view_orgs: tuple[str, ...] = ()
    admins: tuple[str, ...] = ()
    admin_orgs: tuple[str, ...] = ()
    ext: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: Any) -> Any:
        """加载下钻白名单退役前写入的模型。

        Release 逐字存储目录，``extra="forbid"`` 会直接拒掉旧 payload，
        所以退役键在读取时丢弃——和 ``upstreamCommit`` 同一处理。
        """

        return _without_keys(value, {"drillDownDimensions", "drill_down_dimensions"})


class JoinConditionContract(CatalogContract):
    left_field: str = Field(min_length=1, max_length=256)
    right_field: str = Field(min_length=1, max_length=256)
    operator: str = Field(default="=", min_length=1, max_length=32)


class ModelRelationContract(CatalogContract):
    id: str = Field(min_length=1, max_length=128)
    domain_id: str | None = Field(default=None, max_length=128)
    from_model_id: str = Field(min_length=1, max_length=128)
    to_model_id: str = Field(min_length=1, max_length=128)
    join_type: str = Field(min_length=1, max_length=32)
    join_conditions: tuple[JoinConditionContract, ...] = Field(min_length=1)
    # Accuracy extension: the relation DTO carries no
    # cardinality, but the deterministic translator requires an explicit value.
    knowflow_cardinality: Cardinality | None = None
    # Why this edge was proposed. A database constraint is a fact; a name match is
    # a suggestion. Both still require the same human confirmation, but the
    # modeling page must be able to weight them differently.
    knowflow_evidence: Literal["database_foreign_key", "name_convention"] = "database_foreign_key"
    knowflow_rationale: str = Field(default="", max_length=1_000)


class DimValueMapContract(CatalogContract):
    tech_name: str
    biz_name: str
    alias: tuple[str, ...] = ()
    value: str | None = None


class DimensionContract(SchemaItemContract):
    model_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=128)
    expr: str = Field(min_length=1, max_length=2_000)
    semantic_type: str = Field(default="CATEGORY", min_length=1, max_length=128)
    alias: str | None = Field(default=None, max_length=2_000)
    default_values: tuple[str, ...] = ()
    dim_value_maps: tuple[DimValueMapContract, ...] = ()
    data_type: str | None = Field(default=None, max_length=128)
    ext: dict[str, Any] = Field(default_factory=dict)
    type_params: DimensionTimeTypeParamsContract | None = None


class HierarchyContract(CatalogContract):
    """由粗到细的一组维度。levels 是有序的维度 id 列表。"""

    id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    alias: str | None = Field(default=None, max_length=2_000)
    description: str = Field(default="", max_length=4_000)
    levels: tuple[str, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_levels(self) -> HierarchyContract:
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("hierarchy levels must be unique")
        return self


class MetricDefineParamsContract(CatalogContract):
    expr: str = Field(min_length=1, max_length=4_000)
    filter_sql: str | None = Field(default=None, max_length=20_000)


class FieldParamContract(CatalogContract):
    field_name: str = Field(min_length=1, max_length=256)


class MeasureParamContract(CatalogContract):
    biz_name: str = Field(min_length=1, max_length=256)
    constraint: str | None = Field(default=None, max_length=2_000)
    agg: str | None = Field(default=None, max_length=64)


class MetricParamContract(CatalogContract):
    id: str = Field(min_length=1, max_length=128)
    biz_name: str = Field(min_length=1, max_length=256)


class MetricDefineByFieldParamsContract(MetricDefineParamsContract):
    fields: tuple[FieldParamContract, ...] = Field(min_length=1)


class MetricDefineByMeasureParamsContract(MetricDefineParamsContract):
    measures: tuple[MeasureContract, ...] = Field(min_length=1)


class MetricDefineByMetricParamsContract(MetricDefineParamsContract):
    metrics: tuple[MetricParamContract, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dependencies(self) -> MetricDefineByMetricParamsContract:
        ids = [item.id for item in self.metrics]
        names = [item.biz_name for item in self.metrics]
        if len(ids) != len(set(ids)) or len(names) != len(set(names)):
            raise ValueError("metric dependencies must have unique ids and bizNames")
        return self


class DataFormatContract(CatalogContract):
    need_multiply100: bool = False
    decimal_places: int | None = Field(default=None, ge=0, le=20)


class MetricContract(SchemaItemContract):
    model_id: str = Field(min_length=1, max_length=128)
    alias: str | None = Field(default=None, max_length=2_000)
    data_format_type: str | None = Field(default=None, max_length=128)
    data_format: DataFormatContract | None = None
    classifications: tuple[str, ...] = ()
    # 指标沿哪条时间轴聚合(维度 id)。留空时回落到数据集默认时间维度。
    agg_time_dimension_id: str | None = Field(default=None, max_length=128)
    is_tag: int = Field(default=0, ge=0, le=1)
    ext: dict[str, Any] = Field(default_factory=dict)
    metric_define_type: MetricDefineType = MetricDefineType.MEASURE
    metric_define_by_measure_params: MetricDefineByMeasureParamsContract | None = None
    metric_define_by_field_params: MetricDefineByFieldParamsContract | None = None
    metric_define_by_metric_params: MetricDefineByMetricParamsContract | None = None

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: Any) -> Any:
        """加载下钻白名单退役前写入的指标。见 ModelContract 上的同名方法。"""

        return _without_keys(value, {"relateDimension", "relate_dimension"})

    @model_validator(mode="after")
    def validate_definition(self) -> MetricContract:
        definitions = {
            MetricDefineType.FIELD: self.metric_define_by_field_params,
            MetricDefineType.MEASURE: self.metric_define_by_measure_params,
            MetricDefineType.METRIC: self.metric_define_by_metric_params,
        }
        if definitions[self.metric_define_type] is None:
            raise ValueError(f"{self.metric_define_type.value} metric params are required")
        unexpected = (
            value for key, value in definitions.items() if key is not self.metric_define_type
        )
        if any(value is not None for value in unexpected):
            raise ValueError("metric must define exactly one params object")
        return self


class DataSetModelConfigContract(CatalogContract):
    id: str = Field(min_length=1, max_length=128)
    includes_all: bool = False
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> DataSetModelConfigContract:
        if self.includes_all and (self.metrics or self.dimensions):
            raise ValueError("includesAll cannot be combined with explicit element ids")
        return self


class DataSetDetailContract(CatalogContract):
    data_set_model_configs: tuple[DataSetModelConfigContract, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_models(self) -> DataSetDetailContract:
        model_ids = [item.id for item in self.data_set_model_configs]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("dataSetModelConfigs must contain unique models")
        return self


class TimeDefaultConfigContract(CatalogContract):
    unit: int = Field(default=1, ge=-1, le=10_000)
    period: DatePeriod = DatePeriod.DAY
    time_mode: TimeMode = TimeMode.LAST

    @model_validator(mode="after")
    def validate_unit(self) -> TimeDefaultConfigContract:
        # MetricSemanticQuery and DetailSemanticQuery treat -1 as
        # the explicit switch that disables automatic time filtering.
        if self.unit == 0:
            raise ValueError("time default unit must be -1 or a positive integer")
        return self


class DetailTypeDefaultConfigContract(CatalogContract):
    time_default_config: TimeDefaultConfigContract = Field(
        default_factory=TimeDefaultConfigContract
    )
    limit: int = Field(default=500, ge=1, le=100_000)


class AggregateTypeDefaultConfigContract(CatalogContract):
    time_default_config: TimeDefaultConfigContract = Field(
        default_factory=lambda: TimeDefaultConfigContract(
            unit=7,
            period=DatePeriod.DAY,
            time_mode=TimeMode.RECENT,
        )
    )
    limit: int = Field(default=200, ge=1, le=100_000)


class QueryConfigContract(CatalogContract):
    detail_type_default_config: DetailTypeDefaultConfigContract = Field(
        default_factory=DetailTypeDefaultConfigContract
    )
    aggregate_type_default_config: AggregateTypeDefaultConfigContract = Field(
        default_factory=AggregateTypeDefaultConfigContract
    )


class DataSetContract(SchemaItemContract):
    domain_id: str | None = Field(default=None, max_length=128)
    data_set_detail: DataSetDetailContract
    alias: str | None = Field(default=None, max_length=2_000)
    query_config: QueryConfigContract = Field(default_factory=QueryConfigContract)
    admins: tuple[str, ...] = ()
    admin_orgs: tuple[str, ...] = ()


class QueryRuleContract(CatalogContract):
    """Versioned form of the pinned standalone QueryRule resource."""

    id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    priority: int = Field(default=1, ge=0, le=3)
    rule_type: QueryRuleType
    mode: QueryRuleMode
    parameters: tuple[str | int, ...] = ()
    outputs: tuple[str, ...] = ()
    enabled: bool = True

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> QueryRuleContract:
        QueryRuleSpec.model_validate(self.model_dump(mode="python"))
        return self


class SemanticCatalog(CatalogContract):
    project_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    contract_version: str = "knowflow-modeling-v1"
    models: tuple[ModelContract, ...] = ()
    model_relations: tuple[ModelRelationContract, ...] = ()
    dimensions: tuple[DimensionContract, ...] = ()
    hierarchies: tuple[HierarchyContract, ...] = ()
    metrics: tuple[MetricContract, ...] = ()
    data_sets: tuple[DataSetContract, ...] = ()
    terms: tuple[TermSpec, ...] = ()
    dimension_values: tuple[DimensionValueSpec, ...] = ()
    # Reviewed context is part of the immutable semantic contract. AI evidence
    # remains a proposal until review; only accepted entries enter this tuple.
    semantic_context: tuple[SemanticContextEntry, ...] = ()
    # Reviewed KnowFlow extension. DataSet remains the authoritative member
    # scope; this versioned contract freezes the root-relative join route.
    analysis_topic_routes: tuple[AnalysisTopicRouteSpec, ...] = ()
    # Keeping rules in the Candidate/Release contract makes runtime consumption
    # immutable: a published release carries the rules it was validated with.
    query_rules: tuple[QueryRuleContract, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def drop_retired_keys(cls, value: Any) -> Any:
        """Load catalogs persisted before ``upstreamCommit`` was retired.

        Releases store the catalog verbatim, and ``extra="forbid"`` would reject
        an older payload outright, so the retired key is dropped on read.
        """

        if isinstance(value, dict) and ("upstreamCommit" in value or "upstream_commit" in value):
            value = {
                key: item
                for key, item in value.items()
                if key not in {"upstreamCommit", "upstream_commit"}
            }
        return value

    @model_validator(mode="after")
    def validate_references(self) -> SemanticCatalog:
        models = _unique_by_id(self.models, "model")
        dimensions = _unique_by_id(self.dimensions, "dimension")
        metrics = _unique_by_id(self.metrics, "metric")
        dimension_values = _unique_by_id(self.dimension_values, "dimension value")
        context_entries = _unique_by_id(self.semantic_context, "semantic context")
        _unique_by_id(self.model_relations, "model relation")
        _unique_by_id(self.data_sets, "dataset")
        route_index = _unique_by_id(self.analysis_topic_routes, "analysis topic route")
        _unique_by_id(self.query_rules, "query rule")

        for relation in self.model_relations:
            _require(models, relation.from_model_id, f"relation {relation.id} from model")
            _require(models, relation.to_model_id, f"relation {relation.id} to model")
        for dimension in self.dimensions:
            _require(models, dimension.model_id, f"dimension {dimension.id} model")
        for dimension_value in dimension_values.values():
            _require(
                dimensions,
                dimension_value.dimension_id,
                f"dimension value {dimension_value.id} dimension",
            )
        for metric in self.metrics:
            _require(models, metric.model_id, f"metric {metric.id} model")
            params = metric.metric_define_by_metric_params
            if params is not None:
                for dependency in params.metrics:
                    _require(metrics, dependency.id, f"metric {metric.id} dependency")
        for data_set in self.data_sets:
            for model_config in data_set.data_set_detail.data_set_model_configs:
                _require(models, model_config.id, f"dataset {data_set.id} model")
                for dimension_id in model_config.dimensions:
                    dimension = _require(
                        dimensions,
                        dimension_id,
                        f"dataset {data_set.id} dimension",
                    )
                    if dimension.model_id != model_config.id:
                        raise ValueError("dataset dimension belongs to another model")
                for metric_id in model_config.metrics:
                    metric = _require(metrics, metric_id, f"dataset {data_set.id} metric")
                    if metric.model_id != model_config.id:
                        raise ValueError("dataset metric belongs to another model")
        datasets = {item.id: item for item in self.data_sets}
        relations = {item.id: item for item in self.model_relations}
        for route in route_index.values():
            data_set = _require(datasets, route.dataset_id, "analysis topic dataset")
            model_ids = {item.id for item in data_set.data_set_detail.data_set_model_configs}
            if route.root_model_id not in model_ids:
                raise ValueError("analysis topic root belongs to another dataset")
            for path in route.paths:
                if path.target_model_id not in model_ids:
                    raise ValueError("analysis topic target belongs to another dataset")
                for relation_id in path.relation_ids:
                    _require(relations, relation_id, "analysis topic relation")
        for entry in context_entries.values():
            targets = {
                "project": {self.project_id},
                "model": set(models),
                "metric": set(metrics),
                "dimension": set(dimensions),
                "query_scope": set(datasets),
            }[entry.target_type]
            if entry.target_id not in targets:
                raise ValueError(
                    f"semantic context {entry.id} targets an unknown {entry.target_type}"
                )
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def _unique_by_id(items: tuple[Any, ...], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        if item.id in result:
            raise ValueError(f"duplicate {label} id: {item.id}")
        result[item.id] = item
    return result


def _require(index: dict[str, Any], key: str, label: str) -> Any:
    value = index.get(key)
    if value is None:
        raise ValueError(f"unknown {label}: {key}")
    return value
