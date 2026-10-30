"""用操作系统文件锁登记跨进程媒体使用，崩溃后锁自动释放。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import get_ident
from typing import BinaryIO

from filelock import ReadWriteLock, Timeout
from loguru import logger
from sqlmodel import select

from app.core.paths import storage_root
from app.db import session as database
from app.db.entities import AppSetting
from app.db.session import get_session

_PREFIX = "media_usage:"
_held: ContextVar[tuple[int, object] | None] = ContextVar("media_usage_held", default=None)


@dataclass(frozen=True, slots=True)
class _Scope:
    thread: int
    engine: object
    exclusive: bool


_scope: ContextVar[_Scope | None] = ContextVar("media_reference_scope", default=None)


def _current_scope() -> _Scope | None:
    current = _scope.get()
    if current is not None and current.thread == get_ident() and current.engine is database.engine:
        return current
    return None


@contextmanager
def media_reference_scope() -> Iterator[None]:
    """在业务事务之前保护媒体引用；同线程嵌套复用，跨线程重新取得共享门。"""
    if _current_scope() is not None:
        yield
        return
    lock = ReadWriteLock(storage_root() / ".media-references.lock", is_singleton=False)
    with lock.read_lock():
        token = _scope.set(_Scope(get_ident(), database.engine, False))
        try:
            yield
        finally:
            _scope.reset(token)


@contextmanager
def media_cleanup_scope() -> Iterator[bool]:
    """非阻塞取得独占清理门，共享操作活动期间跳过本轮清理。"""
    if _current_scope() is not None:
        yield False
        return
    lock = ReadWriteLock(storage_root() / ".media-references.lock", is_singleton=False)
    try:
        lock.acquire_write(timeout=0)
    except Timeout:
        yield False
        return
    token = _scope.set(_Scope(get_ident(), database.engine, True))
    try:
        yield True
    finally:
        _scope.reset(token)
        lock.release()


def _lock(handle: BinaryIO) -> None:
    """立即取得文件的排他锁，已被其它操作持有时抛出 OSError。"""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def media_operation() -> Iterator[None]:
    """登记媒体使用到本次操作结束，嵌套调用复用同一锁。"""
    with media_reference_scope():
        yield from _registered_operation()


def _registered_operation() -> Iterator[None]:
    identity = (get_ident(), database.engine)
    if _held.get() == identity:
        yield
        return
    root = storage_root() / ".media_usage"
    root.mkdir(parents=True, exist_ok=True)
    key = _PREFIX + uuid.uuid4().hex
    path = root / f"{key.removeprefix(_PREFIX)}.lock"
    handle = path.open("x+b")
    token = _held.set(identity)
    try:
        handle.write(b"1")
        handle.flush()
        _lock(handle)
        with get_session() as db:
            db.add(AppSetting(key=key, value=str(path)))
        yield
    finally:
        _held.reset(token)
        handle.close()
        with get_session() as db:
            row = db.get(AppSetting, key)
            if row is not None:
                db.delete(row)
        path.unlink(missing_ok=True)


def media_in_use() -> bool:
    """在业务会话外探测旧媒体登记，短事务仅删除已确认过期的原登记。"""
    root = (storage_root() / ".media_usage").resolve()
    with get_session(operation="media_usage_snapshot") as db:
        rows = db.exec(select(AppSetting.key, AppSetting.value).where(AppSetting.key.startswith(_PREFIX))).all()
    stale: list[tuple[str, str]] = []
    for key, value in rows:
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            logger.warning("原片清理暂停：媒体使用登记路径不合法。")
            return True
        try:
            with path.open("r+b") as handle:
                _lock(handle)
        except FileNotFoundError:
            pass
        except OSError:
            return True
        path.unlink(missing_ok=True)
        stale.append((key, value))
    if stale:
        with get_session(operation="media_usage_expired") as db:
            for key, value in stale:
                row = db.get(AppSetting, key)
                if row is not None and row.value == value:
                    db.delete(row)
    return False
