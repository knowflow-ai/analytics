from __future__ import annotations

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from knowflow_analytics.contracts import SemanticQueryType
from knowflow_analytics.query.errors import SemanticParsingError


def validate_textual_s2sql(s2sql: str) -> exp.Query:
    """Validate the textual S2SQL boundary without changing its shape.

    Parity source: ``LLMResponseService.getDeduplicationSqlResp`` delegates only
    ordinary SQL syntax validation before ``SemanticParseInfo`` stores the text.
    Semantic names and expressions are handled later by ``SqlQueryParser`` and the
    Translator Parser Registry.
    """

    try:
        statements = parse(s2sql, read="postgres")
    except ParseError as exc:
        raise _invalid("S2SQL is not valid SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise _invalid("S2SQL must contain exactly one query statement")
    statement = statements[0]
    # Candidate-admission parity with SuperSonic's pinned JSQLParser 4.9:
    # SQLGlot accepts incomplete text such as ``SELECT`` and ``SELECT FROM t``
    # as an empty Select AST, while JSQLParser rejects it before LLMSqlParser
    # can add a candidate.  This is structural validation only; it never fills
    # in a projection or otherwise invents query meaning.
    if any(not select.expressions for select in statement.find_all(exp.Select)):
        raise _invalid("S2SQL SELECT must include at least one projection")
    return statement


def textual_query_type(s2sql: str) -> SemanticQueryType:
    """Port ``QueryTypeParser``: aggregate functions in the projection decide the type.

    上游只看 SELECT 项。带 GROUP BY 的语句按 SQL 定义就是聚合查询，即使聚合函数
    只出现在 ORDER BY / HAVING 里（实机「卖得最好的产品是哪个」：模型写
    ``SELECT 商品名称 … GROUP BY 商品名称 ORDER BY SUM(销售数量) DESC LIMIT 1``，
    判成明细后 ORDER BY 里的聚合被当成"明细查询改写指标口径"拒掉，整条失败）。
    这是 2026-08-27「已有 GROUP BY 不得留下过期的 DETAIL 判定」合同的同一条规则。
    """

    tree = validate_textual_s2sql(s2sql)
    selects = list(tree.find_all(exp.Select))
    if isinstance(tree, exp.Select) and not any(item is tree for item in selects):
        selects.insert(0, tree)
    has_select_function = any(
        isinstance(projection, exp.Func) or projection.find(exp.Func) is not None
        for select in selects
        for projection in select.expressions
    )
    has_group_by = any(select.args.get("group") is not None for select in selects)
    if has_select_function or has_group_by:
        return SemanticQueryType.AGGREGATE
    return SemanticQueryType.DETAIL


def _invalid(message: str) -> SemanticParsingError:
    return SemanticParsingError(message, code="LLM_S2SQL_AST_INVALID")
