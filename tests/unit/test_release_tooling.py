"""Release tooling must cover the complete tracked source set and fail closed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conftest as release_pytest
from scripts import run_ruff


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
