"""对已完成回答的文本 S2SQL 做确定性编辑：下钻与报表卡滚动窗都从这里改。

回答的语义权威是文本 S2SQL——``DATE_TRUNC`` 粒度、``RATIO_*`` 期间比、别名都只在
这里；``semantic_query`` 只是它的确定性投影。此前"继续一个回答"（下钻、钉卡片）拿
投影重跑：「按月咖啡的环比销售情况」下钻换个过滤值就变成「按天销售金额」——投影
表达不了的东西全丢，chip 上却写着只换了商品类别（2026-09-05 实机）。这与
``semantic_spec`` 被当成表单写入合同踩的是同一个坑。

编辑只接受「单条 SELECT 直接 FROM 数据集」：没有 CTE / 子查询 / 集合运算。其余形状
fail-closed（``CONTINUATION_SHAPE_UNSUPPORTED``），签发下钻选项时按同一判据不给选项，
用户不会看到一个注定失败的按钮。所有编辑只改 AST、不读问句、不调模型。
"""

from __future__ import annotations

from collections.abc import Callable

import sqlglot
from sqlglot import exp

from knowflow_analytics.query.errors import SemanticParsingError

UNSUPPORTED_CODE = "CONTINUATION_SHAPE_UNSUPPORTED"
_DIALECT = "postgres"
_RATIO_PREFIX = "RATIO_"
_SET_OPERATION = getattr(exp, "SetOperation", exp.Union)


def editable_select(s2sql: str) -> exp.Select | None:
    """只接受「单条 SELECT 直接 FROM 数据集」；其余形状返回 None。"""

    try:
        tree = sqlglot.parse_one(s2sql, read=_DIALECT)
    except sqlglot.errors.ParseError:
        return None
    if not isinstance(tree, exp.Select):
        return None
    if tree.find(exp.With) is not None:
        return None
    if tree.find(exp.Subquery) is not None or tree.find(_SET_OPERATION) is not None:
        return None
    if len(list(tree.find_all(exp.Table))) != 1:
        return None
    return tree


def replace_filter_value(s2sql: str, column: str, value: str) -> str:
    """把某个维度上的等值 / IN 过滤换成一个新值；没有就补一条。其余条件原样保留。"""

    tree = _require(s2sql)
    kept = [term for term in _conjuncts(tree) if not _is_value_predicate(term, column)]
    kept.append(exp.EQ(this=_column(column), expression=exp.Literal.string(value)))
    return _with_where(tree, kept).sql(dialect=_DIALECT)


def add_dimension(s2sql: str, column: str, *, is_metric: Callable[[str], bool]) -> str:
    """多按一个维度切：投影里跟在既有维度后面，GROUP BY 同步补上。"""

    tree = _require(s2sql)
    if any(_mentions(item, column) for item in tree.expressions):
        return tree.sql(dialect=_DIALECT)
    projections = list(tree.expressions)
    insert_at = 0
    for index, item in enumerate(projections):
        if not _is_metric_projection(item, is_metric):
            insert_at = index + 1
    projections.insert(insert_at, _column(column))
    tree.set("expressions", projections)
    group = tree.args.get("group")
    has_metric = any(_is_metric_projection(item, is_metric) for item in projections)
    if group is not None:
        group.set("expressions", [*group.expressions, _column(column)])
    elif has_metric:
        tree.set(
            "group",
            exp.Group(
                expressions=[
                    item.unalias().copy()
                    for item in projections
                    if not _is_metric_projection(item, is_metric)
                ]
            ),
        )
    return tree.sql(dialect=_DIALECT)


def remove_dimension(s2sql: str, column: str, *, is_metric: Callable[[str], bool]) -> str:
    """去掉一个分组维度：投影、GROUP BY、ORDER BY 里引用它的项一起去；值过滤保留
    ——"不按区域分组"与"只看华东"是两句独立的话。"""

    tree = _require(s2sql)
    removed_aliases: set[str] = set()
    kept_projections: list[exp.Expression] = []
    for item in tree.expressions:
        if _mentions(item, column) and not _is_metric_projection(item, is_metric):
            if isinstance(item, exp.Alias):
                removed_aliases.add(item.alias)
            continue
        kept_projections.append(item)
    if not kept_projections:
        raise _unsupported()
    tree.set("expressions", kept_projections)

    def _refers(node: exp.Expression) -> bool:
        return _mentions(node, column) or any(
            col.name in removed_aliases for col in node.find_all(exp.Column)
        )

    group = tree.args.get("group")
    if group is not None:
        remaining = [item for item in group.expressions if not _refers(item)]
        if remaining:
            group.set("expressions", remaining)
        else:
            tree.set("group", None)
    _prune_order(tree, _refers)
    return tree.sql(dialect=_DIALECT)


def replace_metric(s2sql: str, new_metric: str, *, is_metric: Callable[[str], bool]) -> str:
    """换指标：期间比这类**形状**保留（``RATIO_ROLL(旧) → RATIO_ROLL(新)``），普通
    聚合投影收敛成一个裸的新指标（口径由治理聚合决定）；引用旧指标的 HAVING 与
    ORDER BY 一起去掉——带着走要么校验失败，要么拿旧指标的值静默过滤新指标。"""

    tree = _require(s2sql)
    projections: list[exp.Expression] = []
    replaced_aliases: set[str] = set()
    bare_added = False
    for item in tree.expressions:
        if not _is_metric_projection(item, is_metric):
            projections.append(item)
            continue
        if isinstance(item, exp.Alias):
            replaced_aliases.add(item.alias)
        inner = item.unalias()
        if _is_ratio(inner):
            replaced = inner.copy()
            for col in replaced.find_all(exp.Column):
                if is_metric(col.name):
                    col.replace(_column(new_metric))
            if isinstance(item, exp.Alias):
                replaced = exp.alias_(replaced, item.alias, quoted=True)
            projections.append(replaced)
            continue
        if not bare_added:
            projections.append(_column(new_metric))
            bare_added = True
    tree.set("expressions", projections)
    tree.set("having", None)

    def _refers_to_a_metric(node: exp.Expression) -> bool:
        if node.find(exp.AggFunc) is not None:
            return True
        return any(
            is_metric(col.name) or col.name == new_metric or col.name in replaced_aliases
            for col in node.find_all(exp.Column)
        )

    _prune_order(tree, _refers_to_a_metric)
    return tree.sql(dialect=_DIALECT)

def set_time_window(s2sql: str, column: str, start: str | None) -> str:
    """把时间维上的范围条件整体换成一个下界；``start=None`` 即「不限时间」。"""

    tree = _require(s2sql)
    kept = [term for term in _conjuncts(tree) if not _mentions(term, column)]
    if start is not None:
        kept.append(exp.GTE(this=_column(column), expression=exp.Literal.string(start)))
    return _with_where(tree, kept).sql(dialect=_DIALECT)


def _require(s2sql: str) -> exp.Select:
    tree = editable_select(s2sql)
    if tree is None:
        raise _unsupported()
    return tree


def _unsupported() -> SemanticParsingError:
    return SemanticParsingError(
        "这个回答的查询形状不支持这样继续，请重新提问",
        code=UNSUPPORTED_CODE,
    )


def _column(name: str) -> exp.Column:
    return exp.column(name, quoted=True)


def _mentions(node: exp.Expression, column: str) -> bool:
    return any(col.name == column for col in node.find_all(exp.Column))


def _is_ratio(node: exp.Expression) -> bool:
    return any(
        str(item.name).upper().startswith(_RATIO_PREFIX) for item in node.find_all(exp.Anonymous)
    )


def _is_metric_projection(item: exp.Expression, is_metric: Callable[[str], bool]) -> bool:
    inner = item.unalias()
    if isinstance(inner, exp.Column):
        return is_metric(inner.name)
    if inner.find(exp.AggFunc) is not None or _is_ratio(inner):
        return True
    return any(is_metric(col.name) for col in inner.find_all(exp.Column))


def _is_value_predicate(term: exp.Expression, column: str) -> bool:
    return (
        isinstance(term, (exp.EQ, exp.In))
        and isinstance(term.this, exp.Column)
        and term.this.name == column
    )


def _conjuncts(tree: exp.Select) -> list[exp.Expression]:
    where = tree.args.get("where")
    if where is None:
        return []
    condition = where.this
    if isinstance(condition, exp.And):
        return list(condition.flatten())
    return [condition]


def _with_where(tree: exp.Select, conjuncts: list[exp.Expression]) -> exp.Select:
    if not conjuncts:
        tree.set("where", None)
        return tree
    condition = conjuncts[0]
    for term in conjuncts[1:]:
        condition = exp.And(this=condition, expression=term)
    tree.set("where", exp.Where(this=condition))
    return tree


def _prune_order(tree: exp.Select, refers: Callable[[exp.Expression], bool]) -> None:
    order = tree.args.get("order")
    if order is None:
        return
    remaining = [item for item in order.expressions if not refers(item)]
    if remaining:
        order.set("expressions", remaining)
    else:
        tree.set("order", None)
