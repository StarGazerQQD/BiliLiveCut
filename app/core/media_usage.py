"""用操作系统文件锁登记跨进程媒体使用，崩溃后锁自动释放。"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO

from loguru import logger
from sqlmodel import Session, select

from app.core.paths import storage_root
from app.db.entities import AppSetting
from app.db.session import get_session

_PREFIX = "media_usage:"
_held: ContextVar[bool] = ContextVar("media_usage_held", default=False)


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
    if _held.get():
        yield
        return
    root = storage_root() / ".media_usage"
    root.mkdir(parents=True, exist_ok=True)
    key = _PREFIX + uuid.uuid4().hex
    path = root / f"{key.removeprefix(_PREFIX)}.lock"
    handle = path.open("x+b")
    token = _held.set(True)
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


def media_in_use(db: Session) -> bool:
    """清理事务中检查活动锁；移除进程崩溃遗留的未占用登记。"""
    root = (storage_root() / ".media_usage").resolve()
    for row in db.exec(select(AppSetting).where(AppSetting.key.startswith(_PREFIX))).all():
        path = Path(row.value).resolve()
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
        db.delete(row)
        path.unlink(missing_ok=True)
    return False
