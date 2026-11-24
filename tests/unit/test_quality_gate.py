"""Quality gates must reject real failures and retain reproducible tool versions."""

from __future__ import annotations

import hashlib
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


def _workflow_bash() -> str:
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        assert git is not None
        candidate = Path(git).resolve().parents[1] / "bin/bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    assert bash is not None, "Bash is required to exercise the workflow"
    return bash


@pytest.mark.parametrize("tag", ['v0.1.19.3-Alpha"; touch injected; #', "$(touch injected)"])
def test_release_rejects_injected_tag_without_executing_it(tmp_path: Path, tag: str) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    step = next(item for item in workflow["jobs"]["validate-tag"]["steps"] if item.get("name") == "Validate tag input")
    assert "${{ github.event.inputs.tag }}" not in step["run"]
    env = os.environ | {"EVENT_NAME": "workflow_dispatch", "REQUESTED_TAG": tag}
    result = subprocess.run(
        [_workflow_bash(), "-c", step["run"]], cwd=tmp_path, env=env, timeout=10, capture_output=True
    )
    assert result.returncode != 0
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize(
    ("scenario", "expected", "succeeds"),
    [
        ("new", ["create"], True),
        ("empty", ["upload a.zip z.zip"], True),
        ("partial", ["upload a.zip"], True),
        ("complete", [], True),
        ("draft", ["upload a.zip", "edit"], True),
        ("conflict", [], False),
        ("no-digest", [], False),
        ("unfinished", [], False),
        ("query-error", [], False),
        ("upload-error", ["upload a.zip"], False),
        ("moved-tag", [], False),
    ],
)
def test_publish_verified_release_assets(tmp_path: Path, scenario: str, expected: list[str], succeeds: bool) -> None:
    workflow = yaml.safe_load((ci_gate.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["release"]["steps"] if s.get("name") == "Publish verified release assets")
    assets = tmp_path / "release-assets"
    assets.mkdir()
    for name in ("a.zip", "z.zip"):
        (assets / name).write_bytes(b"verified")
    digest = "sha256:" + hashlib.sha256(b"verified").hexdigest()
    tag = "v1.2.3-Alpha"
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "tag",
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "tag", tag], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "."], cwd=tmp_path, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    draft = "true" if scenario in ("draft", "upload-error") else "false"
    (tmp_path / "releases").write_text("" if scenario == "new" else f"{tag}\t42\t{draft}\n", encoding="utf-8")
    inventory = []
    if scenario == "complete":
        inventory.append(f"a.zip\tuploaded\t{digest}")
    if scenario not in ("new", "empty"):
        state = "starter" if scenario == "unfinished" else "uploaded"
        remote_digest = {"conflict": "sha256:wrong", "no-digest": "-"}.get(scenario, digest)
        inventory.append(f"z.zip\t{state}\t{remote_digest}")
    (tmp_path / "inventory").write_text("\n".join(inventory) + "\n", encoding="utf-8")
    gh_boundary = r"""
gh() {
  case "$1 $2" in
    "api --paginate")
      case "$3" in
        */releases\?*) [[ "$SCENARIO" != query-error ]] && cat releases ;;
        */releases/42/assets\?*) cat inventory ;;
        *) return 90 ;;
      esac ;;
    "release create")
      [[ "$*" == *" --verify-tag "* && "$*" == *" --prerelease "* ]] || return 91
      echo create >> calls ;;
    "release upload")
      shift 3
      printf 'upload' >> calls
      printf ' %s' "${@#release-assets/}" >> calls
      echo >> calls
      [[ "$SCENARIO" != upload-error ]] ;;
    "release edit")
      [[ "$*" == *" --draft=false --prerelease=true" ]] || return 92
      echo edit >> calls ;;
    *) return 93 ;;
  esac
}
"""
    env = os.environ | {
        "BLC_RELEASE_TAG": tag,
        "GITHUB_SHA": "0" * 40 if scenario == "moved-tag" else sha,
        "PRERELEASE": "true",
        "SCENARIO": scenario,
        "RUNNER_TEMP": ".",
    }
    assert step["shell"] == "bash"
    result = subprocess.run(
        [_workflow_bash(), "-eo", "pipefail", "-c", gh_boundary + step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert (result.returncode == 0) is succeeds, result.stderr
    calls = tmp_path / "calls"
    assert (calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []) == expected
    if scenario in ("conflict", "unfinished", "no-digest"):
        assert "z.zip" in result.stdout


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
    results = {key: "success" for key in step["env"] if key.endswith("_RESULT")}
    env = os.environ | results | {"EVENT_NAME": event_name, "MACOS_RESULT": macos_result}
    result = subprocess.run(
        [_workflow_bash(), "-c", step["run"]], cwd=tmp_path, env=env, timeout=10, capture_output=True
    )
    assert (result.returncode == 0) is succeeds, result.stderr
