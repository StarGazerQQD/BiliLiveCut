"""Release tooling must cover the complete tracked source set and fail closed."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import yaml

import conftest as release_pytest
from scripts import run_ruff

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_tracked_ruff_scope_includes_previously_missed_files() -> None:
    """Ruff's release scope includes tracked sources and excludes pending deletions."""
    files = set(run_ruff.tracked_python_files())
    assert "packaging/portable/config/model_catalog.py" in files
    assert "scripts/run_coverage.py" in files
    assert all((run_ruff.REPO_ROOT / path).is_file() for path in files)


def test_fail_on_skip_option_sets_failing_exit_status() -> None:
    """A skipped test changes an otherwise successful release session to failure."""

    class Config:
        pluginmanager = SimpleNamespace(
            get_plugin=lambda _name: SimpleNamespace(
                stats={"skipped": [object()]},
                write_sep=lambda *_args, **_kwargs: None,
            )
        )

        @staticmethod
        def getoption(name: str) -> bool:
            return name == "--fail-on-skip"

    session = SimpleNamespace(config=Config(), exitstatus=pytest.ExitCode.OK)
    release_pytest.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_release_gate_cannot_disable_payload_or_portable_checks() -> None:
    """The release gate exposes no skip flags or reproducibility bypass."""
    source = (run_ruff.REPO_ROOT / "scripts" / "release_gate.py").read_text(encoding="utf-8")
    assert "--skip-payload" not in source
    assert "--skip-portable" not in source
    assert "--skip-reproducible" not in source


def test_rust_build_uses_current_python_interpreter() -> None:
    """PyO3 构建必须显式使用当前虚拟环境的 Python。"""
    source = (run_ruff.REPO_ROOT / "tools" / "native" / "build_rust.py").read_text(encoding="utf-8")
    assert 'env.setdefault("PYO3_PYTHON", sys.executable)' in source


def test_rust_build_streams_cargo_output(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Cargo 构建继承控制台输出，不再缓存到进程结束。"""
    from tools.native import build_rust

    rust_source = tmp_path / "rust"
    release_dir = rust_source / "target" / "release"
    release_dir.mkdir(parents=True)
    (rust_source / "Cargo.toml").write_text("[package]\nname='fixture'\n", encoding="utf-8")
    source_suffix = ".dll" if sys.platform == "win32" else ".so"
    destination_suffix = ".pyd" if sys.platform == "win32" else ".so"
    (release_dir / f"_rust_cluster{source_suffix}").write_bytes(b"native")
    target_dir = tmp_path / "analysis"

    observed_kwargs: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        observed_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(build_rust, "RUST_SRC", rust_source)
    monkeypatch.setattr(build_rust, "TARGET_DIR", target_dir)
    monkeypatch.setattr(build_rust.subprocess, "run", fake_run)

    assert build_rust.build() is True
    assert "capture_output" not in observed_kwargs
    assert observed_kwargs["env"]["PYO3_PYTHON"] == sys.executable  # type: ignore[index]
    assert (target_dir / f"_rust_cluster{destination_suffix}").read_bytes() == b"native"


def test_windows_payload_jobs_run_on_windows_and_verify_native_modules() -> None:
    """CI 和 Release 必须在 Windows 构建目标平台原生模块。"""
    release_workflow = yaml.safe_load(
        (run_ruff.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    ci_workflow = yaml.safe_load((run_ruff.REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    assert release_workflow["jobs"]["build-payload"]["runs-on"] == "windows-latest"
    assert ci_workflow["jobs"]["portable-test"]["runs-on"] == "windows-latest"

    release_source = (run_ruff.REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "Missing Windows native modules" in release_source
    assert "Foreign native modules in Windows Payload" in release_source
    assert "Full Bundle native acceleration OK" in release_source
