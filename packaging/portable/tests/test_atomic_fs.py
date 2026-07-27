"""Regression tests for transient Windows atomic replace failures."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

_portable_dir = Path(__file__).resolve().parent.parent
_src_dir = _portable_dir / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from blc_portable import atomic_fs  # noqa: E402

if TYPE_CHECKING:
    from pytest import MonkeyPatch


class _WindowsReplaceError(OSError):
    def __init__(self, winerror: int) -> None:
        super().__init__(f"Windows error {winerror}")
        self.winerror = winerror


def test_replace_retries_transient_windows_errors(monkeypatch: MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_replace(_source: str | Path, _target: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _WindowsReplaceError(5)
        if calls == 2:
            raise _WindowsReplaceError(32)

    monkeypatch.setattr(atomic_fs.sys, "platform", "win32")
    monkeypatch.setattr(atomic_fs.os, "replace", fake_replace)
    monkeypatch.setattr(atomic_fs.time, "sleep", sleeps.append)

    atomic_fs.replace_with_retry("source", "target")

    assert calls == 3
    assert sleeps == [0.05, 0.1]


def test_replace_does_not_retry_permanent_error(monkeypatch: MonkeyPatch) -> None:
    calls = 0

    def fake_replace(_source: str | Path, _target: str | Path) -> None:
        nonlocal calls
        calls += 1
        raise _WindowsReplaceError(2)

    monkeypatch.setattr(atomic_fs.sys, "platform", "win32")
    monkeypatch.setattr(atomic_fs.os, "replace", fake_replace)

    with pytest.raises(OSError, match="Windows error 2"):
        atomic_fs.replace_with_retry("source", "target")

    assert calls == 1


def test_replace_raises_after_retry_budget(monkeypatch: MonkeyPatch) -> None:
    calls = 0

    def fake_replace(_source: str | Path, _target: str | Path) -> None:
        nonlocal calls
        calls += 1
        raise _WindowsReplaceError(33)

    monkeypatch.setattr(atomic_fs.sys, "platform", "win32")
    monkeypatch.setattr(atomic_fs.os, "replace", fake_replace)
    monkeypatch.setattr(atomic_fs.time, "sleep", lambda _delay: None)

    with pytest.raises(OSError, match="Windows error 33"):
        atomic_fs.replace_with_retry("source", "target", attempts=3)

    assert calls == 3


def test_replace_rejects_invalid_retry_budget() -> None:
    with pytest.raises(ValueError, match="attempts"):
        atomic_fs.replace_with_retry("source", "target", attempts=0)


def test_portable_code_routes_atomic_replaces_through_helper() -> None:
    direct_callers = []
    package_root = _src_dir / "blc_portable"
    for path in package_root.rglob("*.py"):
        if path.name != "atomic_fs.py" and "os.replace(" in path.read_text(encoding="utf-8"):
            direct_callers.append(path.relative_to(package_root).as_posix())

    assert direct_callers == []
