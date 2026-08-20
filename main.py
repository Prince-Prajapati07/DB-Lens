"""Thin entry point for the DB-Lens PlanPatch CLI."""

from __future__ import annotations

import sys

from planpatch.cli import run


if __name__ == "__main__":
    sys.exit(run())
