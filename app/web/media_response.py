# ruff: noqa: D100
from __future__ import annotations

import asyncio
from pathlib import Path

from filelock import AsyncReadWriteLock
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from app.core.execution import run_sync
from app.core.paths import storage_root


def _new_response_lock() -> AsyncReadWriteLock:
    return AsyncReadWriteLock(storage_root() / ".media-references.lock", is_singleton=False)


def _close_unused_lock(lock: AsyncReadWriteLock) -> None:
    asyncio.run(lock.close())


class MediaFileResponse(FileResponse):
    """在实际文件传输期间持有共享媒体门，断开或取消后释放专属锁线程。"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """保留 FileResponse 的范围请求和缓存语义，等待文件读取及发送结束。"""
        lock = await run_sync(_new_response_lock, cancel_result=_close_unused_lock)
        try:
            async with lock.read_lock():
                try:
                    self.stat_result = await run_sync(Path(self.path).stat)
                except FileNotFoundError as exc:
                    raise HTTPException(404, "媒体已不存在") from exc
                self.set_stat_headers(self.stat_result)
                # 交由 FileResponse 分块读取，避免 pathsend 在门释放后才打开文件。
                protected_scope = dict(scope)
                protected_scope["extensions"] = {
                    name: value
                    for name, value in scope.get("extensions", {}).items()
                    if name != "http.response.pathsend"
                }
                await super().__call__(protected_scope, receive, send)
        finally:
            await lock.close()
