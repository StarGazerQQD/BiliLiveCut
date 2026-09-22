"""取消期间等待已开始的资源收尾和不可撤销写入。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def complete_cleanup(operation: Awaitable[None]) -> None:
    """屏蔽重复取消直至操作结束，再把取消传回调用者。"""
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
