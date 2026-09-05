"""自然语言问数的默认时间窗：问题没说时间范围时，确定性地补一个窗并记下来。

对齐上游 ``TimeCorrector.addDateIfNotExist``，但守住两条本仓库的底线：

- **用户明确说的时间绝不动。** S2SQL 里只要在任何谓词里引用了本数据集的任一时间
  维度，就一个字都不改。宁可漏补，不可改错。
- **补了必须可见、可撤。** 返回的 ``marker`` 进 ``applied_defaults``，服务层据此在
  回答里单独标出「默认只看最近 7 天」，并挂上现成的换窗 / 不限时间下钻。

只处理最简单的形状：单条 SELECT、直接 FROM 数据集、没有 WITH / 子查询 / 集合运算、
没有同比环比。排名、CTE、期间比这些形状里补窗的位置和语义都不唯一，那就不补——
判定是确定性的，而且每一种跳过都不改变今天的行为。不抄上游的「没有分区维度就把
用户写的时间条件删掉」：那是静默丢条件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlglot import exp

from knowflow_analytics.contracts import (
    DatasetSpec,
    DatasetTimeDefaultConfig,
    SemanticRelease,
    time_window_label,
)
from knowflow_analytics.query.contracts import DefaultTimeWindow
from knowflow_analytics.query.errors import SemanticParsingError
from knowflow_analytics.query.parser import _default_time_range
from knowflow_analytics.query.s2sql_ast import validate_textual_s2sql
from knowflow_analytics.query.symbols import SemanticSymbolTable

# 标记格式与规则路径完全一致：time:<维度 id>:<起>:<止>:<窗口名>。
TIME_WINDOW_MARKER = "time:"


@dataclass(frozen=True)
class InjectedTimeWindow:
    s2sql: str
    marker: str
    window: DefaultTimeWindow


@dataclass(frozen=True)
class ParsedTimeWindowMarker:
    dimension_id: str
    start: str
    end: str
    label: str


def parse_time_window_marker(marker: str) -> ParsedTimeWindowMarker | None:
    """读回 ``applied_defaults`` 里的时间窗标记；认不出的返回 None，不猜。"""

    if not marker.startswith(TIME_WINDOW_MARKER):
        return None
    parts = marker.split(":", 4)
    if len(parts) < 4 or not parts[1] or not parts[2] or not parts[3]:
        return None
    label = parts[4] if len(parts) == 5 and parts[4] else f"{parts[2]} 起"
    return ParsedTimeWindowMarker(dimension_id=parts[1], start=parts[2], end=parts[3], label=label)


def temporal_dimension_ids(release: SemanticRelease, dataset: DatasetSpec) -> frozenset[str]:
    ids = {
        item.id
        for item in release.dimensions
        if item.id in dataset.dimension_ids and item.semantic_type == "time"
    }
    if dataset.default_time_dimension_id is not None:
        ids.add(dataset.default_time_dimension_id)
    return frozenset(ids)


def _predicate_columns(tree: exp.Select) -> list[str]:
    """WHERE / HAVING / JOIN ON 里引用的列名。只看谓词：分组或投影里出现时间维不算
    「用户给了时间范围」。"""

    names: list[str] = []
    scopes: list[exp.Expression] = []
    where = tree.args.get("where")
    having = tree.args.get("having")
    if where is not None:
        scopes.append(where)
    if having is not None:
        scopes.append(having)
    for join in tree.args.get("joins") or ():
        on = join.args.get("on")
        if on is not None:
            scopes.append(on)
    for scope in scopes:
        names.extend(column.name for column in scope.find_all(exp.Column))
    return names


def _simple_select(tree: exp.Expression, symbols: SemanticSymbolTable) -> exp.Select | None:
    """只接受「单条 SELECT 直接 FROM 数据集」；其余形状返回 None。"""

    if not isinstance(tree, exp.Select):
        return None
    if tree.args.get("with_") is not None:
        return None
    if tree.find(exp.Subquery) is not None or tree.find(exp.Union) is not None:
        return None
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or not symbols.is_dataset(tables[0].name):
        return None
    return tree


def inject_default_time_window(
    *,
    s2sql: str,
    release: SemanticRelease,
    dataset: DatasetSpec,
    config: DatasetTimeDefaultConfig,
    now: datetime | None = None,
) -> InjectedTimeWindow | None:
    """给没写时间范围的简单查询补上默认窗；任何不确定的情况都返回 None（不补）。"""

    if dataset.default_time_dimension_id is None:
        return None
    upper = s2sql.upper()
    # 期间比要拿历史期做分母，窗口会把分母切掉；不补。
    if "RATIO_OVER(" in upper or "RATIO_ROLL(" in upper:
        return None
    try:
        tree = validate_textual_s2sql(s2sql)
    except SemanticParsingError:
        return None
    symbols = SemanticSymbolTable(release=release, dataset=dataset)
    select = _simple_select(tree, symbols)
    if select is None:
        return None
    temporal = temporal_dimension_ids(release, dataset)
    for name in _predicate_columns(select):
        try:
            resolved = symbols.resolve_first(name)
        except SemanticParsingError:
            # 谓词里有认不出的名字：不知道它是不是时间，那就当它可能是。
            return None
        if resolved.kind == "dimension" and resolved.id in temporal:
            return None

    normalized_now = now or datetime.now(UTC)
    if normalized_now.tzinfo is None:
        normalized_now = normalized_now.replace(tzinfo=UTC)
    business_today = normalized_now.astimezone(ZoneInfo(dataset.timezone)).date()
    start, end = _default_time_range(config, business_today)
    dimension_name = symbols.canonical_name(dataset.default_time_dimension_id)
    quoted = '"' + dimension_name.replace('"', '""') + '"'
    condition = exp.condition(
        f"{quoted} >= '{start.isoformat()}' AND {quoted} < '{end.isoformat()}'",
        dialect="postgres",
    )
    select.where(condition, copy=False)
    label = time_window_label(config)
    return InjectedTimeWindow(
        s2sql=select.sql(dialect="postgres"),
        marker=(
            f"{TIME_WINDOW_MARKER}{dataset.default_time_dimension_id}"
            f":{start.isoformat()}:{end.isoformat()}:{label}"
        ),
        window=DefaultTimeWindow(
            dimension=dimension_name,
            start=start.isoformat(),
            end=end.isoformat(),
            label=label,
        ),
    )
