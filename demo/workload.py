"""Generate a measurable, read-only workload for the PlanPatch demo."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys
from time import perf_counter

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from planpatch.config import ConfigError, load_config


CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 10_000
WORKLOAD_QUERY = "SELECT * FROM public.orders WHERE customer_id = %s"


def main(argv: Sequence[str] | None = None) -> int:
    """Execute repeated parameterized SELECTs against the demo orders table."""
    parser = argparse.ArgumentParser(
        description="Generate read-only PlanPatch demo workload.",
    )
    parser.add_argument(
        "--iterations",
        type=_positive_int,
        default=200,
        help="Number of SELECT statements to execute (default: 200).",
    )
    parser.add_argument(
        "--customer-span",
        type=_positive_int,
        default=250,
        help="Cycle customer IDs from 1 through this value (default: 250).",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print("PlanPatch workload configuration failed:", file=sys.stderr)
        for issue in exc.issues:
            print(f"- {issue}", file=sys.stderr)
        return 2

    started_at = perf_counter()
    rows_read = 0
    try:
        with psycopg.connect(
            config.target.db_url,
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            application_name="planpatch-demo-workload",
        ) as connection:
            with connection.cursor() as cursor:
                for iteration in range(args.iterations):
                    customer_id = (iteration % args.customer_span) + 1
                    cursor.execute(WORKLOAD_QUERY, (customer_id,))
                    rows_read += len(cursor.fetchall())
    except psycopg.Error as exc:
        primary = getattr(exc.diag, "message_primary", None)
        print(
            f"PlanPatch workload failed: {primary or 'PostgreSQL operation failed'}",
            file=sys.stderr,
        )
        return 2

    elapsed_seconds = perf_counter() - started_at
    print(
        "PlanPatch workload complete: "
        f"queries={args.iterations}, rows_read={rows_read}, "
        f"elapsed_seconds={elapsed_seconds:.2f}, "
        f"target={config.target.host}:{config.target.port}/{config.target.database}"
    )
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
