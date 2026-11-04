from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

_portable_source = Path(__file__).resolve().parents[1] / "src"
if str(_portable_source) not in sys.path:
    sys.path.insert(0, str(_portable_source))

from blc_portable.launcher.main import _wait_for_service_members  # noqa: E402


@pytest.fixture
def service_member() -> Iterator[tuple[subprocess.Popen[bytes], psutil.Process]]:
    process = subprocess.Popen([sys.executable, "-c", "import sys;sys.stdin.buffer.read()"], stdin=subprocess.PIPE)
    try:
        yield process, psutil.Process(process.pid)
    finally:
        assert process.stdin is not None
        if not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_active_service_member_blocks_recovery(
    service_member: tuple[subprocess.Popen[bytes], psutil.Process],
) -> None:
    process, member = service_member
    with pytest.raises(RuntimeError, match="所属子进程仍在执行"):
        _wait_for_service_members([member], timeout=0.02)
    assert process.poll() is None


@pytest.mark.parametrize("status", [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD])
def test_terminal_os_status_does_not_require_foreign_parent_reaping(
    service_member: tuple[subprocess.Popen[bytes], psutil.Process], monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    process, member = service_member
    # 仅控制外部 OS 状态读取，覆盖 Windows 无法产生的 POSIX 终态协议。
    monkeypatch.setattr(member, "status", lambda: status)
    _wait_for_service_members([member], timeout=0.02)
    assert process.poll() is None


def test_member_status_permission_failure_remains_visible(
    service_member: tuple[subprocess.Popen[bytes], psutil.Process], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, member = service_member

    def denied() -> str:
        raise psutil.AccessDenied(member.pid)

    monkeypatch.setattr(member, "status", denied)
    with pytest.raises(psutil.AccessDenied):
        _wait_for_service_members([member], timeout=0.02)


def test_real_exited_member_does_not_need_reaping_by_the_supervisor(
    service_member: tuple[subprocess.Popen[bytes], psutil.Process],
) -> None:
    if os.name == "nt":
        process, member = service_member
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=5)
        _wait_for_service_members([member], timeout=0.02)
        assert not member.is_running()
        return

    parent = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("service_member_actor.py"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        member = psutil.Process(int(parent.stdout.readline()))
        deadline = time.monotonic() + 5
        while member.status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        # 确认旧判据确实把真实僵尸列为 alive，再验证正式监督的新判据。
        _, pending = psutil.wait_procs([member], timeout=0)
        assert pending == [member]
        _wait_for_service_members([member], timeout=0.02)
        assert member.status() == psutil.STATUS_ZOMBIE
        assert parent.poll() is None
    finally:
        try:
            _, error = parent.communicate(input="\n", timeout=5)
            assert parent.returncode == 0, error
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
    assert not member.is_running()
