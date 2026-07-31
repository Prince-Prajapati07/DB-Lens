import logging
import os
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


@dataclass(frozen=True)
class DaemonConfig:
    target_database_url: str
    target_database_name: str
    metadata_database_url: str
    poll_interval_seconds: int
    prefer_ipv4: bool


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


def fetch_target_metrics(config: DaemonConfig) -> list[dict[str, Any]] | None:
    try:
        with psycopg.connect(target_connection_url(config), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(PG_STAT_STATEMENTS_SCAN)
                rows = cur.fetchall()
                logging.info("Fetched %s pg_stat_statements rows from target", len(rows))
                return [dict(row) for row in rows]
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
                "sharedBlksRead"
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
