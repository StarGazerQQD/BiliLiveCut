"""稳定配置代理、原子快照发布和任务内配置隔离。

数据库初始化前只读取环境；数据库地址与安装路径永远不反向依赖数据库。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from typing import TYPE_CHECKING, Literal, ParamSpec, TypeVar

from app.core.configuration_policy import RESTART_KEYS, configuration_policy

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
    restart: dict[str, object]
    revision: int


@dataclass(frozen=True)
class ConfigurationSnapshot:
    """运行单元实际保留的配置；仅由脱敏后的管理视图公开状态。"""

    settings: Settings
    extras: dict[str, str]
    boundary: Literal["task", "recording"]


_published: _PublishedSettings | None = None
_active_views: dict[int, ConfigurationSnapshot] = {}
_active_lock = RLock()


def active_settings_views() -> list[Settings]:
    """返回仍被任务使用的配置，模型回收据此保留旧任务的多窗口实例。"""
    with _active_lock:
        return [view.settings for view in _active_views.values()]


def active_configuration_snapshots() -> tuple[ConfigurationSnapshot, ...]:
    """复制当前运行快照的登记，调用方不得回显其中的凭据。"""
    with _active_lock:
        return tuple(_active_views.values())


def publish_settings(values: dict[str, object], extras: dict[str, str], *, revision: int = 0) -> None:
    """在数据库提交后一次替换配置视图，不改变活动任务的快照。"""
    from app.core.config import get_settings

    global _published
    base = get_settings()
    previous = _published
    restart = (
        previous.restart
        if previous is not None and previous.base is base
        else {key: values.get(key, getattr(base, key)) for key in RESTART_KEYS}
    )
    runtime_extras = extras | {key: json.dumps(value) for key, value in restart.items()}
    _published = _PublishedSettings(base, dict(values), runtime_extras, dict(restart), revision)


def process_configuration(*, prospective: Settings | None = None) -> tuple[Settings, dict[str, str], int]:
    """返回当前进程的完整配置；预览保存时仍保留已经启动的重启配置。"""
    from app.core.config import get_settings

    base = get_settings()
    state = _published
    active = state is not None and state.base is base
    values = prospective.model_dump() if prospective is not None else (state.overrides if active else {})
    if active:
        values = values | state.restart
    return (
        base.model_copy(update=values, deep=True),
        dict(state.extras) if active else {},
        state.revision if active else 0,
    )


def effective_settings() -> Settings:
    """取得当前任务或进程的有效配置副本。"""
    active = _task_view.get()
    if active is not None:
        current = process_settings()
        values = {
            key: getattr(current, key) for key in type(active).model_fields if configuration_policy(key).current_read
        }
        return active.model_copy(update=values, deep=True)
    return process_settings()


def process_settings() -> Settings:
    """读取当前进程视图，不继承活动任务的旧快照。"""
    from app.core.config import get_settings

    base = get_settings()
    state = _published
    values = state.overrides | state.restart if state is not None and state.base is base else {}
    return base.model_copy(update=values, deep=True)


def snapshot_extra(key: str) -> str | None:
    """读取任务固定的业务配置；其他存储键仍直接读取数据库。"""
    extras = _task_extras.get()
    if key in RESTART_KEYS or configuration_policy(key).current_read:
        from app.core.config import get_settings

        base = get_settings()
        state = _published
        if state is None or state.base is not base:
            return None
        if key in type(base).model_fields:
            value = state.restart.get(key, state.overrides.get(key, getattr(base, key)))
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        return state.extras.get(key)
    if extras is not None and key.startswith("plugin."):
        return extras.get(key, "")
    return extras.get(key) if extras is not None else None


class SettingsProxy:
    """保持模块导入引用稳定，测试覆盖仅修改进程环境对象。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> object:
        """从任务快照或最新进程配置读取属性。"""
        # 代理没有本地属性字典，也不把底层配置字典暴露为代理自身状态。
        if name == "__dict__":
            raise AttributeError(name)

        from app.core.config import get_settings

        active = _task_view.get()
        if active is not None and not configuration_policy(name).current_read:
            return getattr(active, name)
        base = get_settings()
        state = _published
        if state is not None and base is state.base:
            if name in state.restart:
                return state.restart[name]
            if name in state.overrides:
                return state.overrides[name]
        return getattr(base, name)

    def __setattr__(self, name: str, value: object) -> None:
        """为测试或本地嵌入提供不持久化的环境基线覆盖。"""
        from app.core.config import get_settings

        setattr(get_settings(), name, value)


@contextmanager
def settings_scope(
    *, fresh: bool = False, refresh: bool = True, boundary: Literal["task", "recording"] = "task"
) -> Iterator[None]:
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
    snapshot = base.model_copy(update=(state.overrides | state.restart) if active else {}, deep=True)
    token = _task_view.set(snapshot)
    extras = dict(state.extras) if active else {}
    extra_token = _task_extras.set(extras)
    with _active_lock:
        _active_views[id(snapshot)] = ConfigurationSnapshot(snapshot, extras, boundary)
    try:
        yield
    finally:
        with _active_lock:
            _active_views.pop(id(snapshot), None)
        _task_view.reset(token)
        _task_extras.reset(extra_token)


@asynccontextmanager
async def async_settings_scope(
    *, fresh: bool = False, boundary: Literal["task", "recording"] = "task"
) -> AsyncIterator[None]:
    """异步入口在线程中刷新数据库，事件循环只捕获内存快照。"""
    from app.core.config import get_settings
    from app.core.configuration import reload_configuration
    from app.core.execution import run_sync

    state = _published
    if (fresh or _task_view.get() is None) and state is not None and state.base is get_settings():
        await run_sync(reload_configuration)
    with settings_scope(fresh=fresh, refresh=False, boundary=boundary):
        yield


def configured_task(function: Callable[P, R], *, fresh: bool = False) -> Callable[P, R]:
    """让同步业务入口及其调用链使用同一配置快照。"""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        from contextlib import nullcontext

        from app.core.config import get_settings
        from app.core.execution import resource_scope
        from app.core.media_usage import media_operation

        state = _published
        usage = media_operation() if state is not None and state.base is get_settings() else nullcontext()
        with settings_scope(fresh=fresh), usage, resource_scope():
            return function(*args, **kwargs)

    return wrapped


def configured_entry(function: Callable[P, R]) -> Callable[P, R]:
    """后台调度的独立入口始终取得最新快照，不继承上游录制设置。"""
    return configured_task(function, fresh=True)
