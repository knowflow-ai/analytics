from __future__ import annotations

import re
from datetime import date

from knowflow_analytics.contracts import DEFAULT_DATE_FORMAT, Aggregation

_NUMERIC_TYPE = re.compile(
    r"^(?:smallint|integer|bigint|decimal|numeric|real|double\s+precision|"
    r"smallserial|serial|bigserial|money|float\d*|int[248]?)(?:\b|\s*\()",
    re.IGNORECASE,
)
_TEMPORAL_TYPE = re.compile(
    r"^(?:date|time|timestamp)(?:\b|\s*\()",
    re.IGNORECASE,
)
_TEXT_TYPE = re.compile(
    r"^(?:text|citext|character(?:\s+varying)?|varchar|char)(?:\b|\s*\()",
    re.IGNORECASE,
)


def normalize_database_type(data_type: str) -> str:
    return " ".join(data_type.strip().casefold().split())


def is_numeric_type(data_type: str) -> bool:
    return _NUMERIC_TYPE.search(normalize_database_type(data_type)) is not None


def is_temporal_type(data_type: str) -> bool:
    return _TEMPORAL_TYPE.search(normalize_database_type(data_type)) is not None


def is_additive_type(data_type: str) -> bool:
    normalized = normalize_database_type(data_type)
    return is_numeric_type(normalized) or normalized == "interval"


def is_text_type(data_type: str) -> bool:
    return _TEXT_TYPE.search(normalize_database_type(data_type)) is not None


def types_can_join(left_type: str, right_type: str) -> bool:
    """Conservative PostgreSQL equality compatibility for governed Join keys."""

    left = normalize_database_type(left_type)
    right = normalize_database_type(right_type)
    if left == right:
        return True
    if is_numeric_type(left) and is_numeric_type(right):
        return True
    if is_text_type(left) and is_text_type(right):
        return True
    left_temporal = _temporal_join_family(left)
    right_temporal = _temporal_join_family(right)
    return left_temporal is not None and left_temporal == right_temporal


def _temporal_join_family(data_type: str) -> str | None:
    if data_type == "date" or data_type.startswith("timestamp"):
        return "date_timestamp"
    if data_type == "time" or data_type.startswith("time ") or data_type.startswith("time("):
        return "time"
    return None


def aggregation_accepts_type(aggregation: Aggregation, data_type: str) -> bool:
    """Return whether PostgreSQL can apply a governed aggregation safely.

    SUM and AVG have a strict additive input contract: PostgreSQL numeric types and
    interval. COUNT variants are defined for arbitrary scalar fields. MIN/MAX depends on ordering
    operators and is therefore left to the existing translation/dry-run gate instead
    of pretending this small type registry covers every extension type.
    """

    if aggregation in {Aggregation.SUM, Aggregation.AVG}:
        return is_additive_type(data_type)
    return True


# --- 时间过滤字面量 -----------------------------------------------------------

# Java SimpleDateFormat -> strftime。按 token 长度降序替换，避免 "yyyy" 被
# "yy" 先吃掉。只覆盖日期部分：时间过滤的边界都是整日。
_DATE_FORMAT_TOKENS: tuple[tuple[str, str], ...] = (
    ("yyyy", "%Y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("yy", "%y"),
)


def java_date_format_to_strftime(date_format: str) -> str:
    """把 Java 风格的日期格式串转成 strftime 格式串。

    不认识的字符原样保留（如 ``yyyy年MM月dd日`` 中的汉字），因为建模者写下的
    分隔符就是列里真实存在的分隔符。
    """

    rendered = date_format
    for token, replacement in _DATE_FORMAT_TOKENS:
        rendered = rendered.replace(token, replacement)
    return rendered


def render_time_bound(
    bound: date,
    *,
    data_type: str | None,
    date_format: str | None,
) -> date | str:
    """把时间过滤的边界渲染成与物理列可比较的值。

    分区时间列不一定是数据库的日期类型：``stat_date int``（20260802）、
    ``dt varchar(8)`` 都很常见，我们自己的规则分类器（``classify.py`` 的
    「列名像时间但类型不是」分支）也会把它们判成时间维度。PG16 实测::

        int     >= date  ->  operator does not exist: integer >= date
        varchar >= date  ->  operator does not exist: character varying >= date
        date    >= date  ->  正常

    按建模期录入的 ``dateFormat`` 渲染成字符串后，int 与 varchar 两种列都能正常
    比较（PostgreSQL 会把未定型字面量强制转换成列类型）。日期/时间戳列保持
    ``date`` 对象——参数化绑定最精确。``data_type`` 缺失时保持既有行为。

    对齐上游 ``TimeCorrector`` 用 ``dateFormat`` 格式化它自动补上的时间区间。
    """

    if data_type is None or is_temporal_type(data_type):
        return bound
    pattern = java_date_format_to_strftime(date_format or DEFAULT_DATE_FORMAT)
    return bound.strftime(pattern)
