"""HypoPG-backed planner evaluation for PlanPatch index candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
import re
import sys
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import sqlparse

from planpatch.eligibility import check_eligibility


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5_000
MIN_COST_REDUCTION_PCT = 30.0
_PREPARED_STATEMENT_NAME = "planpatch_candidate"
_PLACEHOLDER_PATTERN = re.compile(r"\$([1-9][0-9]*)")
_INDEX_SCAN_NODE_TYPES = frozenset({"Index Scan", "Bitmap Index Scan"})

_COLUMN_TYPE_QUERY = """
SELECT format_type(attribute.atttypid, attribute.atttypmod)::text AS parameter_type
FROM pg_attribute AS attribute
WHERE attribute.attrelid = to_regclass(%s)
  AND attribute.attname = %s
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
"""

_CREATE_HYPOTHETICAL_INDEX_QUERY = """
SELECT
    indexrelid::bigint AS index_oid,
    indexname::text AS index_name
FROM hypopg_create_index(%s)
"""


@dataclass(frozen=True, slots=True)
class PlanEvidence:
    """Planner-cost evidence for one hypothetical index."""

    baseline_cost: float
    optimized_cost: float
    cost_reduction_pct: float
    used_virtual_index: bool
    is_viable: bool


class PlannerError(RuntimeError):
    """Raised when planner evidence cannot be produced safely."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        message = code if detail is None else f"{code}: {detail}"
        super().__init__(message)


def evaluate_candidate(
    db_url: str,
    query_string: str,
    table_name: str,
    column_name: str,
) -> PlanEvidence:
    """Compare baseline and HypoPG plans for a validated index candidate.

    The original normalized query is prepared with its catalog-derived
    parameter type. ``force_generic_plan`` ensures planning does not depend on
    a fabricated parameter value.
    """
    _validate_inputs(db_url, query_string, table_name, column_name)
    prepared_query = _prepare_query_text(query_string)
    parameter_count = _parameter_count(prepared_query)

    try:
        connection = psycopg.connect(
            db_url,
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            row_factory=dict_row,
        )
    except psycopg.Error as exc:
        raise PlannerError("database_connection_error", _database_error_detail(exc)) from exc

    with connection:
        with connection.cursor() as cursor:
            try:
                cursor.execute("SET plan_cache_mode = force_generic_plan")
                parameter_type = _load_parameter_type(
                    cursor,
                    table_name=table_name,
                    column_name=column_name,
                )
                _prepare_statement(
                    cursor,
                    query_string=prepared_query,
                    parameter_type=parameter_type,
                    parameter_count=parameter_count,
                )

                baseline_plan = _explain_prepared(cursor, parameter_count)
                baseline_cost = _root_total_cost(baseline_plan)

                virtual_index_name = _create_virtual_index(
                    cursor,
                    connection=connection,
                    table_name=table_name,
                    column_name=column_name,
                )
                # PostgreSQL caches a generic plan after the first EXPLAIN.
                # HypoPG's session-local index creation does not invalidate
                # that prepared plan, so re-prepare the identical statement.
                _deallocate_statement(cursor)
                _prepare_statement(
                    cursor,
                    query_string=prepared_query,
                    parameter_type=parameter_type,
                    parameter_count=parameter_count,
                )

                optimized_plan = _explain_prepared(cursor, parameter_count)
                optimized_cost = _root_total_cost(optimized_plan)
                used_virtual_index = _plan_uses_index(
                    optimized_plan,
                    virtual_index_name,
                )
            except PlannerError:
                raise
            except psycopg.Error as exc:
                raise PlannerError("planner_database_error", _database_error_detail(exc)) from exc
            finally:
                active_exception = sys.exc_info()[0] is not None
                try:
                    cursor.execute("SELECT hypopg_reset()")
                except psycopg.Error as exc:
                    # Do not hide the original planning failure. Closing the
                    # session remains a second cleanup boundary for HypoPG's
                    # session-local state.
                    if not active_exception:
                        raise PlannerError(
                            "hypopg_cleanup_error",
                            _database_error_detail(exc),
                        ) from exc

    cost_reduction_pct = _cost_reduction_pct(baseline_cost, optimized_cost)
    return PlanEvidence(
        baseline_cost=baseline_cost,
        optimized_cost=optimized_cost,
        cost_reduction_pct=cost_reduction_pct,
        used_virtual_index=used_virtual_index,
        is_viable=(
            used_virtual_index
            and cost_reduction_pct >= MIN_COST_REDUCTION_PCT
        ),
    )


def _validate_inputs(
    db_url: str,
    query_string: str,
    table_name: str,
    column_name: str,
) -> None:
    if not db_url.strip():
        raise PlannerError("invalid_database_url")

    eligibility = check_eligibility(query_string)
    if not eligibility.is_eligible:
        reason = eligibility.rejection_reason or "unsupported_sql"
        raise PlannerError(reason)
    if eligibility.table_name != table_name:
        raise PlannerError("candidate_table_mismatch")
    if eligibility.candidate_column != column_name:
        raise PlannerError("candidate_column_mismatch")


def _prepare_query_text(query_string: str) -> str:
    return sqlparse.format(query_string, strip_comments=True).strip().removesuffix(";").strip()


def _parameter_count(query_string: str) -> int:
    positions = tuple(int(match) for match in _PLACEHOLDER_PATTERN.findall(query_string))
    if not positions:
        raise PlannerError("missing_query_parameter")
    return max(positions)


def _load_parameter_type(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    table_name: str,
    column_name: str,
) -> str:
    cursor.execute(_COLUMN_TYPE_QUERY, (table_name, column_name))
    row = cursor.fetchone()
    if row is None:
        raise PlannerError("candidate_column_not_found")

    parameter_type = row.get("parameter_type")
    if not isinstance(parameter_type, str) or not parameter_type:
        raise PlannerError("invalid_candidate_column_type")
    return parameter_type


def _prepare_statement(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    query_string: str,
    parameter_type: str,
    parameter_count: int,
) -> None:
    parameter_types = sql.SQL(", ").join(
        sql.SQL(parameter_type) for _ in range(parameter_count)
    )
    statement = sql.SQL("PREPARE {} ({}) AS {}").format(
        sql.Identifier(_PREPARED_STATEMENT_NAME),
        parameter_types,
        sql.SQL(query_string),
    )
    cursor.execute(statement)


def _explain_prepared(
    cursor: psycopg.Cursor[dict[str, Any]],
    parameter_count: int,
) -> Mapping[str, object]:
    arguments = sql.SQL(", ").join(sql.SQL("NULL") for _ in range(parameter_count))
    statement = sql.SQL("EXPLAIN (FORMAT JSON) EXECUTE {} ({})").format(
        sql.Identifier(_PREPARED_STATEMENT_NAME),
        arguments,
    )
    cursor.execute(statement)
    row = cursor.fetchone()
    if row is None:
        raise PlannerError("missing_explain_result")
    return _parse_explain_json(row.get("QUERY PLAN"))


def _deallocate_statement(cursor: psycopg.Cursor[dict[str, Any]]) -> None:
    cursor.execute(
        sql.SQL("DEALLOCATE {}").format(
            sql.Identifier(_PREPARED_STATEMENT_NAME),
        )
    )


def _create_virtual_index(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    connection: psycopg.Connection[dict[str, Any]],
    table_name: str,
    column_name: str,
) -> str:
    relation_parts = _split_qualified_identifier(table_name)
    index_definition = sql.SQL("CREATE INDEX ON {} ({})").format(
        sql.Identifier(*relation_parts),
        sql.Identifier(column_name),
    ).as_string(connection)

    cursor.execute(_CREATE_HYPOTHETICAL_INDEX_QUERY, (index_definition,))
    row = cursor.fetchone()
    if row is None:
        raise PlannerError("hypopg_index_not_created")

    index_name = row.get("index_name")
    if not isinstance(index_name, str) or not index_name:
        raise PlannerError("invalid_hypopg_index_name")
    return index_name


def _parse_explain_json(raw_value: object) -> Mapping[str, object]:
    value = raw_value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PlannerError("invalid_explain_json", str(exc)) from exc

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PlannerError("invalid_explain_json", "expected a JSON array")
    if len(value) != 1 or not isinstance(value[0], Mapping):
        raise PlannerError("invalid_explain_json", "expected one plan document")

    plan = value[0].get("Plan")
    if not isinstance(plan, Mapping):
        raise PlannerError("invalid_explain_json", "missing Plan object")
    return plan


def _root_total_cost(plan: Mapping[str, object]) -> float:
    raw_cost = plan.get("Total Cost")
    if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)):
        raise PlannerError("invalid_explain_json", "invalid Total Cost")

    total_cost = float(raw_cost)
    if not math.isfinite(total_cost) or total_cost <= 0.0:
        raise PlannerError("invalid_explain_json", "Total Cost must be positive")
    return total_cost


def _plan_uses_index(plan: Mapping[str, object], index_name: str) -> bool:
    if (
        plan.get("Node Type") in _INDEX_SCAN_NODE_TYPES
        and plan.get("Index Name") == index_name
    ):
        return True

    children = plan.get("Plans", ())
    if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
        raise PlannerError("invalid_explain_json", "Plans must be an array")

    for child in children:
        if not isinstance(child, Mapping):
            raise PlannerError("invalid_explain_json", "invalid child plan")
        if _plan_uses_index(child, index_name):
            return True
    return False


def _cost_reduction_pct(baseline_cost: float, optimized_cost: float) -> float:
    if baseline_cost <= 0.0:
        raise PlannerError("invalid_baseline_cost")
    reduction = ((baseline_cost - optimized_cost) / baseline_cost) * 100.0
    if not math.isfinite(reduction):
        raise PlannerError("invalid_cost_reduction")
    return reduction


def _split_qualified_identifier(identifier: str) -> tuple[str, ...]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    index = 0

    while index < len(identifier):
        character = identifier[index]
        if character == '"':
            if quoted and index + 1 < len(identifier) and identifier[index + 1] == '"':
                current.append('"')
                index += 2
                continue
            quoted = not quoted
        elif character == "." and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
        index += 1

    if quoted:
        raise PlannerError("invalid_table_identifier")
    parts.append("".join(current))
    if not 1 <= len(parts) <= 2 or any(not part for part in parts):
        raise PlannerError("invalid_table_identifier")
    return tuple(parts)


def _database_error_detail(exc: psycopg.Error) -> str:
    primary = getattr(exc.diag, "message_primary", None)
    return str(primary or "PostgreSQL operation failed")
