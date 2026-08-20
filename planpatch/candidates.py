"""Read-only PostgreSQL safety gates for PlanPatch index candidates."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5_000
MIN_TABLE_SIZE_BYTES = 10 * 1024 * 1024

_RELATION_QUERY = """
SELECT
    relation.oid::bigint AS relation_oid,
    namespace.nspname::text AS schema_name,
    relation.relname::text AS table_name,
    pg_relation_size(relation.oid)::bigint AS table_size_bytes
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE relation.oid = to_regclass(%s)
  AND relation.relkind IN ('r', 'p')
  AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
"""

_COLUMN_QUERY = """
SELECT attribute.attnum::integer AS attribute_number
FROM pg_attribute AS attribute
WHERE attribute.attrelid = %s
  AND attribute.attname = %s
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
"""

_EXISTING_INDEX_QUERY = """
SELECT index_relation.relname::text AS index_name
FROM pg_index AS index_metadata
JOIN pg_class AS index_relation ON index_relation.oid = index_metadata.indexrelid
WHERE index_metadata.indrelid = %s
  AND index_metadata.indisvalid
  AND index_metadata.indisready
  AND index_metadata.indpred IS NULL
  AND index_metadata.indexprs IS NULL
  AND index_metadata.indnkeyatts = 1
  AND index_metadata.indnatts = 1
  AND index_metadata.indkey[0] = %s
LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Outcome of validating one proposed single-column index."""

    is_valid: bool
    table_name: str
    column_name: str
    rejection_reason: str | None = None
    table_size_bytes: int | None = None
    existing_index_name: str | None = None


def validate_candidate(
    db_url: str,
    table_name: str,
    column_name: str,
) -> CandidateResult:
    """Apply relation-size and existing-index gates without changing the DB."""
    if not db_url.strip():
        return _rejected(table_name, column_name, "invalid_database_url")
    if not table_name.strip():
        return _rejected(table_name, column_name, "invalid_table_name")
    if not column_name.strip():
        return _rejected(table_name, column_name, "invalid_column_name")

    try:
        with psycopg.connect(
            db_url,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            row_factory=dict_row,
        ) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(_RELATION_QUERY, (table_name,))
                relation = cursor.fetchone()
                if relation is None:
                    return _rejected(table_name, column_name, "relation_not_found")

                relation_oid = int(relation["relation_oid"])
                table_size_bytes = int(relation["table_size_bytes"])

                cursor.execute(_COLUMN_QUERY, (relation_oid, column_name))
                column = cursor.fetchone()
                if column is None:
                    return _rejected(
                        table_name,
                        column_name,
                        "column_not_found",
                        table_size_bytes=table_size_bytes,
                    )

                attribute_number = int(column["attribute_number"])
                cursor.execute(
                    _EXISTING_INDEX_QUERY,
                    (relation_oid, attribute_number),
                )
                existing_index = cursor.fetchone()
    except (psycopg.Error, ValueError, TypeError):
        return _rejected(table_name, column_name, "database_error")

    if existing_index is not None:
        return _rejected(
            table_name,
            column_name,
            "already_indexed",
            table_size_bytes=table_size_bytes,
            existing_index_name=str(existing_index["index_name"]),
        )
    if table_size_bytes < MIN_TABLE_SIZE_BYTES:
        return _rejected(
            table_name,
            column_name,
            "small_table",
            table_size_bytes=table_size_bytes,
        )

    return CandidateResult(
        is_valid=True,
        table_name=table_name,
        column_name=column_name,
        table_size_bytes=table_size_bytes,
    )


def _rejected(
    table_name: str,
    column_name: str,
    reason: str,
    *,
    table_size_bytes: int | None = None,
    existing_index_name: str | None = None,
) -> CandidateResult:
    return CandidateResult(
        is_valid=False,
        table_name=table_name,
        column_name=column_name,
        rejection_reason=reason,
        table_size_bytes=table_size_bytes,
        existing_index_name=existing_index_name,
    )
