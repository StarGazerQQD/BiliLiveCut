"""支持超时和协作取消的子进程执行工具。"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any


class ProcessCancelledError(RuntimeError):
    """外部命令因用户取消而终止。"""


def run_cancellable(
    command: Sequence[str],
    *,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float | None = None,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    poll_interval_s: float = 0.2,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """运行外部命令，并在取消或超时时终止子进程。"""
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("capture_output 不能与 stdout/stderr 同时使用")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    process = subprocess.Popen(command, text=text, **kwargs)
    started = time.monotonic()
    stdout = stderr = None
    while True:
        if cancel_check is not None and cancel_check():
            _terminate(process)
            raise ProcessCancelledError("用户取消了外部处理")
        if timeout is not None and time.monotonic() - started > timeout:
            _terminate(process)
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            stdout, stderr = process.communicate(timeout=poll_interval_s)
            break
        except subprocess.TimeoutExpired:
            continue
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, output=stdout, stderr=stderr)
    return result


def _terminate(process: subprocess.Popen) -> None:
    """先优雅终止，超时后强制结束精确子进程。"""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)
