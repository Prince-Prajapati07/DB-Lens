"""Interval delta calculations for PlanPatch snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from planpatch.state import QueryCounters, Snapshot


@dataclass(frozen=True, slots=True)
class QueryDelta:
    """Interval-level execution statistics for one query fingerprint."""

    fingerprint: str
    query: str
    delta_calls: int
    delta_total_exec_time_ms: float
    avg_latency_ms: float
    counter_reset: bool = False


@dataclass(frozen=True, slots=True)
class DeltaReport:
    """Ranked delta report between two snapshots."""

    baseline: Snapshot
    current: Snapshot
    deltas: tuple[QueryDelta, ...]
    reset_fingerprints: tuple[str, ...]


def calculate_delta(baseline: Snapshot, current: Snapshot) -> DeltaReport:
    """Calculate ranked interval deltas between two cumulative snapshots."""
    if baseline.target_identity != current.target_identity:
        raise ValueError(
            "Cannot calculate deltas for different targets: "
            f"{baseline.target_identity!r} != {current.target_identity!r}."
        )

    deltas: list[QueryDelta] = []
    reset_fingerprints: list[str] = []

    for fingerprint, current_counters in current.queries.items():
        baseline_counters = baseline.queries.get(fingerprint)
        query_delta = _calculate_query_delta(
            fingerprint=fingerprint,
            baseline=baseline_counters,
            current=current_counters,
        )

        if query_delta.counter_reset:
            reset_fingerprints.append(fingerprint)

        if query_delta.delta_calls > 0 and query_delta.delta_total_exec_time_ms > 0:
            deltas.append(query_delta)

    deltas.sort(key=lambda item: item.delta_total_exec_time_ms, reverse=True)

    return DeltaReport(
        baseline=baseline,
        current=current,
        deltas=tuple(deltas),
        reset_fingerprints=tuple(sorted(reset_fingerprints)),
    )


def _calculate_query_delta(
    *,
    fingerprint: str,
    baseline: QueryCounters | None,
    current: QueryCounters,
) -> QueryDelta:
    if baseline is None:
        delta_calls = current.calls
        delta_total_exec_time_ms = current.total_exec_time_ms
        counter_reset = False
    elif current.calls < baseline.calls:
        delta_calls = current.calls
        delta_total_exec_time_ms = current.total_exec_time_ms
        counter_reset = True
    else:
        delta_calls = current.calls - baseline.calls
        delta_total_exec_time_ms = current.total_exec_time_ms - baseline.total_exec_time_ms
        counter_reset = current.total_exec_time_ms < baseline.total_exec_time_ms

        if counter_reset:
            delta_calls = current.calls
            delta_total_exec_time_ms = current.total_exec_time_ms

    avg_latency_ms = (
        delta_total_exec_time_ms / delta_calls
        if delta_calls > 0
        else 0.0
    )

    return QueryDelta(
        fingerprint=fingerprint,
        query=current.query,
        delta_calls=delta_calls,
        delta_total_exec_time_ms=delta_total_exec_time_ms,
        avg_latency_ms=avg_latency_ms,
        counter_reset=counter_reset,
    )
