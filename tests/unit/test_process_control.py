"""可取消子进程执行工具测试。"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from app.core.process_control import ProcessCancelledError, run_cancellable


def test_run_cancellable_returns_completed_process() -> None:
    """正常命令返回标准 CompletedProcess。"""
    result = run_cancellable(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_run_cancellable_terminates_process_on_cancel() -> None:
    """取消检查为真后迅速终止子进程。"""
    started = time.monotonic()

    with pytest.raises(ProcessCancelledError):
        run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cancel_check=lambda: time.monotonic() - started > 0.2,
            timeout=10,
            poll_interval_s=0.05,
        )

    assert time.monotonic() - started < 5


def test_run_cancellable_enforces_timeout() -> None:
    """超时会终止进程并抛出 TimeoutExpired。"""
    with pytest.raises(subprocess.TimeoutExpired):
        run_cancellable(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2,
            poll_interval_s=0.05,
        )
