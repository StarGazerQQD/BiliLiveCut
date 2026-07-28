"""Tests for fail-closed Portable runtime dependency auditing."""

from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import audit_portable_runtime_locks as lock_audit
from scripts.check_pip_audit_exemptions import (
    AuditReportError,
    Vulnerability,
    check_exemptions,
    load_exemptions,
    parse_audit_report,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _report(*, vulnerabilities: list[dict[str, object]] | None = None) -> str:
    return json.dumps(
        {
            "dependencies": [
                {
                    "name": "Example_Package",
                    "version": "1.2.3",
                    "vulns": vulnerabilities or [],
                }
            ],
            "fixes": [],
        }
    )


def _exemptions(*entries: dict[str, object]) -> dict[str, object]:
    return {"format_version": 1, "exemptions": list(entries)}


def test_parse_audit_report_normalizes_packages_and_deduplicates_aliases() -> None:
    vulnerability = {
        "id": "PYSEC-2026-1",
        "aliases": ["CVE-2026-0001", "GHSA-abcd-efgh-ijkl"],
        "fix_versions": ["1.2.4"],
    }
    report = parse_audit_report(_report(vulnerabilities=[vulnerability, vulnerability]))

    assert report.dependencies == frozenset({("example-package", "1.2.3")})
    assert len(report.vulnerabilities) == 1
    assert report.vulnerabilities[0].identifiers == {
        "PYSEC-2026-1",
        "CVE-2026-0001",
        "GHSA-abcd-efgh-ijkl",
    }


@pytest.mark.parametrize(
    "payload",
    ["not-json", "{}", '{"dependencies": [{}]}', '{"dependencies": [{"name": "x", "version": "1"}]}'],
)
def test_parse_audit_report_rejects_incomplete_output(payload: str) -> None:
    with pytest.raises(AuditReportError):
        parse_audit_report(payload)


def test_check_exemptions_requires_matching_package_and_accepts_alias() -> None:
    vulnerability = Vulnerability("example-package", "1.2.3", "PYSEC-2026-1", ("CVE-2026-0001",))
    active_until = (date.today() + timedelta(days=30)).isoformat()
    wrong_package = _exemptions(
        {
            "cve": "CVE-2026-0001",
            "package": "other-package",
            "reason": "Test only",
            "owner": "security",
            "expires": active_until,
        }
    )
    matching = _exemptions(
        {
            "cve": "CVE-2026-0001",
            "package": "Example_Package",
            "reason": "Test only",
            "owner": "security",
            "expires": active_until,
        }
    )

    assert not check_exemptions([vulnerability], wrong_package)[0]
    assert check_exemptions([vulnerability], matching) == (True, [])


def test_check_exemptions_rejects_expired_match() -> None:
    vulnerability = Vulnerability("example-package", "1.2.3", "CVE-2026-0001", ())
    expired = _exemptions(
        {
            "cve": "CVE-2026-0001",
            "package": "example-package",
            "reason": "Test only",
            "owner": "security",
            "expires": (date.today() - timedelta(days=1)).isoformat(),
        }
    )

    covered, uncovered = check_exemptions([vulnerability], expired)

    assert not covered
    assert "EXPIRED" in uncovered[0]


def test_load_exemptions_requires_auditable_fields(tmp_path: Path) -> None:
    exemptions = tmp_path / "exemptions.json"
    exemptions.write_text(
        json.dumps({"format_version": 1, "exemptions": [{"cve": "CVE-2026-0001"}]}),
        encoding="utf-8",
    )

    with pytest.raises(AuditReportError, match="missing required fields"):
        load_exemptions(exemptions)


def test_audit_lock_fails_closed_when_report_omits_locked_package(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    lock = tmp_path / "runtime.lock"
    lock.write_text(
        "example-package==1.2.3 --hash=sha256:" + "a" * 64 + "  # example_package-1.2.3-py3-none-any.whl\n",
        encoding="utf-8",
    )
    incomplete_report = json.dumps({"dependencies": [], "fixes": []})
    result = subprocess.CompletedProcess([], 0, stdout=incomplete_report, stderr="")
    monkeypatch.setattr(lock_audit.subprocess, "run", lambda *args, **kwargs: result)

    assert not lock_audit.audit_lock(lock)


def test_audit_lock_fails_closed_on_scanner_error(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    lock = tmp_path / "runtime.lock"
    lock.write_text(
        "example-package==1.2.3 --hash=sha256:" + "a" * 64 + "  # example_package-1.2.3-py3-none-any.whl\n",
        encoding="utf-8",
    )
    result = subprocess.CompletedProcess([], 2, stdout=_report(), stderr="scanner failed")
    monkeypatch.setattr(lock_audit.subprocess, "run", lambda *args, **kwargs: result)

    assert not lock_audit.audit_lock(lock)


def test_audit_lock_retries_when_scanner_emits_no_json(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    lock = tmp_path / "runtime.lock"
    lock.write_text(
        "example-package==1.2.3 --hash=sha256:" + "a" * 64 + "  # example_package-1.2.3-py3-none-any.whl\n",
        encoding="utf-8",
    )
    results = iter(
        (
            subprocess.CompletedProcess([], 2, stdout="", stderr="network timeout"),
            subprocess.CompletedProcess([], 0, stdout=_report(), stderr=""),
        )
    )
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return next(results)

    monkeypatch.setattr(lock_audit.subprocess, "run", fake_run)
    monkeypatch.setattr(lock_audit.time, "sleep", lambda _seconds: None)

    assert lock_audit.audit_lock(lock)
    assert calls == 2


def test_audit_lock_fails_closed_after_retry_limit(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    lock = tmp_path / "runtime.lock"
    lock.write_text(
        "example-package==1.2.3 --hash=sha256:" + "a" * 64 + "  # example_package-1.2.3-py3-none-any.whl\n",
        encoding="utf-8",
    )
    calls = 0

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess([], 2, stdout="", stderr="network timeout")

    monkeypatch.setattr(lock_audit.subprocess, "run", fake_run)
    monkeypatch.setattr(lock_audit.time, "sleep", lambda _seconds: None)

    assert not lock_audit.audit_lock(lock)
    assert calls == lock_audit.AUDIT_ATTEMPTS
