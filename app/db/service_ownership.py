# ruff: noqa: D100
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from threading import Lock
from urllib.parse import unquote
from weakref import WeakKeyDictionary

from sqlalchemy.engine import Engine

from app.core.supervisor import Supervisor
from config.file_locks import FileLock

_memory_owners: WeakKeyDictionary[Engine, Lock] = WeakKeyDictionary()
_memory_guard = Lock()
_retained: set[ServiceOwnership] = set()


class ServiceOwnership:
    """在任何数据库恢复前取得根目录及数据库所有权，关闭未确认时保留锁。"""

    def __init__(self, root: Path, engine: Engine) -> None:
        self.root = root.resolve()
        self.supervisor: Supervisor | None = None
        self._locks = ExitStack()
        try:
            self._locks.enter_context(FileLock(self.root / ".service.lock").acquire(timeout=0))
            if engine.url.get_backend_name() != "sqlite":
                raise RuntimeError("应用执行所有权当前仅支持 SQLite")
            database = engine.url.database
            if str(engine.url.query.get("uri", "")).lower() in {"1", "true"}:
                if engine.url.query.get("mode") == "memory" or database == "file::memory:":
                    raise RuntimeError("应用执行锁不支持共享内存 URI，请使用普通 SQLite 文件数据库")
                if database and database.startswith("file:"):
                    database = unquote(database[5:])
            if not database or database == ":memory:":
                with _memory_guard:
                    lock = _memory_owners.setdefault(engine, Lock())
                if not lock.acquire(blocking=False):
                    raise RuntimeError("当前数据库已有应用实例，不能重复启动或恢复")
                self._locks.callback(lock.release)
            else:
                path = Path(str(Path(database).resolve()) + ".service.lock")
                self._locks.enter_context(FileLock(path).acquire(timeout=0))
        except BaseException:
            self._locks.close()
            raise
        # 关闭失败后仍须保留强引用，不能因生成器回收而间接释放 OS 锁。
        _retained.add(self)

    def release(self) -> None:
        """只有早期启动失败或全部服务确认关闭后才归还所有权。"""
        if self.supervisor is not None and not self.supervisor.shutdown_confirmed:
            return
        self._locks.close()
        _retained.discard(self)
