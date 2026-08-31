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
    """Port ``QueryTypeParser``: aggregate functions decide the query type."""

    tree = validate_textual_s2sql(s2sql)
    selects = list(tree.find_all(exp.Select))
    if isinstance(tree, exp.Select) and not any(item is tree for item in selects):
        selects.insert(0, tree)
    has_select_function = any(
        isinstance(projection, exp.Func) or projection.find(exp.Func) is not None
        for select in selects
        for projection in select.expressions
    )
    return SemanticQueryType.AGGREGATE if has_select_function else SemanticQueryType.DETAIL


def _invalid(message: str) -> SemanticParsingError:
    return SemanticParsingError(message, code="LLM_S2SQL_AST_INVALID")
