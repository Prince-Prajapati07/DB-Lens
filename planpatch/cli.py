"""Command line interface for DB-Lens PlanPatch."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from planpatch.candidates import validate_candidate
from planpatch.collector import CollectionError, RawSnapshot, collect_pg_stat_statements
from planpatch.config import ConfigError, load_config
from planpatch.delta import DeltaReport, QueryDelta, calculate_delta
from planpatch.eligibility import check_eligibility
from planpatch.github import (
    GitHubAPIError,
    PRPayload,
    build_pr_payload,
    publish_draft_pr,
)
from planpatch.migrations import MigrationArtifact, MigrationError, generate_migration
from planpatch.models import AppConfig
from planpatch.planner import PlanEvidence, PlannerError, evaluate_candidate
from planpatch.state import STATE_FILE, load_snapshot, save_snapshot

CommandHandler = Callable[[argparse.Namespace, Console], int]
MIGRATION_OUTPUT_DIR = Path("db/migrations")


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """Successful end-to-end recommendation for one interval query."""

    query_delta: QueryDelta
    table_name: str
    column_name: str
    evidence: PlanEvidence
    artifact: MigrationArtifact
    pr_payload: PRPayload


@dataclass(frozen=True, slots=True)
class AnalysisRejection:
    """One explainable query rejection during candidate selection."""

    fingerprint: str
    reason: str

console = Console()


def run(argv: Sequence[str] | None = None) -> int:
    """Run the PlanPatch CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handler: CommandHandler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    return handler(args, console)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level command parser."""
    parser = argparse.ArgumentParser(
        prog="planpatch",
        description="Local-first PostgreSQL performance CLI for DB-Lens.",
    )
    subparsers = parser.add_subparsers(dest="command")

    demo_parser = subparsers.add_parser("demo", help="Prepare or baseline demo data.")
    demo_parser.add_argument("--seed", action="store_true", help="Acknowledge demo seed mode.")
    demo_parser.add_argument(
        "--baseline",
        action="store_true",
        help="Acknowledge baseline capture mode.",
    )
    demo_parser.set_defaults(handler=_handle_demo)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze workload evidence.")
    analyze_parser.add_argument("--demo", action="store_true", help="Use demo workflow settings.")
    analyze_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate local artifacts and PR content without network calls.",
    )
    analyze_parser.add_argument(
        "--publish",
        action="store_true",
        help="Create or reuse a draft PR after local artifact generation.",
    )
    analyze_parser.add_argument(
        "--repo",
        metavar="OWNER/REPOSITORY",
        help="GitHub repository; falls back to GITHUB_REPOSITORY.",
    )
    analyze_parser.set_defaults(handler=_handle_analyze)

    watch_parser = subparsers.add_parser("watch", help="Watch workload at an interval.")
    watch_parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Polling interval in seconds.",
    )
    watch_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview actions without publishing or writing changes.",
    )
    watch_parser.set_defaults(handler=_handle_watch)

    return parser


def _handle_demo(args: argparse.Namespace, output: Console) -> int:
    config = _load_config_or_render_error(output)
    if config is None:
        return 2

    raw_snapshot = _collect_or_render_error(config, output)
    if raw_snapshot is None:
        return 2

    snapshot = raw_snapshot.to_snapshot()

    if args.baseline:
        save_snapshot(snapshot)
        _render_baseline_panel(output, snapshot_count=len(snapshot.queries), config=config)
        return 0

    baseline = load_snapshot()
    if baseline is None:
        output.print(
            Panel(
                "No baseline snapshot found. Run `python main.py demo --baseline` first.",
                title="PlanPatch baseline required",
                border_style="yellow",
            )
        )
        return 2

    try:
        report = calculate_delta(baseline, snapshot)
    except ValueError as exc:
        output.print(
            Panel(
                str(exc),
                title="PlanPatch delta failed",
                border_style="red",
            )
        )
        return 2

    _render_delta_panel(output, report, seed=args.seed, config=config)
    return 0


def _handle_analyze(args: argparse.Namespace, output: Console) -> int:
    config = _load_config_or_render_error(output)
    if config is None:
        return 2

    raw_snapshot = _collect_or_render_error(config, output)
    if raw_snapshot is None:
        return 2
    current = raw_snapshot.to_snapshot()

    try:
        baseline = load_snapshot()
    except (OSError, ValueError) as exc:
        _render_failure_panel(
            output,
            title="PlanPatch baseline could not be loaded",
            message=f"invalid_snapshot_state: {type(exc).__name__}",
        )
        return 2

    if baseline is None:
        _render_failure_panel(
            output,
            title="PlanPatch baseline required",
            message="Run `python main.py demo --baseline` before analyze.",
            warning=True,
        )
        return 2

    try:
        report = calculate_delta(baseline, current)
        save_snapshot(current)
    except (OSError, ValueError) as exc:
        _render_failure_panel(
            output,
            title="PlanPatch interval calculation failed",
            message=str(exc),
        )
        return 2

    if not report.deltas:
        _render_no_recommendation(
            output,
            report=report,
            rejections=(),
            summary_reason="no_interval_activity",
        )
        return 0

    outcome, rejections = _find_viable_candidate(
        report,
        config=config,
    )
    if outcome is None:
        _render_no_recommendation(
            output,
            report=report,
            rejections=rejections,
            summary_reason="no_eligible_or_viable_query",
        )
        return 0

    dry_run = args.dry_run or not args.publish
    repo = args.repo or os.getenv("GITHUB_REPOSITORY")
    pr_status = "dry-run: no network request"
    pr_url: str | None = None
    publish_failed = False

    if args.publish and not args.dry_run:
        if config.github_token is None:
            pr_status = "dry-run: GitHub token not configured"
            dry_run = True
        elif not repo:
            pr_status = "dry-run: GitHub repository not configured"
            dry_run = True
        else:
            try:
                pr_url = publish_draft_pr(
                    outcome.pr_payload,
                    repo=repo,
                    token=config.github_token,
                )
                pr_status = "draft PR created or reused"
                dry_run = False
            except (GitHubAPIError, ValueError) as exc:
                pr_status = f"publish failed: {exc}"
                publish_failed = True
                dry_run = True

    _render_analysis_outcome(
        output,
        outcome=outcome,
        report=report,
        pr_status=pr_status,
        pr_url=pr_url,
        dry_run=dry_run,
        rejections=rejections,
    )
    return 2 if publish_failed else 0


def _find_viable_candidate(
    report: DeltaReport,
    *,
    config: AppConfig,
) -> tuple[AnalysisOutcome | None, tuple[AnalysisRejection, ...]]:
    rejections: list[AnalysisRejection] = []

    for query_delta in report.deltas:
        eligibility = check_eligibility(query_delta.query)
        if not eligibility.is_eligible:
            rejections.append(
                AnalysisRejection(
                    query_delta.fingerprint,
                    eligibility.rejection_reason or "unsupported_sql",
                )
            )
            continue

        table_name = eligibility.table_name
        column_name = eligibility.candidate_column
        if table_name is None or column_name is None:
            rejections.append(
                AnalysisRejection(query_delta.fingerprint, "invalid_eligibility_result")
            )
            continue

        candidate = validate_candidate(
            config.target.db_url,
            table_name,
            column_name,
        )
        if not candidate.is_valid:
            rejections.append(
                AnalysisRejection(
                    query_delta.fingerprint,
                    candidate.rejection_reason or "candidate_rejected",
                )
            )
            continue

        try:
            evidence = evaluate_candidate(
                config.target.db_url,
                query_delta.query,
                table_name,
                column_name,
            )
        except PlannerError as exc:
            rejections.append(AnalysisRejection(query_delta.fingerprint, exc.code))
            continue

        if not evidence.is_viable:
            reason = (
                "weak_estimate"
                if evidence.used_virtual_index
                else "hypothetical_index_unused"
            )
            rejections.append(AnalysisRejection(query_delta.fingerprint, reason))
            continue

        try:
            artifact = generate_migration(
                table_name,
                column_name,
                evidence,
                str(MIGRATION_OUTPUT_DIR),
            )
            pr_payload = build_pr_payload(
                table_name,
                column_name,
                evidence,
                artifact,
            )
        except (MigrationError, OSError, ValueError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, MigrationError)
                else "artifact_generation_failed"
            )
            rejections.append(AnalysisRejection(query_delta.fingerprint, reason))
            continue

        return (
            AnalysisOutcome(
                query_delta=query_delta,
                table_name=table_name,
                column_name=column_name,
                evidence=evidence,
                artifact=artifact,
                pr_payload=pr_payload,
            ),
            tuple(rejections),
        )

    return None, tuple(rejections)


def _handle_watch(args: argparse.Namespace, output: Console) -> int:
    config = _load_config_or_render_error(output)
    if config is None:
        return 2

    if args.interval < 1:
        output.print(
            Panel(
                "The --interval value must be at least 1 second.",
                title="Invalid watch interval",
                border_style="red",
            )
        )
        return 2

    effective_dry_run = config.dry_run or args.dry_run
    flags = {"interval": args.interval, "dry_run": effective_dry_run}
    _render_command_panel(output, "watch", flags, config)
    return 0


def _load_config_or_render_error(output: Console) -> AppConfig | None:
    try:
        return load_config()
    except ConfigError as exc:
        table = Table.grid(padding=(0, 1))
        table.add_column("issue", style="red")
        for issue in exc.issues:
            table.add_row(f"- {issue}")

        output.print(
            Panel(
                table,
                title="PlanPatch configuration failed validation",
                border_style="red",
            )
        )
        return None


def _collect_or_render_error(config: AppConfig, output: Console) -> RawSnapshot | None:
    try:
        return collect_pg_stat_statements(config.target)
    except CollectionError as exc:
        output.print(
            Panel(
                str(exc),
                title="PlanPatch collection failed",
                border_style="red",
            )
        )
        return None


def _render_baseline_panel(
    output: Console,
    *,
    snapshot_count: int,
    config: AppConfig,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column("key", style="cyan", no_wrap=True)
    table.add_column("value", style="white")

    table.add_row("command", "demo --baseline")
    table.add_row("status", "baseline snapshot saved")
    table.add_row("state_file", str(STATE_FILE))
    table.add_row("queries_captured", str(snapshot_count))
    table.add_row("target.db_url", config.target.masked_db_url)
    table.add_row("target.host", config.target.host)
    table.add_row("target.database", config.target.database)

    output.print(
        Panel(
            table,
            title="DB-Lens PlanPatch",
            subtitle="Day 2 baseline",
            border_style="green",
        )
    )


def _render_delta_panel(
    output: Console,
    report: DeltaReport,
    *,
    seed: bool,
    config: AppConfig,
) -> None:
    table = Table(
        title="Top interval deltas",
        show_lines=False,
        expand=True,
    )
    table.add_column("rank", justify="right", style="cyan", no_wrap=True)
    table.add_column("fingerprint", style="white", overflow="fold")
    table.add_column("calls", justify="right", style="green", no_wrap=True)
    table.add_column("total ms", justify="right", style="green", no_wrap=True)
    table.add_column("avg ms", justify="right", style="green", no_wrap=True)
    table.add_column("query", overflow="ellipsis")

    for index, item in enumerate(report.deltas[:10], start=1):
        table.add_row(
            str(index),
            item.fingerprint,
            str(item.delta_calls),
            f"{item.delta_total_exec_time_ms:.2f}",
            f"{item.avg_latency_ms:.2f}",
            _compact_query(item.query),
        )

    if not report.deltas:
        table.add_row("-", "no interval activity", "0", "0.00", "0.00", "")

    summary = Table.grid(padding=(0, 2))
    summary.add_column("key", style="cyan", no_wrap=True)
    summary.add_column("value", style="white")
    summary.add_row("command", "demo")
    summary.add_row("seed", str(seed))
    summary.add_row("status", "interval delta calculated")
    summary.add_row("target.db_url", config.target.masked_db_url)
    summary.add_row("baseline_at", report.baseline.captured_at.isoformat())
    summary.add_row("current_at", report.current.captured_at.isoformat())
    summary.add_row("queries_ranked", str(len(report.deltas)))
    summary.add_row("counter_resets", str(len(report.reset_fingerprints)))

    output.print(
        Panel(
            summary,
            title="DB-Lens PlanPatch",
            subtitle="Day 2 delta",
            border_style="green",
        )
    )
    output.print(table)


def _render_analysis_outcome(
    output: Console,
    *,
    outcome: AnalysisOutcome,
    report: DeltaReport,
    pr_status: str,
    pr_url: str | None,
    dry_run: bool,
    rejections: tuple[AnalysisRejection, ...],
) -> None:
    delta = outcome.query_delta
    evidence = outcome.evidence
    summary = Table.grid(padding=(0, 2))
    summary.add_column("key", style="cyan", no_wrap=True)
    summary.add_column("value", style="white", overflow="fold")
    summary.add_row("status", "viable index recommendation")
    summary.add_row("fingerprint", delta.fingerprint)
    summary.add_row("normalized_query", _compact_query(delta.query))
    summary.add_row("target", f"{outcome.table_name} ({outcome.column_name})")
    summary.add_row("interval_calls", str(delta.delta_calls))
    summary.add_row("interval_total_ms", f"{delta.delta_total_exec_time_ms:.2f}")
    summary.add_row("interval_avg_ms", f"{delta.avg_latency_ms:.2f}")
    summary.add_row(
        "planner_cost",
        (
            f"{evidence.baseline_cost:.2f} -> {evidence.optimized_cost:.2f} "
            f"({evidence.cost_reduction_pct:.2f}% reduction)"
        ),
    )
    summary.add_row("virtual_index_used", str(evidence.used_virtual_index))
    summary.add_row("up_migration", str(outcome.artifact.up_sql_path))
    summary.add_row("down_migration", str(outcome.artifact.down_sql_path))
    summary.add_row("pr_status", pr_status)
    if pr_url is not None:
        summary.add_row("pr_url", pr_url)
    summary.add_row("baseline_advanced_to", report.current.captured_at.isoformat())

    output.print(
        Panel(
            summary,
            title="DB-Lens PlanPatch analysis",
            subtitle="planner estimates require human review",
            border_style="green",
        )
    )
    if rejections:
        _render_rejections(output, rejections)
    if dry_run:
        output.print(
            Panel(
                Markdown(outcome.pr_payload.markdown_body),
                title="Draft PR payload (dry run)",
                border_style="cyan",
            )
        )


def _render_no_recommendation(
    output: Console,
    *,
    report: DeltaReport,
    rejections: tuple[AnalysisRejection, ...],
    summary_reason: str,
) -> None:
    summary = Table.grid(padding=(0, 2))
    summary.add_column("key", style="cyan", no_wrap=True)
    summary.add_column("value", style="white")
    summary.add_row("status", "no recommendation generated")
    summary.add_row("reason", summary_reason)
    summary.add_row("queries_ranked", str(len(report.deltas)))
    summary.add_row("queries_rejected", str(len(rejections)))
    summary.add_row("counter_resets", str(len(report.reset_fingerprints)))
    summary.add_row("baseline_advanced_to", report.current.captured_at.isoformat())
    output.print(
        Panel(
            summary,
            title="DB-Lens PlanPatch analysis",
            subtitle="safe rejection is a valid outcome",
            border_style="yellow",
        )
    )
    if rejections:
        _render_rejections(output, rejections)


def _render_rejections(
    output: Console,
    rejections: tuple[AnalysisRejection, ...],
) -> None:
    table = Table(title="Candidate decisions", expand=True)
    table.add_column("rank", justify="right", style="cyan", no_wrap=True)
    table.add_column("fingerprint", style="white", overflow="fold")
    table.add_column("decision", style="yellow", overflow="fold")
    for rank, rejection in enumerate(rejections[:10], start=1):
        table.add_row(str(rank), rejection.fingerprint, rejection.reason)
    if len(rejections) > 10:
        table.caption = f"Showing 10 of {len(rejections)} rejected queries."
    output.print(table)


def _render_failure_panel(
    output: Console,
    *,
    title: str,
    message: str,
    warning: bool = False,
) -> None:
    output.print(
        Panel(
            message,
            title=title,
            border_style="yellow" if warning else "red",
        )
    )


def _render_command_panel(
    output: Console,
    command_name: str,
    flags: dict[str, Any],
    config: AppConfig,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column("key", style="cyan", no_wrap=True)
    table.add_column("value", style="white")

    table.add_row("command", command_name)
    table.add_row("status", "validated configuration loaded")

    for key, value in flags.items():
        table.add_row(key, str(value))

    for key, value in config.to_display_dict().items():
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                table.add_row(f"target.{nested_key}", str(nested_value))
        else:
            table.add_row(f"config.{key}", str(value))

    output.print(
        Panel(
            table,
            title="DB-Lens PlanPatch",
            subtitle="Day 1 CLI skeleton",
            border_style="green",
        )
    )


def _compact_query(query: str, *, max_length: int = 120) -> str:
    compacted = " ".join(query.split())
    if len(compacted) <= max_length:
        return compacted

    return f"{compacted[: max_length - 1]}..."
