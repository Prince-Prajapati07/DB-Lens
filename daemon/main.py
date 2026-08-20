import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
import schedule
from dotenv import load_dotenv
from psycopg.rows import dict_row


PG_STAT_STATEMENTS_SCAN = """
SELECT
  queryid::text as query_hash,
  query,
  calls,
  total_exec_time,
  rows,
  shared_blks_hit,
  shared_blks_read
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 50;
"""


RESERVED_WORDS = {
    "and",
    "or",
    "not",
    "null",
    "true",
    "false",
    "select",
    "from",
    "where",
    "join",
    "on",
    "in",
    "is",
    "like",
}

SKIPPED_RELATIONS = {
    "information_schema",
    "pg_available_extensions",
    "pg_catalog",
    "pg_extension",
    "pg_namespace",
    "pg_stat_statements",
    "pg_stat_statements_info",
}


@dataclass(frozen=True)
class DaemonConfig:
    target_database_url: str
    target_database_name: str
    metadata_database_url: str
    poll_interval_seconds: int
    prefer_ipv4: bool


@dataclass(frozen=True)
class CandidateIndex:
    table_parts: list[str]
    columns: list[str]
    proposed_sql: str


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def load_config() -> DaemonConfig:
    load_dotenv()

    missing = [
        name
        for name in ("TARGET_DATABASE_URL", "METADATA_DATABASE_URL")
        if not os.getenv(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    interval_raw = os.getenv("POLL_INTERVAL_SECONDS", "60")
    try:
        interval = int(interval_raw)
    except ValueError as exc:
        raise RuntimeError("POLL_INTERVAL_SECONDS must be an integer") from exc

    if interval <= 0:
        raise RuntimeError("POLL_INTERVAL_SECONDS must be greater than zero")

    return DaemonConfig(
        target_database_url=os.environ["TARGET_DATABASE_URL"],
        target_database_name=os.getenv("TARGET_DATABASE_NAME", "default-target"),
        metadata_database_url=os.environ["METADATA_DATABASE_URL"],
        poll_interval_seconds=interval,
        prefer_ipv4=os.getenv("TARGET_DATABASE_PREFER_IPV4", "false").lower()
        in {"1", "true", "yes"},
    )


def with_ipv4_hostaddr(database_url: str) -> str:
    parsed = urlsplit(database_url)
    if not parsed.hostname:
        return database_url

    try:
        addr_info = socket.getaddrinfo(parsed.hostname, parsed.port, socket.AF_INET)
    except socket.gaierror:
        logging.warning("No IPv4 address found for target host %s", parsed.hostname)
        return database_url

    if not addr_info:
        logging.warning("No IPv4 address found for target host %s", parsed.hostname)
        return database_url

    ipv4_address = addr_info[0][4][0]
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["hostaddr"] = ipv4_address
    logging.info("Resolved target host %s to IPv4 %s", parsed.hostname, ipv4_address)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def target_connection_url(config: DaemonConfig) -> str:
    if not config.prefer_ipv4:
        return config.target_database_url
    return with_ipv4_hostaddr(config.target_database_url)


def normalize_sql(sql: str) -> str:
    without_line_comments = re.sub(r"--.*?(?=\n|$)", " ", sql)
    without_block_comments = re.sub(r"/\*.*?\*/", " ", without_line_comments, flags=re.S)
    return re.sub(r"\s+", " ", without_block_comments).strip().rstrip(";")


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sanitize_identifier(raw: str) -> str | None:
    cleaned = raw.replace('"', "").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cleaned):
        return None
    if cleaned.lower() in RESERVED_WORDS:
        return None
    return cleaned


def split_table_name(raw: str) -> list[str] | None:
    parts = []
    for part in raw.split("."):
        cleaned = sanitize_identifier(part)
        if not cleaned:
            return None
        parts.append(cleaned)

    return parts if 0 < len(parts) <= 2 else None


def format_table_name(parts: list[str]) -> str:
    return ".".join(quote_identifier(part) for part in parts)


def extract_target_table(query: str) -> list[str] | None:
    normalized = normalize_sql(query)
    match = re.search(r'\bfrom\s+("?\w+"?(?:\."?\w+"?)?)', normalized, re.I)
    if not match:
        return None

    after_table = normalized[match.end() :].lstrip()
    if after_table.startswith("("):
        return None

    return split_table_name(match.group(1))


def extract_predicate_columns(query: str) -> list[str]:
    normalized = normalize_sql(query)
    columns: list[str] = []
    seen = set()
    predicate_pattern = re.compile(
        r'(?:^|[\s(])(?:(?:"?[A-Za-z_][A-Za-z0-9_]*"?)[.])?"?'
        r'([A-Za-z_][A-Za-z0-9_]*)"?\s*(?:=|>=|<=|>|<|\bin\b|\blike\b|\bis\b)',
        re.I,
    )
    section_pattern = re.compile(
        r"\b(?:where|on)\b(.+?)(?:\bjoin\b|\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\blimit\b|$)",
        re.I,
    )

    for section_match in section_pattern.finditer(normalized):
        for column_match in predicate_pattern.finditer(section_match.group(1)):
            column = sanitize_identifier(column_match.group(1))
            if column and column not in seen:
                seen.add(column)
                columns.append(column)
            if len(columns) >= 3:
                return columns

    return columns


def build_candidate_index(query: str) -> CandidateIndex | None:
    table_parts = extract_target_table(query)
    columns = extract_predicate_columns(query)
    if not table_parts or not columns:
        return None
    if any(part.lower() in SKIPPED_RELATIONS for part in table_parts):
        return None

    index_name = quote_identifier(
        f"idx_dblens_{table_parts[-1]}_{'_'.join(columns)}"[:62]
    )
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    return CandidateIndex(
        table_parts=table_parts,
        columns=columns,
        proposed_sql=f"CREATE INDEX {index_name} ON {format_table_name(table_parts)} ({column_sql});",
    )


def validate_candidate_index(cur: psycopg.Cursor, candidate: CandidateIndex) -> bool:
    relation_name = ".".join(candidate.table_parts)
    try:
        cur.execute(
            """
            SELECT c.oid
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = to_regclass(%s)
              AND c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            """,
            (relation_name,),
        )
        relation = cur.fetchone()
        if not relation:
            cur.connection.rollback()
            return False

        relation_oid = relation["oid"]
        cur.execute(
            """
            SELECT attname
            FROM pg_attribute
            WHERE attrelid = %s
              AND attnum > 0
              AND NOT attisdropped
            """,
            (relation_oid,),
        )
        existing_columns = {row["attname"] for row in cur.fetchall()}
        cur.connection.commit()
        return all(column in existing_columns for column in candidate.columns)
    except psycopg.Error as exc:
        logging.info("Candidate validation skipped: %s", exc)
        cur.connection.rollback()
        return False


def explainable_query(query: str) -> str | None:
    normalized = normalize_sql(query)
    if not re.match(r"^(select|with)\b", normalized, re.I):
        return None
    return re.sub(r"\$\d+", "NULL", normalized)


def extract_total_cost(explain_rows: list[dict[str, Any]]) -> float | None:
    if not explain_rows:
        return None

    plan_payload = explain_rows[0].get("QUERY PLAN")
    if isinstance(plan_payload, list) and plan_payload:
        cost = plan_payload[0].get("Plan", {}).get("Total Cost")
        if isinstance(cost, (int, float)):
            return float(cost)

    return None


def plan_has_seq_scan(plan: dict[str, Any]) -> bool:
    if plan.get("Node Type") == "Seq Scan":
        return True
    return any(plan_has_seq_scan(child) for child in plan.get("Plans", []))


def explain_plan_has_seq_scan(explain_rows: list[dict[str, Any]]) -> bool:
    if not explain_rows:
        return False

    plan_payload = explain_rows[0].get("QUERY PLAN")
    if not isinstance(plan_payload, list) or not plan_payload:
        return False

    plan = plan_payload[0].get("Plan")
    return isinstance(plan, dict) and plan_has_seq_scan(plan)


def explain_query(cur: psycopg.Cursor, query: str) -> list[dict[str, Any]] | None:
    try:
        cur.execute(f"EXPLAIN (FORMAT JSON) {query}")
        return [dict(row) for row in cur.fetchall()]
    except psycopg.Error as exc:
        logging.info("Skipping EXPLAIN for unsupported statement: %s", exc)
        cur.connection.rollback()
        return None


def ensure_hypopg(cur: psycopg.Cursor) -> bool:
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg")
        cur.connection.commit()
        return True
    except psycopg.Error as exc:
        logging.warning("HypoPG is unavailable on target database: %s", exc)
        cur.connection.rollback()
        return False


def reset_hypopg(cur: psycopg.Cursor) -> None:
    try:
        cur.execute("SELECT hypopg_reset()")
        cur.connection.commit()
    except psycopg.Error as exc:
        logging.info("HypoPG reset skipped: %s", exc)
        cur.connection.rollback()


def simulate_candidate_index(
    cur: psycopg.Cursor,
    query: str,
    proposed_index_sql: str,
) -> float | None:
    explain_before = explain_query(cur, query)
    if not explain_before or not explain_plan_has_seq_scan(explain_before):
        return None

    before_cost = extract_total_cost(explain_before)
    if not before_cost or before_cost <= 0:
        return None

    try:
        cur.execute("SELECT * FROM hypopg_reset()")
        cur.execute("SELECT * FROM hypopg_create_index(%s)", (proposed_index_sql,))
        explain_after = explain_query(cur, query)
        cur.execute("SELECT * FROM hypopg_reset()")
        cur.connection.commit()
    except psycopg.Error as exc:
        logging.info("HypoPG simulation skipped: %s", exc)
        cur.connection.rollback()
        reset_hypopg(cur)
        return None

    after_cost = extract_total_cost(explain_after or [])
    if after_cost is None:
        return None

    reduction = max(0.0, ((before_cost - after_cost) / before_cost) * 100)
    return round(reduction, 2)


def add_target_side_remediation(
    cur: psycopg.Cursor,
    metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not ensure_hypopg(cur):
        return metrics

    enriched = []
    simulations = 0
    for row in metrics:
        enriched_row = dict(row)
        query = explainable_query(str(row["query"]))
        candidate = build_candidate_index(str(row["query"]))
        if query and candidate and validate_candidate_index(cur, candidate):
            reduction = simulate_candidate_index(cur, query, candidate.proposed_sql)
            if reduction and reduction > 0:
                enriched_row["proposed_index_sql"] = candidate.proposed_sql
                enriched_row["cost_reduction_pct"] = reduction
                simulations += 1

        enriched.append(enriched_row)

    logging.info("Calculated %s target-side HypoPG remediation estimates", simulations)
    return enriched


def fetch_target_metrics(config: DaemonConfig) -> list[dict[str, Any]] | None:
    try:
        with psycopg.connect(target_connection_url(config), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(PG_STAT_STATEMENTS_SCAN)
                rows = cur.fetchall()
                logging.info("Fetched %s pg_stat_statements rows from target", len(rows))
                return add_target_side_remediation(cur, [dict(row) for row in rows])
    except psycopg.Error as exc:
        logging.warning("Target database read failed: %s", exc)
        return None
    except Exception as exc:
        logging.warning("Unexpected target database error: %s", exc)
        return None


def ensure_target_database(conn: psycopg.Connection, config: DaemonConfig) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT "id"
            FROM "TargetDatabase"
            WHERE "name" = %s AND "connectionUri" = %s
            ORDER BY "createdAt" ASC
            LIMIT 1
            """,
            (config.target_database_name, config.target_database_url),
        )
        existing = cur.fetchone()
        if existing:
            return existing[0]

        database_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO "TargetDatabase" ("id", "name", "connectionUri")
            VALUES (%s, %s, %s)
            """,
            (database_id, config.target_database_name, config.target_database_url),
        )
        logging.info("Registered target database %s", config.target_database_name)
        return database_id


def insert_metrics_batch(
    conn: psycopg.Connection,
    database_id: str,
    metrics: list[dict[str, Any]],
) -> int:
    rows = [
        (
            str(uuid.uuid4()),
            database_id,
            row["query_hash"],
            row["query"],
            int(row["calls"]),
            float(row["total_exec_time"]),
            int(row["rows"]),
            int(row["shared_blks_hit"]),
            int(row["shared_blks_read"]),
            row.get("proposed_index_sql"),
            row.get("cost_reduction_pct"),
        )
        for row in metrics
        if row.get("query_hash") is not None
    ]

    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO "MetricsHistory" (
                "id",
                "databaseId",
                "queryHash",
                "normalizedQuery",
                "callCount",
                "totalExecTimeMs",
                "rowsReturned",
                "sharedBlksHit",
                "sharedBlksRead",
                "proposedIndexSql",
                "costReductionPct"
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    return len(rows)


def persist_metrics(config: DaemonConfig, metrics: list[dict[str, Any]]) -> bool:
    try:
        with psycopg.connect(config.metadata_database_url) as conn:
            database_id = ensure_target_database(conn, config)
            inserted = insert_metrics_batch(conn, database_id, metrics)
            conn.commit()
            logging.info("Inserted %s metrics history rows", inserted)
            return True
    except psycopg.Error as exc:
        logging.warning("Metadata database write failed: %s", exc)
        return False
    except Exception as exc:
        logging.warning("Unexpected metadata database error: %s", exc)
        return False


def run_tick(config: DaemonConfig) -> None:
    logging.info("Starting telemetry tick")
    metrics = fetch_target_metrics(config)
    if metrics is None:
        logging.info("Telemetry tick skipped; target metrics unavailable")
        return
    if not metrics:
        logging.info("Telemetry tick completed; no target metrics available yet")
        return

    if not persist_metrics(config, metrics):
        logging.info("Telemetry tick skipped; metadata persistence unavailable")
        return

    logging.info("Telemetry tick completed")


def main() -> None:
    configure_logging()
    config = load_config()

    logging.info(
        "DB-Lens telemetry daemon started; polling every %s seconds",
        config.poll_interval_seconds,
    )

    run_tick(config)
    schedule.every(config.poll_interval_seconds).seconds.do(run_tick, config)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
