"""稳定配置代理、原子快照发布和任务内配置隔离。

数据库初始化前只读取环境；数据库地址与安装路径永远不反向依赖数据库。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING, ParamSpec, TypeVar

if TYPE_CHECKING:
    from app.core.config import Settings

P = ParamSpec("P")
R = TypeVar("R")
_task_view: ContextVar[Settings | None] = ContextVar("task_settings", default=None)
_task_extras: ContextVar[dict[str, str] | None] = ContextVar("task_settings_extras", default=None)


@dataclass(frozen=True)
class _PublishedSettings:
    base: Settings
    overrides: dict[str, object]
    extras: dict[str, str]


_published: _PublishedSettings | None = None
_active_views: dict[int, Settings] = {}
_active_lock = RLock()


def active_settings_views() -> list[Settings]:
    """返回仍被任务使用的配置，模型回收据此保留旧任务的多窗口实例。"""
    with _active_lock:
        return list(_active_views.values())


def publish_settings(values: dict[str, object], extras: dict[str, str]) -> None:
    """在数据库提交后一次替换配置视图，不改变活动任务的快照。"""
    from app.core.config import get_settings

    global _published
    _published = _PublishedSettings(get_settings(), dict(values), dict(extras))


def effective_settings() -> Settings:
    """取得当前任务或进程的有效配置副本。"""
    active = _task_view.get()
    if active is not None:
        return active.model_copy(deep=True)
    return process_settings()


def process_settings() -> Settings:
    """读取当前进程视图，不继承活动任务的旧快照。"""
    from app.core.config import get_settings

    base = get_settings()
    state = _published
    return base.model_copy(update=state.overrides if state is not None and base is state.base else {}, deep=True)


def snapshot_extra(key: str) -> str | None:
    """读取任务固定的业务配置；其他存储键仍直接读取数据库。"""
    extras = _task_extras.get()
    if extras is not None and key.startswith("plugin."):
        return extras.get(key, "")
    return extras.get(key) if extras is not None else None


class SettingsProxy:
    """保持模块导入引用稳定，测试覆盖仅修改进程环境对象。"""

    def __getattr__(self, name: str) -> object:
        """从任务快照或最新进程配置读取属性。"""
        from app.core.config import get_settings

        active = _task_view.get()
        if active is not None:
            return getattr(active, name)
        base = get_settings()
        state = _published
        if state is not None and base is state.base and name in state.overrides:
            return state.overrides[name]
        return getattr(base, name)

    def __setattr__(self, name: str, value: object) -> None:
        """为测试或本地嵌入提供不持久化的环境基线覆盖。"""
        from app.core.config import get_settings

        setattr(get_settings(), name, value)


@contextmanager
def settings_scope(*, fresh: bool = False, refresh: bool = True) -> Iterator[None]:
    """固定一次操作的配置；线程入口可刷新其他进程保存的数据库覆盖。"""
    from app.core.config import get_settings

    if _task_view.get() is not None and not fresh:
        yield
        return
    state = _published
    if refresh and state is not None and state.base is get_settings():
        from app.core.configuration import reload_configuration

        reload_configuration()
    state = _published
    base = get_settings()
    active = state is not None and state.base is base
    snapshot = base.model_copy(update=state.overrides if active else {}, deep=True)
    token = _task_view.set(snapshot)
    extra_token = _task_extras.set(dict(state.extras) if active else {})
    with _active_lock:
        _active_views[id(snapshot)] = snapshot
    try:
        yield
    finally:
        with _active_lock:
            _active_views.pop(id(snapshot), None)
        _task_view.reset(token)
        _task_extras.reset(extra_token)


@asynccontextmanager
async def async_settings_scope(*, fresh: bool = False) -> AsyncIterator[None]:
    """异步入口在线程中刷新数据库，事件循环只捕获内存快照。"""
    import asyncio

    from app.core.config import get_settings
    from app.core.configuration import reload_configuration

    state = _published
    if (fresh or _task_view.get() is None) and state is not None and state.base is get_settings():
        await asyncio.to_thread(reload_configuration)
    with settings_scope(fresh=fresh, refresh=False):
        yield


def configured_task(function: Callable[P, R], *, fresh: bool = False) -> Callable[P, R]:
    """让同步业务入口及其调用链使用同一配置快照。"""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from contextlib import nullcontext

        from app.core.config import get_settings
        from app.core.media_usage import media_operation

        state = _published
        usage = media_operation() if state is not None and state.base is get_settings() else nullcontext()
        with settings_scope(fresh=fresh), usage:
            return function(*args, **kwargs)

    return wrapped


def configured_entry(function: Callable[P, R]) -> Callable[P, R]:
    """后台调度的独立入口始终取得最新快照，不继承上游录制设置。"""
    return configured_task(function, fresh=True)
