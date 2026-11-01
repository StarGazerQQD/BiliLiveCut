from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest


class ExternalProcess:
    """提供外部进程的管道协议，退出行为由具体故障场景控制。"""

    stdin = None
    stdout: asyncio.StreamReader | None = None
    stderr: asyncio.StreamReader | None = None
    returncode: int | None = None

    @property
    def pid(self) -> int:
        """返回仅用于测试注册表的身份，不传给实际操作系统。"""
        return id(self)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        output = await self.stdout.read() if self.stdout is not None else b""
        error = await self.stderr.read() if self.stderr is not None else b""
        await self.wait()
        return output, error

    def kill(self) -> None:
        raise NotImplementedError

    def terminate(self) -> None:
        self.kill()


Process = TypeVar("Process", bound=ExternalProcess)


def install_process_boundary(monkeypatch: pytest.MonkeyPatch, spawn: Callable[..., Awaitable[Process]]) -> None:
    """仅替换外部创建及 OS Job/进程组接口，执行真实所有权与清理调用链。"""
    from app.analysis.transcription import process_tree

    registry: dict[int, tuple[Process, asyncio.AbstractEventLoop]] = {}

    class ExternalTree:
        def __init__(self, pid: int) -> None:
            self.process, self.loop = registry[pid]
            self.parent_pipe: int | None = None

        def own_parent_pipe(self, descriptor: int) -> None:
            self.parent_pipe = descriptor

        def resume_suspended(self) -> None:
            pass

        def terminate(self) -> None:
            async def stop() -> None:
                try:
                    if self.process.returncode is None:
                        self.process.terminate()
                except ProcessLookupError:
                    if self.process.returncode is None:
                        raise

            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=2)
            if self.parent_pipe is not None:
                os.close(self.parent_pipe)
                self.parent_pipe = None

    async def external_spawn(*args: str, **kwargs: object) -> Process:
        process = await spawn(*(args[8:] if os.name != "nt" else args), **kwargs)
        registry[process.pid] = process, asyncio.get_running_loop()
        if os.name != "nt":
            os.write(int(args[4]), b'{"kind":"ready","guardian":987654321}\n')
        return process

    monkeypatch.setattr(process_tree, "ProcessTree", ExternalTree)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", external_spawn)
