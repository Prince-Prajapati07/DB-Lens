"""Configuration loading and validation for PlanPatch."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

from dotenv import load_dotenv

from planpatch.models import AppConfig, TargetConfig

DEFAULT_POSTGRES_PORT = 5432
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(ValueError):
    """Raised when PlanPatch cannot build a valid application config."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("\n".join(issues))


def load_config(env_path: Path | None = None) -> AppConfig:
    """Load and validate PlanPatch configuration from environment variables."""
    load_dotenv(dotenv_path=env_path)

    db_url = _get_first_env("TARGET_DATABASE_URL", "DATABASE_URL")
    github_token = _get_first_env("GITHUB_TOKEN", "GH_TOKEN")
    dry_run = _parse_bool(os.getenv("PLANPATCH_DRY_RUN"), default=True)
    log_level = os.getenv("PLANPATCH_LOG_LEVEL", "INFO").upper()

    issues: list[str] = []
    if db_url is None:
        issues.append("Missing TARGET_DATABASE_URL or DATABASE_URL.")

    if log_level not in VALID_LOG_LEVELS:
        issues.append(
            "Invalid PLANPATCH_LOG_LEVEL. Expected one of: "
            f"{', '.join(sorted(VALID_LOG_LEVELS))}."
        )

    target: TargetConfig | None = None
    if db_url is not None:
        try:
            target = _build_target_config(db_url)
        except ConfigError as exc:
            issues.extend(exc.issues)

    if issues:
        raise ConfigError(issues)

    if target is None:
        raise ConfigError(["Unable to build target database configuration."])

    return AppConfig(
        target=target,
        dry_run=dry_run,
        github_token=github_token,
        log_level=log_level,
    )


def mask_database_url(db_url: str) -> str:
    """Return a version of a PostgreSQL URL with its password hidden."""
    parsed = urlsplit(db_url)
    username = parsed.username or ""
    password = parsed.password
    hostname = parsed.hostname or ""
    port = parsed.port

    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    auth = username
    if password is not None:
        auth = f"{username}:********"

    host_port = hostname
    if port is not None:
        host_port = f"{host_port}:{port}"

    netloc = f"{auth}@{host_port}" if auth else host_port
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _build_target_config(db_url: str) -> TargetConfig:
    parsed = urlsplit(db_url)
    issues: list[str] = []

    if parsed.scheme not in {"postgres", "postgresql"}:
        issues.append("TARGET_DATABASE_URL must use postgres:// or postgresql://.")

    if not parsed.hostname:
        issues.append("TARGET_DATABASE_URL must include a database host.")

    if not parsed.username:
        issues.append("TARGET_DATABASE_URL must include a database user.")

    if parsed.password is None:
        issues.append("TARGET_DATABASE_URL must include a database password.")

    database = parsed.path.lstrip("/")
    if not database:
        issues.append("TARGET_DATABASE_URL must include a database name.")

    try:
        port = parsed.port or DEFAULT_POSTGRES_PORT
    except ValueError as exc:
        issues.append(f"TARGET_DATABASE_URL has an invalid port: {exc}.")
        port = DEFAULT_POSTGRES_PORT

    if issues:
        raise ConfigError(issues)

    return TargetConfig(
        db_url=db_url,
        port=port,
        user=unquote(parsed.username or ""),
        host=parsed.hostname or "",
        database=unquote(database),
        masked_db_url=mask_database_url(db_url),
    )


def _get_first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True

    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    return default
