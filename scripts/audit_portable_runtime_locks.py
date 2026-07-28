#!/usr/bin/env python3
"""Audit both Portable Windows runtime locks and fail closed on scan errors."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from scripts.check_pip_audit_exemptions import (
        AuditReport,
        AuditReportError,
        canonicalize_name,
        check_exemptions,
        load_exemptions,
        parse_audit_report,
    )
else:
    from check_pip_audit_exemptions import (
        AuditReport,
        AuditReportError,
        canonicalize_name,
        check_exemptions,
        load_exemptions,
        parse_audit_report,
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_DIR = REPO_ROOT / "packaging" / "portable" / "locks"
DEFAULT_LOCKS = (
    LOCK_DIR / "requirements-runtime-py311-win-x64.lock",
    LOCK_DIR / "requirements-runtime-py312-win-x64.lock",
)
LOCK_ENTRY_PATTERN = re.compile(r"^(?P<name>[^=\s]+)==(?P<version>\S+)\s+--hash=sha256:[0-9a-f]{64}")
AUDIT_ATTEMPTS = 3
AUDIT_RETRY_DELAY_SECONDS = 2


def locked_dependencies(lock_path: Path) -> frozenset[tuple[str, str]]:
    """Return all normalized package/version pairs from one strict lock."""
    dependencies: set[tuple[str, str]] = set()
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        match = LOCK_ENTRY_PATTERN.match(raw_line)
        if not match:
            raise AuditReportError(f"Invalid strict lock entry in {lock_path.name}: {raw_line}")
        item = (canonicalize_name(match.group("name")), match.group("version"))
        if item in dependencies:
            raise AuditReportError(f"Duplicate lock entry in {lock_path.name}: {item[0]}=={item[1]}")
        dependencies.add(item)
    if not dependencies:
        raise AuditReportError(f"Runtime lock is empty: {lock_path}")
    return frozenset(dependencies)


def _run_audit_with_retries(
    command: list[str], lock_path: Path
) -> tuple[subprocess.CompletedProcess[str], AuditReport] | None:
    """Run pip-audit with bounded retries when no valid JSON report is produced."""
    last_error = "pip-audit did not run"
    last_stderr = ""
    for attempt in range(1, AUDIT_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = str(exc)
            last_stderr = ""
        else:
            last_stderr = result.stderr.strip()
            try:
                return result, parse_audit_report(result.stdout)
            except AuditReportError as exc:
                last_error = str(exc)

        if attempt < AUDIT_ATTEMPTS:
            print(
                f"WARN {lock_path.name}: pip-audit attempt {attempt}/{AUDIT_ATTEMPTS} "
                f"produced no valid report ({last_error}); retrying",
                file=sys.stderr,
            )
            time.sleep(AUDIT_RETRY_DELAY_SECONDS * attempt)

    print(f"FAIL {lock_path.name}: {last_error}", file=sys.stderr)
    if last_stderr:
        print(last_stderr, file=sys.stderr)
    return None


def audit_lock(lock_path: Path) -> bool:
    """Run pip-audit for one lock and validate complete, exempted results."""
    expected = locked_dependencies(lock_path)
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--strict",
        "--disable-pip",
        "--no-deps",
        "--format",
        "json",
        "--requirement",
        str(lock_path),
    ]
    scan = _run_audit_with_retries(command, lock_path)
    if scan is None:
        return False
    result, report = scan

    if report.dependencies != expected:
        missing = sorted(expected - report.dependencies)
        unexpected = sorted(report.dependencies - expected)
        print(f"FAIL {lock_path.name}: pip-audit report does not cover the complete lock", file=sys.stderr)
        if missing:
            print(f"  Missing: {missing}", file=sys.stderr)
        if unexpected:
            print(f"  Unexpected: {unexpected}", file=sys.stderr)
        return False
    if result.returncode not in (0, 1):
        print(f"FAIL {lock_path.name}: pip-audit exited with {result.returncode}", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return False
    if result.returncode == 0 and report.vulnerabilities:
        print(f"FAIL {lock_path.name}: pip-audit returned success with vulnerabilities", file=sys.stderr)
        return False
    if result.returncode == 1 and not report.vulnerabilities:
        print(f"FAIL {lock_path.name}: pip-audit failed without a vulnerability report", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return False

    exemptions = load_exemptions()
    covered, uncovered = check_exemptions(report.vulnerabilities, exemptions)
    if not covered:
        print(f"FAIL {lock_path.name}: {len(uncovered)} unexempted vulnerabilities", file=sys.stderr)
        for item in uncovered:
            print(f"  {item}", file=sys.stderr)
        return False

    status = "clean" if not report.vulnerabilities else f"{len(report.vulnerabilities)} explicitly exempted"
    print(f"PASS {lock_path.name}: {len(expected)} dependencies, {status}")
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse optional explicit lock paths for tests and diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locks", nargs="*", type=Path, default=list(DEFAULT_LOCKS))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Audit every requested lock sequentially."""
    args = parse_args(argv)
    try:
        results = [audit_lock(path.resolve()) for path in args.locks]
    except (AuditReportError, FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        print(f"Portable runtime lock audit failed: {exc}", file=sys.stderr)
        return 2
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
