"""实际镜像依赖清单与漏洞扫描完整性的回归。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import audit_docker_runtime as audit

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def inventory() -> list[dict[str, str]]:
    return [
        {"name": "bili-live-cut", "version": str(audit.PROJECT_VERSION)},
        {"name": "torch", "version": "2.13.0+cpu"},
        {"name": "torchaudio", "version": "2.11.0+cpu"},
        {"name": "typing_extensions", "version": "4.16.0"},
    ]


def clean_report() -> dict[str, object]:
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []}
            for name, version in sorted(audit.dependencies_from_inventory(json.dumps(inventory())))
        ]
    }


def test_cpu_wheels_use_public_advisory_versions_without_skipping_dependencies() -> None:
    assert audit.dependencies_from_inventory(json.dumps(inventory())) == frozenset(
        {("torch", "2.13.0"), ("torchaudio", "2.11.0"), ("typing-extensions", "4.16.0")}
    )


@pytest.mark.parametrize("value", [None, {}, [], [None], [{"name": "torch"}], [{"name": "-r bad", "version": "1"}]])
def test_invalid_inventory_is_rejected(value: object) -> None:
    with pytest.raises((audit.AuditReportError, ValueError)):
        audit.dependencies_from_inventory(json.dumps(value))


def test_duplicate_canonical_name_is_rejected() -> None:
    with pytest.raises(audit.AuditReportError, match="重复"):
        audit.dependencies_from_inventory(
            json.dumps([*inventory(), {"name": "typing-extensions", "version": "4.16.0"}])
        )


@pytest.mark.parametrize("name,version", [("torch", "2.13.0+cu130"), ("typing_extensions", "4.16.0+vendor")])
def test_unknown_local_versions_are_not_silently_skipped(name: str, version: str) -> None:
    entries = inventory()
    for entry in entries:
        if entry["name"] == name:
            entry["version"] = version
    with pytest.raises(audit.AuditReportError, match="本地版本"):
        audit.dependencies_from_inventory(json.dumps(entries))


def test_wrong_project_version_is_rejected() -> None:
    entries = inventory()
    entries[0]["version"] = "0.0.1"
    with pytest.raises(audit.AuditReportError, match="版本不一致"):
        audit.dependencies_from_inventory(json.dumps(entries))


@pytest.mark.parametrize("missing", ["bili-live-cut", "torch", "torchaudio"])
def test_incomplete_image_is_rejected(missing: str) -> None:
    entries = [entry for entry in inventory() if entry["name"] != missing]
    with pytest.raises(audit.AuditReportError, match="缺少"):
        audit.dependencies_from_inventory(json.dumps(entries))


def test_audit_uses_exact_inventory_and_retains_evidence(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "--disable-pip" in command and "--no-deps" in command
        requirements = Path(command[command.index("-r") + 1]).read_text(encoding="utf-8")
        assert requirements == "torch==2.13.0\ntorchaudio==2.11.0\ntyping-extensions==4.16.0\n"
        return subprocess.CompletedProcess(command, 0, json.dumps(clean_report()), "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    text = json.dumps(inventory())
    assert audit.audit_inventory(text, tmp_path)
    assert (tmp_path / "inventory.json").read_text(encoding="utf-8") == text
    assert json.loads((tmp_path / "pip-audit.json").read_text(encoding="utf-8")) == clean_report()


@pytest.mark.parametrize(
    "mode", ["missing", "version", "skipped", "empty", "malformed", "vulnerability", "scan_failure"]
)
def test_audit_fails_closed(tmp_path: Path, monkeypatch: MonkeyPatch, mode: str) -> None:
    rows = [
        {"name": name, "version": version, "vulns": []}
        for name, version in sorted(audit.dependencies_from_inventory(json.dumps(inventory())))
    ]
    if mode == "missing":
        rows.pop()
    elif mode == "version":
        rows[0]["version"] = "2.10.0"
    elif mode == "skipped":
        rows[0] = {"name": "torch", "skip_reason": "not found"}
    elif mode == "vulnerability":
        rows[0]["vulns"] = [{"id": "CVE-2025-3000", "aliases": []}]
    report = json.dumps({"dependencies": [] if mode == "empty" else rows})
    if mode == "malformed":
        report = "service unavailable"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1 if mode == "scan_failure" else 0, report, "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    with pytest.raises(audit.AuditReportError):
        audit.audit_inventory(json.dumps(inventory()), tmp_path)


@pytest.mark.parametrize("system_failure", [None, "vulnerability", "omitted_report"])
def test_both_python_environments_must_pass(
    tmp_path: Path, monkeypatch: MonkeyPatch, system_failure: str | None
) -> None:
    commands: list[list[str]] = []
    system_inventory = [] if system_failure is None else [{"name": "urllib3", "version": "2.7.0"}]
    system_rows = [{"name": "urllib3", "version": "2.7.0", "vulns": []}]
    if system_failure == "vulnerability":
        system_rows[0]["vulns"] = [{"id": "CVE-2026-97687", "aliases": []}]
    elif system_failure == "omitted_report":
        system_rows = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "docker":
            python = command[command.index("--entrypoint") + 1]
            entries = inventory() if python == "/app/.venv/bin/python" else system_inventory
            output = json.dumps(entries)
        else:
            requirements = Path(command[command.index("-r") + 1])
            if requirements.parent.name == "application":
                output = json.dumps(clean_report())
            else:
                assert requirements.read_text(encoding="utf-8") == "urllib3==2.7.0\n"
                output = json.dumps({"dependencies": system_rows})
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.main(["--output-dir", str(tmp_path)]) == (0 if system_failure is None else 1)
    assert len(commands) == (3 if system_failure is None else 4)
    for command, python in zip(commands[::2], ["/app/.venv/bin/python", "/usr/local/bin/python"], strict=True):
        assert command[:3] == ["docker", "run", "--rm"]
        assert command[command.index("--network") + 1] == "none"
        assert command[command.index("--entrypoint") + 1] == python
        assert "--read-only" in command and "--cap-drop" in command
        assert "--env" not in command and "--volume" not in command
    assert "runtime contains pip" in commands[2][-1]
    assert "bootstrap wheels" in commands[2][-1]
    assert (tmp_path / "application" / "pip-audit.json").exists()
    if system_failure is None:
        assert not (tmp_path / "system" / "pip-audit.json").exists()
        assert json.loads((tmp_path / "system" / "inventory-check.json").read_text(encoding="utf-8")) == {
            "third_party_packages": 0,
            "vulnerability_query_executed": False,
        }
        assert json.loads((tmp_path / "system" / "inventory.json").read_text(encoding="utf-8")) == system_inventory


@pytest.mark.parametrize("failure", [FileNotFoundError("docker"), subprocess.TimeoutExpired("docker", 180)])
def test_missing_engine_or_timeout_fails(tmp_path: Path, monkeypatch: MonkeyPatch, failure: Exception) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    monkeypatch.setattr(audit.subprocess, "run", run)
    assert audit.main(["--output-dir", str(tmp_path)]) == 1


def test_ffmpeg_download_must_finish_verification_before_extracting() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "packaging/docker/Dockerfile").read_text(encoding="utf-8")
    builder = dockerfile.split("FROM python-base AS ffmpeg-builder", 1)[1].split("FROM python-base AS builder", 1)[0]
    assert "COPY scripts/download_docker_ffmpeg.py /tmp/download_docker_ffmpeg.py" in builder
    download = "RUN python /tmp/download_docker_ffmpeg.py --output /tmp/ffmpeg.tar.xz"
    assert builder.index(download) < builder.index("tar -xf /tmp/ffmpeg.tar.xz")
