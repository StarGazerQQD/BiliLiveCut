"""覆盖率门禁必须同时反映测试结果与覆盖率结果。"""

from __future__ import annotations

import contextlib
import io
from typing import TYPE_CHECKING

import yaml

from scripts import run_coverage

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_main_fails_when_pytest_fails_even_if_coverage_passes(monkeypatch) -> None:
    monkeypatch.setattr(run_coverage, "check_native_extension", lambda: (True, "loaded"))
    monkeypatch.setattr(run_coverage, "run_pytest", lambda: 2)
    monkeypatch.setattr(
        run_coverage,
        "parse_coverage_xml",
        lambda: {"statements": 100, "covered": 60, "missed": 40, "percentage": 60.0},
    )

    assert run_coverage.main() == 2


def test_run_pytest_preserves_whisper_default(monkeypatch) -> None:
    captured_env: dict[str, str] = {}
    captured_cmd: list[str] = []

    class Completed:
        returncode = 0

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        captured_cmd.extend(command)
        captured_env.update(kwargs["env"])
        return Completed()

    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--timeout=120")
    monkeypatch.setattr(run_coverage.subprocess, "run", fake_run)

    assert run_coverage.run_pytest() == 0
    assert captured_env["ASR_NO_MODEL_DOWNLOAD"] == "1"
    assert captured_env["PYTEST_ADDOPTS"] == "--timeout=120"
    assert "WHISPER_MODEL" not in captured_env
    assert "--fail-on-skip" in captured_cmd


def test_main_output_is_safe_for_windows_legacy_console(monkeypatch) -> None:
    """CI diagnostics must be printable by the Windows cp1252 console."""
    monkeypatch.setattr(
        run_coverage,
        "check_native_extension",
        lambda: (False, "NOT FOUND - C extension not compiled"),
    )
    monkeypatch.setattr(run_coverage, "run_pytest", lambda: 0)
    monkeypatch.setattr(
        run_coverage,
        "parse_coverage_xml",
        lambda: {"statements": 100, "covered": 60, "missed": 40, "percentage": 60.0},
    )
    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding="cp1252", errors="strict")

    with contextlib.redirect_stdout(console):
        assert run_coverage.main() == 0

    console.flush()
    assert b"additional lines needed" in output.getvalue()


def test_application_partition_keeps_coverage_and_required_distribution_step(monkeypatch: MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> run_coverage.subprocess.CompletedProcess[str]:
        commands.append(command)
        return run_coverage.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(run_coverage.subprocess, "run", execute)
    assert run_coverage.run_pytest() == 0
    assert run_coverage.run_pytest(application_only=True) == 0
    assert not any(item.startswith("--ignore") for item in commands[0])
    assert commands[1] == commands[0] + ["--ignore=tests/integration/test_wheel_frontend.py"]
    assert "--fail-on-skip" in commands[1] and "--cov-fail-under=50" in commands[1]
    workflow = yaml.safe_load((run_coverage.REPO_ROOT / ".github/workflows/ci.yml").read_text("utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    distribution = next(step for step in steps if "test_wheel_frontend.py" in step.get("run", ""))
    coverage = next(step for step in steps if "run_coverage.py" in step.get("run", ""))
    assert steps.index(distribution) < steps.index(coverage)
    assert "--fail-on-skip" in distribution["run"]
    assert "--application-only" in coverage["run"]
    for step in (distribution, coverage):
        assert "if" not in step and not step.get("continue-on-error", False)
    assert distribution["timeout-minutes"] == 15
    assert coverage["timeout-minutes"] == 30
    assert coverage["env"]["PYTEST_ADDOPTS"] == "--timeout=120"
    for scope in (workflow, workflow["jobs"]["test"], workflow["jobs"]["test-macos"], distribution):
        assert "PYTEST_ADDOPTS" not in scope.get("env", {})
    macos_coverage = next(
        step for step in workflow["jobs"]["test-macos"]["steps"] if "run_coverage.py" in step.get("run", "")
    )
    assert "PYTEST_ADDOPTS" not in macos_coverage["env"]
    assert macos_coverage["timeout-minutes"] == 90
