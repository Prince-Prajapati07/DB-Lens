"""Typed application models for the PlanPatch CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def mask_secret(value: str | None, *, visible_prefix: int = 4) -> str:
    """Return a display-safe version of a secret value."""
    if not value:
        return ""

    if len(value) <= visible_prefix:
        return "*" * len(value)

    return f"{value[:visible_prefix]}{'*' * 8}"


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """Configuration for the PostgreSQL database being analyzed."""

    db_url: str = field(repr=False)
    port: int
    user: str
    host: str
    database: str
    masked_db_url: str

    def to_display_dict(self) -> dict[str, Any]:
        """Return a secret-safe representation for console output."""
        return {
            "db_url": self.masked_db_url,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
        }

    def __str__(self) -> str:
        return (
            "TargetConfig("
            f"db_url='{self.masked_db_url}', "
            f"host='{self.host}', "
            f"port={self.port}, "
            f"user='{self.user}', "
            f"database='{self.database}'"
            ")"
        )

    def __repr__(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Runtime configuration for PlanPatch."""

    target: TargetConfig
    dry_run: bool
    github_token: str | None = field(default=None, repr=False)
    log_level: str = "INFO"

    @property
    def masked_github_token(self) -> str:
        return mask_secret(self.github_token)

    def to_display_dict(self) -> dict[str, Any]:
        """Return a secret-safe representation for console output."""
        return {
            "dry_run": self.dry_run,
            "github_token": self.masked_github_token or "not configured",
            "log_level": self.log_level,
            "target": self.target.to_display_dict(),
        }

    def __str__(self) -> str:
        return (
            "AppConfig("
            f"dry_run={self.dry_run}, "
            f"github_token='{self.masked_github_token or 'not configured'}', "
            f"log_level='{self.log_level}', "
            f"target={self.target}"
            ")"
        )

    def __repr__(self) -> str:
        return str(self)
