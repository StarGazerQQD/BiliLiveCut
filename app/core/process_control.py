"""支持超时和协作取消的子进程执行工具。"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from app.analysis.transcription.process_tree import ProcessTree
from app.core.execution import retain_cleanup
from app.core.process_spawn import SpawnPlan
from app.core.sanitize import safe_exception_summary
from app.core.supervisor import remaining_shutdown


class ProcessCancelledError(RuntimeError):
    """外部命令因用户取消而终止。"""


class ProcessShutdownError(ProcessCancelledError):
    """服务关闭中止了受监管命令，可安全重新排队且不计为业务失败。"""


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
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """监管外部命令及后代；kwargs 仅透传 Popen 选项，取消/超时确认进程树退出。"""
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("capture_output 不能与 stdout/stderr 同时使用")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    from app.pipeline.lifecycle import shutdown_event, track_subprocess, untrack_subprocess

    if shutdown_event.is_set():
        raise ProcessShutdownError("服务正在关闭，不再启动媒体命令")
    with SpawnPlan(command, {**kwargs, "text": text}) as plan:
        process = subprocess.Popen(plan.command, **plan.options)

        def cleanup_starting() -> None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)

        release_starting = retain_cleanup(cleanup_starting)
        try:
            tree = ProcessTree(process.pid)
        except BaseException:
            cleanup_starting()
            release_starting()
            raise
        plan.attach(tree)
        track_subprocess(process, tree)

        def cleanup() -> None:
            terminate_owned_process(process, tree)
            untrack_subprocess(process)

        release = retain_cleanup(cleanup)
        release_starting()
        try:
            tree.resume_suspended()
            plan.confirm()
        except BaseException:
            cleanup()
            release()
            raise
    started = time.monotonic()
    stdout = stderr = None
    try:
        while True:
            if shutdown_event.is_set():
                raise ProcessShutdownError("服务关闭，中止媒体命令")
            if cancel_check is not None and cancel_check():
                raise ProcessCancelledError("用户取消了外部处理")
            if timeout is not None and time.monotonic() - started > timeout:
                stdout, stderr = terminate_owned_process(process, tree)
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            try:
                stdout, stderr = process.communicate(timeout=poll_interval_s)
                if shutdown_event.is_set():
                    raise ProcessShutdownError("服务关闭，中止媒体命令")
                break
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    # 命令已退出但后代仍继承管道，先收尾进程树再排空输出。
                    tree.terminate()
                continue
    except BaseException as error:
        # 取消回调或管道读取本身异常也必须回收子进程。
        try:
            terminate_owned_process(process, tree)
        except (OSError, RuntimeError, subprocess.SubprocessError) as cleanup_error:
            error.add_note(f"子进程清理失败: {safe_exception_summary(cleanup_error)}")
        raise
    finally:
        tree.terminate()
        if process.poll() is not None:
            untrack_subprocess(process)
            release()
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, output=stdout, stderr=stderr)
    return result


def terminate_owned_process(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], tree: ProcessTree, *, drain_output: bool = True
) -> tuple[str | bytes | None, str | bytes | None]:
    """先等待直接子进程停止，再终止独占后代并排空管道、确认父进程退出。"""
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # 子进程在 poll 与 terminate 之间退出。
    try:
        process.wait(timeout=remaining_shutdown(3))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    tree.terminate()
    if drain_output:
        return process.communicate(timeout=remaining_shutdown(3))
    process.wait(timeout=remaining_shutdown(3))
    return None, None
