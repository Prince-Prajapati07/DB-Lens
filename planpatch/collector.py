"""PostgreSQL pg_stat_statements collection for PlanPatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from planpatch.models import TargetConfig
from planpatch.config import mask_database_url
from planpatch.state import QueryCounters, Snapshot, target_identity_from_config

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5_000
PG_STAT_STATEMENTS_QUERY = """
SELECT
    dbid::text AS dbid,
    queryid::text AS queryid,
    query::text AS query,
    calls::bigint AS calls,
    total_exec_time::double precision AS total_exec_time_ms
FROM pg_stat_statements
WHERE dbid = (
    SELECT oid
    FROM pg_database
    WHERE datname = current_database()
)
AND queryid IS NOT NULL
ORDER BY total_exec_time DESC
"""


@dataclass(frozen=True, slots=True)
class RawStatement:
    """One raw row from pg_stat_statements."""

    dbid: str
    queryid: str
    query: str
    calls: int
    total_exec_time_ms: float

    @property
    def fingerprint(self) -> str:
        return f"{self.dbid}:{self.queryid}"


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """Raw PostgreSQL statistics collected at one point in time."""

    target_identity: str
    captured_at: datetime
    statements: tuple[RawStatement, ...]

    def to_snapshot(self) -> Snapshot:
        return Snapshot(
            target_identity=self.target_identity,
            captured_at=self.captured_at,
            queries={
                statement.fingerprint: QueryCounters(
                    calls=statement.calls,
                    total_exec_time_ms=statement.total_exec_time_ms,
                    query=statement.query,
                )
                for statement in self.statements
            },
        )


class CollectionError(RuntimeError):
    """Raised when PlanPatch cannot collect pg_stat_statements data."""


def collect_pg_stat_statements(target: TargetConfig) -> RawSnapshot:
    """Collect cumulative pg_stat_statements counters from PostgreSQL."""
    try:
        with psycopg.connect(
            target.db_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(STATEMENT_TIMEOUT_MS),),
                )
                cursor.execute(PG_STAT_STATEMENTS_QUERY)
                rows = cursor.fetchall()
    except psycopg.Error as exc:
        raise CollectionError(_sanitize_error_message(str(exc), target)) from exc

    return RawSnapshot(
        target_identity=target_identity_from_config(target),
        captured_at=datetime.now(UTC),
        statements=tuple(
            RawStatement(
                dbid=str(row["dbid"]),
                queryid=str(row["queryid"]),
                query=str(row["query"]),
                calls=int(row["calls"]),
                total_exec_time_ms=float(row["total_exec_time_ms"]),
            )
            for row in rows
        ),
    )


def _sanitize_error_message(message: str, target: TargetConfig) -> str:
    return message.replace(target.db_url, mask_database_url(target.db_url))
