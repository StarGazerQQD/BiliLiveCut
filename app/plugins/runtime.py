"""CLI 的异步插件作用域，与 Web 共用进程来源注册器。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.plugins.manager import plugin_manager


@asynccontextmanager
async def plugin_runtime() -> AsyncIterator[None]:
    """在同一事件循环初始化插件并完成清理，包括初始化部分失败的情况。"""
    try:
        await plugin_manager.start()
        yield
    finally:
        await plugin_manager.stop()
