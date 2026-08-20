"""Local snapshot state for PlanPatch."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from planpatch.models import TargetConfig

STATE_DIR = Path(".planpatch")
STATE_FILE = STATE_DIR / "state.json"


@dataclass(frozen=True, slots=True)
class QueryCounters:
    """Cumulative counters for a normalized PostgreSQL statement."""

    calls: int
    total_exec_time_ms: float
    query: str = ""

    @classmethod
    def from_json(cls, payload: dict[str, Any] | list[Any] | tuple[Any, ...]) -> "QueryCounters":
        if isinstance(payload, dict):
            return cls(
                calls=int(payload["calls"]),
                total_exec_time_ms=float(payload["total_exec_time_ms"]),
                query=str(payload.get("query", "")),
            )

        if len(payload) < 2:
            raise ValueError("Query counter tuples must contain calls and total_exec_time_ms.")

        return cls(calls=int(payload[0]), total_exec_time_ms=float(payload[1]))

    def to_json(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "total_exec_time_ms": self.total_exec_time_ms,
            "query": self.query,
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A local point-in-time copy of cumulative pg_stat_statements counters."""

    target_identity: str
    captured_at: datetime
    queries: dict[str, QueryCounters]

    @classmethod
    def now(cls, *, target_identity: str, queries: dict[str, QueryCounters]) -> "Snapshot":
        return cls(
            target_identity=target_identity,
            captured_at=datetime.now(UTC),
            queries=queries,
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Snapshot":
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, dict):
            raise ValueError("Snapshot JSON must contain a queries object.")

        return cls(
            target_identity=str(payload["target_identity"]),
            captured_at=datetime.fromisoformat(str(payload["captured_at"])),
            queries={
                str(fingerprint): QueryCounters.from_json(counters)
                for fingerprint, counters in raw_queries.items()
            },
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target_identity": self.target_identity,
            "captured_at": self.captured_at.isoformat(),
            "queries": {
                fingerprint: counters.to_json()
                for fingerprint, counters in sorted(self.queries.items())
            },
        }


def target_identity_from_config(target: TargetConfig) -> str:
    """Build a stable, secret-free identity for the monitored target database."""
    return f"{target.host}:{target.port}/{target.database}?user={target.user}"


def load_snapshot(path: Path = STATE_FILE) -> Snapshot | None:
    """Load the last saved snapshot, returning None if no state exists."""
    if not path.exists():
        return None

    with path.open("r", encoding="utf-8") as state_file:
        payload = json.load(state_file)

    if not isinstance(payload, dict):
        raise ValueError(f"Snapshot state at {path} must be a JSON object.")

    return Snapshot.from_json(payload)


def save_snapshot(snapshot: Snapshot, path: Path = STATE_FILE) -> None:
    """Atomically save snapshot state to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.to_json()

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, indent=2, sort_keys=True)
        temp_file.write("\n")
        temp_file.flush()
        os.fsync(temp_file.fileno())

    os.replace(temp_path, path)
