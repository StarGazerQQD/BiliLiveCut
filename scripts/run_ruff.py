#!/usr/bin/env python3
"""Run Ruff against every Python file tracked by Git."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_python_files() -> list[str]:
    """Return existing repository-relative paths for tracked Python files."""
    result = subprocess.run(
        ["git", "ls-files", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line for line in result.stdout.splitlines() if line and (REPO_ROOT / line).is_file()]


def main(argv: list[str] | None = None) -> int:
    """Run ``ruff check`` or ``ruff format --check`` on the tracked file set."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in (["check"], ["format"]):
        print("Usage: python scripts/run_ruff.py {check|format}", file=sys.stderr)
        return 2

    files = tracked_python_files()
    if not files:
        print("No tracked Python files found", file=sys.stderr)
        return 1

    command = [sys.executable, "-m", "ruff", "check", "--no-respect-gitignore", "--", *files]
    if args == ["format"]:
        command = [sys.executable, "-m", "ruff", "format", "--check", "--no-respect-gitignore", "--", *files]
    return subprocess.run(command, cwd=REPO_ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
