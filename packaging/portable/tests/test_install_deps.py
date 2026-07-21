"""依赖安装测试 — lock 选择、资源路径、失败语义。"""

from __future__ import annotations

import subprocess as _sp
import sys
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_src_dir = _portable_dir / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

_lock_dir = _portable_dir / "locks"


def test_find_lock_py312_mocked() -> None:
    """Mock ABI=py312: _find_lock_file returns py312 lock."""
    from blc_portable.launcher.main import _find_lock_file  # noqa: E402

    orig = _sp.run

    def _fake(args, **kwargs):
        if "print(f'py" in str(args):
            return _sp.CompletedProcess(args, 0, stdout="py312\n", stderr="")
        return orig(args, **kwargs)

    _sp.run = _fake
    try:
        lock_file = _find_lock_file(Path(sys.executable))
        assert lock_file.exists()
        assert "requirements-runtime-py312" in str(lock_file)
    finally:
        _sp.run = orig


def test_find_lock_py311_mocked() -> None:
    """Mock ABI=py311: _find_lock_file returns py311 lock."""
    from blc_portable.launcher.main import _find_lock_file  # noqa: E402

    orig = _sp.run

    def _fake(args, **kwargs):
        if "print(f'py" in str(args):
            return _sp.CompletedProcess(args, 0, stdout="py311\n", stderr="")
        return orig(args, **kwargs)

    _sp.run = _fake
    try:
        lock_file = _find_lock_file(Path(sys.executable))
        assert lock_file.exists()
        assert "requirements-runtime-py311" in str(lock_file)
    finally:
        _sp.run = orig


def test_find_lock_unsupported_abi_raises() -> None:
    """Calling with ABI py39 must raise RuntimeError."""
    from blc_portable.launcher.main import _find_lock_file  # noqa: E402

    orig = _sp.run

    def _fake(args, **kwargs):
        if "print(f'py" in str(args):
            return _sp.CompletedProcess(args, 0, stdout="py39\n", stderr="")
        return orig(args, **kwargs)

    _sp.run = _fake
    try:
        with pytest.raises(RuntimeError, match="Lock file not found"):
            _find_lock_file(Path(sys.executable))
    finally:
        _sp.run = orig


def test_install_dep_callable() -> None:
    """install_dependencies must be importable and callable."""
    from blc_portable.launcher.main import install_dependencies  # noqa: E402

    assert callable(install_dependencies)


def test_install_dependencies_recognizes_strict_hash_lock_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed hashed requirement must not trigger a redundant reinstall."""
    from blc_portable.launcher import main  # noqa: E402

    lock_file = tmp_path / "requirements-runtime-py312-win-x64.lock"
    lock_file.write_text(
        "pydantic-settings==2.14.2 --hash=sha256:" + "0" * 64 + "  # wheel.whl\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        assert args[-2:] == ["pip", "freeze"]
        return _sp.CompletedProcess(args, 0, stdout="Pydantic_Settings==2.14.2\n", stderr="")

    monkeypatch.setattr(main, "_find_lock_file", lambda _python: lock_file)
    monkeypatch.setattr(main.subprocess, "run", _fake_run)

    main.install_dependencies(Path(sys.executable), tmp_path)

    assert len(calls) == 1


def test_lock_files_exist_in_disk() -> None:
    """Both py311 and py312 lock files must exist on disk."""
    assert (_lock_dir / "requirements-runtime-py311-win-x64.lock").exists()
    assert (_lock_dir / "requirements-runtime-py312-win-x64.lock").exists()
