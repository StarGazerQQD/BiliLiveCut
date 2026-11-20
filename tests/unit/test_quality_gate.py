"""Quality gates must reject real failures and retain reproducible tool versions."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts import ci_gate


def test_hook_tool_versions_match_development_and_ci() -> None:
    root = ci_gate.REPO_ROOT
    config = yaml.safe_load((root / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = metadata["project"]["optional-dependencies"]["dev"]
    ruff_repo = next(repo for repo in config["repos"] if repo["repo"].endswith("/ruff-pre-commit"))
    assert len(ruff_repo["rev"]) == 40
    ruff_dependency = next(value for value in dependencies if value.startswith("ruff=="))
    assert all(ruff_dependency in hook["additional_dependencies"] for hook in ruff_repo["hooks"])
    version = config["minimum_pre_commit_version"]
    assert f"pre-commit=={version}" in dependencies
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert any(f"pre-commit=={version}" in step.get("run", "") for step in workflow["jobs"]["lint"]["steps"])


@pytest.mark.parametrize("workflow_name,job_name", [("ci.yml", "lint"), ("release.yml", "test")])
def test_react_hooks_prepare_locked_dependencies_before_clean_checkout_checks(
    workflow_name: str, job_name: str
) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    checks = next(i for i, step in enumerate(steps) if "pre_commit run --all-files" in step.get("run", ""))
    installs = [i for i, step in enumerate(steps[:checks]) if step.get("run", "").strip() == "npm --prefix frontend ci"]
    assert installs, f"{workflow_name}/{job_name}: React hooks require npm ci before the shared checks"
    install = installs[-1]
    setup = next(step for step in steps[:install] if step.get("uses", "").startswith("actions/setup-node@"))
    assert str(setup["with"]["node-version"]) == "24"
    for step in (setup, steps[install], steps[checks]):
        assert not step.get("if")
        assert not step.get("continue-on-error", False)


@pytest.mark.parametrize(
    "workflow_name,job_name",
    [("ci.yml", "portable-test"), ("release.yml", "build-payload"), ("release.yml", "build-sdist")],
)
def test_frontend_distribution_jobs_prepare_compatible_node(workflow_name: str, job_name: str) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    command = "python -m build " if job_name == "build-sdist" else "python build_payload.py"
    build = next(i for i, step in enumerate(steps) if command in step.get("run", ""))
    setups = [step for step in steps[:build] if step.get("uses", "").startswith("actions/setup-node@")]
    assert setups, f"{workflow_name}/{job_name}: Distribution includes a frontend build and requires Node"
    assert str(setups[-1]["with"]["node-version"]) == "24"
    assert not setups[-1].get("if")
    assert not setups[-1].get("continue-on-error", False)


def test_uv_default_build_tools_match_reproducible_wheel_builder() -> None:
    from scripts.build_portable_runtime_wheels import REQUIRED_BUILD_TOOLS

    metadata = tomllib.loads((ci_gate.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for name, version in REQUIRED_BUILD_TOOLS.items():
        assert f"{name}=={version}" in metadata["dependency-groups"]["dev"]


def test_local_gate_rejects_real_pytest_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_skipped.py").write_text(
        "import pytest\ndef test_missing_dependency():\n    pytest.skip('missing dependency')\n", encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text(
        (ci_gate.REPO_ROOT / "conftest.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(ci_gate, "REPO_ROOT", tmp_path)
    assert ci_gate._pytest("tests/") is False


def test_payload_failure_cannot_validate_old_portable_artifacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suites: list[str] = []
    monkeypatch.setattr(ci_gate, "_run", lambda command, **kwargs: "packaging/portable/build_payload.py" not in command)
    monkeypatch.setattr(ci_gate, "_pytest", lambda path, **kwargs: suites.append(path) or True)
    monkeypatch.setattr(sys, "argv", ["ci_gate.py", "--skip-audit"])
    assert ci_gate.main() == 1
    assert suites == ["tests/"]
    assert "refusing to test stale artifacts" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--skip-portable", "--skip-coverage", "--skip-audit"])
def test_partial_gate_does_not_report_full_ci_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    monkeypatch.setattr(ci_gate, "_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(ci_gate, "_pytest", lambda *args, **kwargs: True)
    monkeypatch.setattr(sys, "argv", ["ci_gate.py", flag])
    assert ci_gate.main() == 0
    output = capsys.readouterr().out
    assert "full CI gate not evaluated" in output
    assert "ALL CHECKS PASSED" not in output


def test_shared_hooks_are_immutable_and_do_not_modify_sources() -> None:
    config = yaml.safe_load((ci_gate.REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    for repo in config["repos"]:
        if repo["repo"] != "local":
            assert re.fullmatch(r"[0-9a-f]{40}", repo["rev"])
        for hook in repo["hooks"]:
            assert "--fix" not in hook.get("args", [])
            if hook["id"] == "ruff-format":
                assert "--check" in hook["args"]
            if hook["id"] == "check-merge-conflict":
                assert "--assume-in-merge" in hook["args"]


def test_release_build_jobs_have_read_only_tokens_and_keep_tls_verification() -> None:
    for filename in ("ci.yml", "release.yml"):
        text = (ci_gate.REPO_ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        assert workflow["permissions"]["contents"] == "read"
        assert "--trusted-host" not in text
        for name, job in workflow["jobs"].items():
            if filename == "release.yml" and name == "release":
                assert job["permissions"]["contents"] == "write"
            else:
                assert job.get("permissions", workflow["permissions"])["contents"] == "read"
            for step in job.get("steps", []):
                action = step.get("uses", "")
                if action.startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False
                if action.startswith(("actions/setup-node@", "actions/cache@", "astral-sh/setup-uv@")):
                    assert re.fullmatch(r"[0-9a-f]{40}", action.split("@", 1)[1])


@pytest.mark.parametrize("tag", ['v0.1.19.3-Alpha"; touch injected; #', "$(touch injected)"])
def test_release_rejects_injected_tag_without_executing_it(tmp_path: Path, tag: str) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    step = next(item for item in workflow["jobs"]["validate-tag"]["steps"] if item.get("name") == "Validate tag input")
    assert "${{ github.event.inputs.tag }}" not in step["run"]
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        candidate = Path(git).resolve().parents[1] / "bin/bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    assert bash is not None, "Bash is required to exercise the actual Release tag validator"
    env = os.environ | {"EVENT_NAME": "workflow_dispatch", "REQUESTED_TAG": tag}
    result = subprocess.run([bash, "-c", step["run"]], cwd=tmp_path, env=env, timeout=10, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize(
    ("event_name", "macos_result", "succeeds"),
    [
        ("push", "success", True),
        ("push", "failure", False),
        ("push", "skipped", False),
        ("workflow_dispatch", "success", True),
        ("workflow_dispatch", "failure", False),
        ("workflow_dispatch", "skipped", False),
        ("pull_request", "success", True),
        ("pull_request", "skipped", False),
        ("pull_request", "failure", False),
    ],
)
def test_ci_status_requires_macos_for_all_source_events(
    tmp_path: Path, event_name: str, macos_result: str, succeeds: bool
) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = workflow["jobs"]["ci-status"]["steps"][0]
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        candidate = Path(git).resolve().parents[1] / "bin/bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    assert bash is not None
    results = {key: "success" for key in step["env"] if key.endswith("_RESULT")}
    env = os.environ | results | {"EVENT_NAME": event_name, "MACOS_RESULT": macos_result}
    result = subprocess.run([bash, "-c", step["run"]], cwd=tmp_path, env=env, timeout=10, capture_output=True)
    assert (result.returncode == 0) is succeeds, result.stderr
