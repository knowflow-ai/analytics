"""S3 排除法分类：按顺序第一条命中即定，带置信度和理由。

现在 rule_modeller 的规则是 ``numeric → measure SUM 0.65``，一刀切：``year``、
``status_code``、``zip`` 全被标成可加度量。这类静默错数只有画像能拦——规则 7 / 8
是那条拦截线。表角色（S2）让同一张表的数值列共享先验：事实表里是度量，维度表里
几乎不是。

``confidence < REVIEW_THRESHOLD`` 的列交给 S5 让模型确认或推翻。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field

from knowflow_analytics.contracts import Aggregation, FieldKind, FrozenModel
from knowflow_analytics.modeling.contracts import SchemaColumnSnapshot, TableSnapshot
from knowflow_analytics.modeling.profile import ColumnProfile, TableProfile
from knowflow_analytics.modeling.type_system import (
    is_numeric_type,
    is_temporal_type,
    is_text_type,
)

REVIEW_THRESHOLD = 0.8


class TableRole(StrEnum):
    FACT = "fact"
    DIMENSION = "dimension"
    BRIDGE = "bridge"
    LOOKUP = "lookup"


# 数值列名里带这些后缀的，几乎从不是可加度量。
# 中文列名没有 _ 分隔符，英文的 (^|_)…$ 锚定完全失效——「首套房贷款利率」曾因此
# 走 numeric→SUM 免审。中文部分：编码类锚定结尾（「500强排名」「状态编码」），
# 比率类不锚定（「首套房首付比例」词在中间）。
_CODE_LIKE = re.compile(
    r"(^|_)(id|no|num|code|key|year|month|day|week|quarter|status|state|type|level|"
    r"grade|flag|seq|rank|tier|cls|class|cat|category)$"
    r"|(编码|编号|代码|类别|类型|状态|等级|级别|排名|名次|年份|月份|季度|标志|标识)$",
    re.IGNORECASE,
)
_RATE_LIKE = re.compile(
    r"(rate|ratio|pct|percent|avg|mean|score|index|price|unit_cost"
    r"|比例|比率|占比|利率|费率|汇率|折扣|单价|均价|平均|指数|评分|得分|率)",
    re.IGNORECASE,
)
_TIME_NAME = re.compile(r"(^|_)(date|time|at|ts|dt)$|(日期|时间)", re.IGNORECASE)
_BOOL_TYPE = re.compile(r"^bool", re.IGNORECASE)


class Prefill(FrozenModel):
    column: str
    kind: FieldKind
    identifier_type: Literal["primary", "foreign"] | None = None
    dimension_type: Literal["categorical", "time", "partition_time"] | None = None
    aggregation: Aggregation | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=400)
    # 判断域：结论建立在列名启发式或弱先验上的规则（5/7/8/9/10/11/13/14）。
    # 数据库事实（主外键、时间类型）、唯一率、低基数文本和布尔不属于判断域。
    judgment_zone: bool = False
    # 盲判分歧：规则与模型独立判定不一致，默认采盲判结论，升级为人工决策。
    disputed: bool = False
    # 模型复核未执行（网关失败或返回不合法）：与"模型确认过"必须可区分。
    review_skipped: bool = False

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD


def classify_table(
    table: TableSnapshot,
    *,
    role: TableRole,
    profile: TableProfile | None,
    foreign_key_columns: frozenset[str],
) -> tuple[Prefill, ...]:
    return tuple(
        _classify_column(
            column,
            role=role,
            profile=profile.column(column.name) if profile else None,
            row_count=profile.row_count if profile else 0,
            is_foreign=column.name in foreign_key_columns,
        )
        for column in table.columns
    )


def _classify_column(
    column: SchemaColumnSnapshot,
    *,
    role: TableRole,
    profile: ColumnProfile | None,
    row_count: int,
    is_foreign: bool,
) -> Prefill:
    name, dtype = column.name, column.data_type
    numeric = is_numeric_type(dtype)
    distinct = profile.distinct_count if profile else None
    ratio = profile.distinct_ratio if profile else None

    def out(kind, conf, reason, *, zone=False, **extra):
        return Prefill(
            column=name, kind=kind, confidence=conf, reason=reason, judgment_zone=zone, **extra
        )

    # 1–2 数据库约束：权威事实
    if column.primary_key:
        return out(FieldKind.IDENTIFIER, 1.0, "数据库主键", identifier_type="primary")
    if is_foreign:
        return out(FieldKind.IDENTIFIER, 1.0, "数据库外键", identifier_type="foreign")
    # 3 时间类型 —— 必须先于唯一率：时间戳天然几乎每行不同，不是键
    if is_temporal_type(dtype):
        return out(FieldKind.TIME, 0.95, "时间类型", dimension_type="time")
    # 4 几乎每行不同：是键不是度量
    if ratio is not None and ratio >= 0.95 and row_count >= 100:
        return out(
            FieldKind.IDENTIFIER,
            0.9,
            f"唯一率 {ratio:.0%}，几乎每行不同",
            identifier_type="primary",
        )
    # 5 列名像时间但类型不是
    if _TIME_NAME.search(name) and (numeric or is_text_type(dtype)):
        return out(
            FieldKind.TIME, 0.6, "列名像时间但类型不是，需确认", zone=True, dimension_type="time"
        )
    # 6 低基数文本 → 分类
    if not numeric and distinct is not None and distinct <= 50:
        return out(
            FieldKind.DIMENSION,
            0.9,
            f"仅 {distinct} 个取值的文本列",
            dimension_type="categorical",
        )
    # 7–8 数值但不是度量：静默错数的拦截线
    if numeric and _CODE_LIKE.search(name):
        return out(
            FieldKind.DIMENSION,
            0.85,
            "列名像编码/年份/状态，相加无业务意义",
            zone=True,
            dimension_type="categorical",
        )
    if numeric and distinct is not None and distinct < 100 and row_count >= 1_000:
        # 对"不能 SUM"很有把握，对"它到底是什么"没把握（年龄段？等级？）——交模型。
        return out(
            FieldKind.DIMENSION,
            0.75,
            f"数值列仅 {distinct} 个取值，更像分类而非度量",
            zone=True,
            dimension_type="categorical",
        )
    # 9–10 事实表里的数值度量：比率 / 单价按行相加无意义，默认 AVG；其余 SUM
    if numeric and role is TableRole.FACT and _RATE_LIKE.search(name):
        return out(
            FieldKind.MEASURE,
            0.75,
            "列名像比率/单价，按行相加无意义",
            zone=True,
            aggregation=Aggregation.AVG,
        )
    if numeric and role is TableRole.FACT:
        return out(
            FieldKind.MEASURE, 0.8, "事实表中的数值列", zone=True, aggregation=Aggregation.SUM
        )
    # 11 维度表里的数值：属性居多，交模型
    if numeric:
        return out(
            FieldKind.DIMENSION,
            0.5,
            f"{role.value} 表中的数值列，通常是属性而非度量",
            zone=True,
            dimension_type="categorical",
        )
    # 12 布尔
    if _BOOL_TYPE.search(dtype):
        return out(FieldKind.DIMENSION, 0.9, "布尔列", dimension_type="categorical")
    # 13 高基数文本：可能是名称/描述/自由文本
    if is_text_type(dtype):
        return out(
            FieldKind.DIMENSION,
            0.6,
            "高基数文本，可能是名称或自由文本",
            zone=True,
            dimension_type="categorical",
        )
    # 14 其余
    return out(FieldKind.FIELD, 0.3, "没有足够证据分类", zone=True)


def rule_based_role(
    table: TableSnapshot, *, in_degree: int, out_degree: int, prefills_numeric_non_key: int
) -> TableRole:
    """S2 模型不可用时的兜底。保守偏 fact：宁可多度量让人改，不要漏度量。"""

    non_key_columns = [c for c in table.columns if not c.primary_key]
    if out_degree >= 2 and not non_key_columns:
        return TableRole.BRIDGE
    if in_degree >= 2 and out_degree == 0:
        return TableRole.DIMENSION
    # 码表：只有 code + name 两列那种。三列的客户表不是码表。
    if (
        len(non_key_columns) <= 2
        and any(re.search(r"(^|_)code$", c.name, re.I) for c in table.columns)
        and any(re.search(r"(^|_)name$", c.name, re.I) for c in table.columns)
    ):
        return TableRole.LOOKUP
    if out_degree >= 1 and prefills_numeric_non_key >= 1:
        return TableRole.FACT
    if in_degree >= 1 and out_degree == 0:
        return TableRole.DIMENSION
    return TableRole.FACT
