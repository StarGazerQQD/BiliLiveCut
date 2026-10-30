# ruff: noqa: D100
from __future__ import annotations

import asyncio
import json
import os
import select
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from loguru import logger

from app.analysis.transcription import process_tree
from app.core.async_cleanup import complete_cleanup
from app.core.execution import resource_scope, retain_cleanup, run_sync, wait_for_completion
from app.core.sanitize import safe_exception_summary
from app.core.supervisor import remaining_shutdown, shutdown_deadline
from config import process_guard

if TYPE_CHECKING:
    from app.analysis.transcription.process_tree import ProcessTree


class SpawnPlan(AbstractContextManager):
    """共享同步与异步启动的 OS 所有权握手，保留真实命令的错误与 PID。"""

    def __init__(self, command: Sequence[str], options: dict[str, Any]) -> None:
        self.command = list(command)
        self.options = dict(options)
        self._fds: set[int] = set()
        self._status: int | None = None
        self._life: int | None = None
        self._child_ends: tuple[int, ...] = ()
        self._tree: ProcessTree | None = None
        self._executable = self.options.get("executable") or self.command[0]

    def _pipe(self) -> tuple[int, int]:
        import fcntl

        ends = list(os.pipe())
        self._fds.update(ends)
        for index, descriptor in enumerate(ends):
            if descriptor <= 2:
                # 私有管道不能被 Popen 的标准流 dup2 覆盖，也不能补开宿主已关闭的标准流。
                replacement = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 3)
                self._fds.add(replacement)
                os.close(descriptor)
                self._fds.remove(descriptor)
                ends[index] = replacement
        return ends[0], ends[1]

    def __enter__(self) -> SpawnPlan:
        """准备私有控制管道或挂起标记，所有权建立前不运行实际命令。"""
        if os.name == "nt":
            self.options["creationflags"] = self.options.get("creationflags", 0) | 0x00000004
            return self
        try:
            life_read, self._life = self._pipe()
            self._status, status_write = self._pipe()
            self._child_ends = (life_read, status_write)
            extra = list(self.options.get("pass_fds", ()))
            if self.options.pop("shell", False):
                self._executable = self.options.get("executable") or "/bin/sh"
                self.command = [self._executable, "-c", *self.command]
            self.options.pop("executable", None)
            restore_signals = self.options.get("restore_signals", True)
            self.options.update(start_new_session=True, pass_fds=(*extra, *self._child_ends))
            self.command = [
                sys.executable,
                "-I",
                str(Path(process_guard.__file__).resolve()),
                str(life_read),
                str(status_write),
                "1" if restore_signals else "0",
                json.dumps(extra),
                self._executable,
                *self.command,
            ]
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def attach(self, tree: ProcessTree) -> None:
        """先登记所有权，再等待启动状态，失败也可回收已创建的树。"""
        self._tree = tree
        for descriptor in self._child_ends:
            os.close(descriptor)
            self._fds.remove(descriptor)
        self._child_ends = ()
        if self._life is not None:
            tree.own_parent_pipe(self._life)
            self._fds.remove(self._life)
            self._life = None

    def confirm(self) -> None:
        """CLOEXEC 的 EOF 确认 exec；启动错误仍按真实 errno 返回。"""
        if self._status is None:
            return
        deadline = shutdown_deadline(10)
        content = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self._status], [], [], remaining)[0]:
                raise TimeoutError("所属进程启动握手超时")
            chunk = os.read(self._status, 4096)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > 8192:
                raise RuntimeError("所属进程启动握手超过容量")
        records = [json.loads(line) for line in content.splitlines()]
        ready = next((record for record in records if record.get("kind") == "ready"), None)
        if ready is not None:
            assert self._tree is not None
            self._tree.guardian_pid = ready["guardian"]
        error = next((record for record in records if record.get("kind") == "exec_error"), None)
        if error is not None:
            number = error["errno"]
            raise OSError(number, os.strerror(number), self._executable)
        if len(records) != 1 or records[0].get("kind") != "ready":
            raise RuntimeError("所属进程未完成启动握手")

    def __exit__(self, *args: object) -> None:
        """关闭尚未交给所属进程树的私有管道。"""
        for descriptor in self._fds:
            os.close(descriptor)
        self._fds.clear()


def spawn_owned(command: Sequence[str], **options: Any) -> tuple[subprocess.Popen, ProcessTree]:
    """启动同步外部进程，接管完成后才允许运行，失败回收所属树。"""
    with resource_scope(), SpawnPlan(command, options) as plan:
        process = subprocess.Popen(plan.command, **plan.options)
        tree: ProcessTree | None = None

        def cleanup() -> None:
            if tree is not None:
                tree.terminate()
            elif process.poll() is None:
                process.kill()
            process.communicate(timeout=remaining_shutdown(5))

        release = retain_cleanup(cleanup)
        tree = process_tree.ProcessTree(process.pid)
        plan.attach(tree)
        tree.resume_suspended()
        plan.confirm()
        release()
        return process, tree


async def terminate_owned_async(
    process: asyncio.subprocess.Process, tree: ProcessTree | None, *, drain_output: bool = False
) -> None:
    """清理失败仍持有执行，确认所属树、启动进程与管道退出后才交回占位。"""
    previous_error = None
    while True:
        try:
            if tree is not None:
                await run_sync(tree.terminate)
            elif process.returncode is None:
                process.kill()
            operation = process.communicate() if drain_output else process.wait()
            waiting = asyncio.create_task(operation)
            if await wait_for_completion(waiting):
                waiting.result()
                raise asyncio.CancelledError
            waiting.result()
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, psutil.Error) as exc:
            detail = safe_exception_summary(exc)
            if detail != previous_error:
                logger.error("所属进程清理尚未确认，继续持有执行 pid={} error={}", process.pid, detail)
                previous_error = detail
            await asyncio.sleep(0.2)


async def wait_process_exit(process: asyncio.subprocess.Process) -> int:
    """观察真实进程退出，避免继承 PIPE 的后代阻止进入所属树清理。"""
    while process.returncode is None:
        await asyncio.sleep(0.05)
    return process.returncode


async def spawn_owned_async(command: Sequence[str], **options: Any) -> tuple[asyncio.subprocess.Process, ProcessTree]:
    """异步录制启动也保留取消窗口内的真实句柄和完整进程树。"""
    with SpawnPlan(command, options) as plan:
        starting = asyncio.create_task(asyncio.create_subprocess_exec(*plan.command, **plan.options))
        cancelled = await wait_for_completion(starting)
        process = starting.result()
        tree: ProcessTree | None = None
        try:
            tree = process_tree.ProcessTree(process.pid)
            plan.attach(tree)
            if cancelled:
                raise asyncio.CancelledError
            tree.resume_suspended()
            await run_sync(plan.confirm)
            return process, tree
        except BaseException:
            await complete_cleanup(terminate_owned_async(process, tree, drain_output=True))
            raise
