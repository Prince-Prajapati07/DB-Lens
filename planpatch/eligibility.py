"""Conservative SQL eligibility checks for PlanPatch index candidates."""

from __future__ import annotations

from dataclasses import dataclass
import re

import sqlparse
from sqlparse.sql import Parenthesis, Statement, TokenList
from sqlparse.tokens import Comment, Keyword, Whitespace


_IDENTIFIER = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_RELATION = rf'{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?'
_SUPPORTED_QUERY = re.compile(
    rf"""
    \A\s*SELECT\b.+?\bFROM\s+
    (?P<table>{_RELATION})
    (?:\s+(?:AS\s+)?(?P<alias>{_IDENTIFIER}))?
    \s+WHERE\s+
    (?:(?P<qualifier>{_IDENTIFIER})\s*\.\s*)?
    (?P<column>{_IDENTIFIER})
    \s*=\s*\$(?P<parameter>[1-9][0-9]*)
    \s*;?\s*\Z
    """,
    flags=re.IGNORECASE | re.DOTALL | re.VERBOSE,
)
_FROM_CLAUSE = re.compile(
    r"\bFROM\b(?P<from_clause>.*?)(?:\bWHERE\b|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Result of applying PlanPatch's deliberately narrow SQL contract."""

    is_eligible: bool
    rejection_reason: str | None = None
    table_name: str | None = None
    candidate_column: str | None = None


def check_eligibility(query_string: str) -> EligibilityResult:
    """Return whether a query can safely produce a single-column candidate.

    The accepted grammar is intentionally limited to a single SELECT relation
    and one ``column = $n`` predicate. Unsupported SQL is rejected rather than
    interpreted heuristically.
    """
    if not query_string.strip():
        return _rejected("empty_query")

    query_without_comments = sqlparse.format(query_string, strip_comments=True).strip()
    statements = tuple(
        statement
        for statement in sqlparse.parse(query_without_comments)
        if _has_meaningful_tokens(statement)
    )
    if len(statements) != 1:
        return _rejected("multiple_statements")

    statement = statements[0]
    if statement.get_type() != "SELECT":
        return _rejected("unsupported_sql_not_select")

    flattened_keywords = tuple(
        token.normalized.upper()
        for token in statement.flatten()
        if token.ttype in Keyword
    )
    if "WITH" in flattened_keywords:
        return _rejected("unsupported_sql_cte")
    if any("JOIN" in keyword for keyword in flattened_keywords):
        return _rejected("unsupported_sql_join")
    if any(keyword.startswith("UNION") for keyword in flattened_keywords):
        return _rejected("unsupported_sql_union")
    if _contains_subquery(statement):
        return _rejected("unsupported_sql_subquery")

    from_match = _FROM_CLAUSE.search(query_without_comments)
    if from_match is None:
        return _rejected("missing_from_clause")

    from_clause = from_match.group("from_clause")
    if "," in from_clause:
        return _rejected("multiple_tables")
    if not re.search(r"\bWHERE\b", query_without_comments, flags=re.IGNORECASE):
        return _rejected("missing_where_clause")

    match = _SUPPORTED_QUERY.fullmatch(query_without_comments)
    if match is None:
        return _rejected("unsupported_where_predicate")

    table_name = _canonical_qualified_identifier(match.group("table"))
    column_name = _unquote_identifier(match.group("column"))
    qualifier = match.group("qualifier")
    if qualifier is not None:
        expected_qualifier = match.group("alias") or match.group("table").split(".")[-1]
        if _unquote_identifier(qualifier) != _unquote_identifier(
            expected_qualifier.strip()
        ):
            return _rejected("invalid_column_qualifier")

    return EligibilityResult(
        is_eligible=True,
        table_name=table_name,
        candidate_column=column_name,
    )


def _rejected(reason: str) -> EligibilityResult:
    return EligibilityResult(is_eligible=False, rejection_reason=reason)


def _has_meaningful_tokens(statement: Statement) -> bool:
    return any(
        not token.is_whitespace and token.ttype not in Comment
        for token in statement.tokens
    )


def _contains_subquery(token_list: TokenList) -> bool:
    for token in token_list.tokens:
        if isinstance(token, Parenthesis):
            if any(
                child.normalized.upper() == "SELECT"
                for child in token.flatten()
                if child.ttype not in Whitespace
            ):
                return True
        if isinstance(token, TokenList) and _contains_subquery(token):
            return True
    return False


def _canonical_qualified_identifier(value: str) -> str:
    return ".".join(
        _canonical_relation_part(part.strip())
        for part in re.split(r"\s*\.\s*", value)
    )


def _canonical_relation_part(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value
    return value.lower()


def _unquote_identifier(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.lower()
