"""跨进程文件锁 — Runtime 和 Engine Pack 安装共用。"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Generator


class FileLock:
    """基于文件的跨进程排他锁。

    用法:
        lock = FileLock("/path/to/.lock")
        with lock.acquire(timeout=60):
            ...
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    @contextlib.contextmanager
    def acquire(self, timeout: float = 120.0) -> Generator[None, None, None]:
        """获取排他锁。

        :param timeout: 最长等待秒数 (0 表示不等待)。
        :yields: 成功获取锁后继续。
        :raises TimeoutError: 超时未获取锁。
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout

        while True:
            try:
                self._fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                try:
                    os.write(self._fd, str(os.getpid()).encode())
                except OSError:
                    pass
                break
            except FileExistsError:
                if timeout == 0 or time.time() >= deadline:
                    raise TimeoutError(f"无法获取锁: {self._lock_path} (超时 {timeout}s)") from None
                time.sleep(0.5)

        try:
            yield
        finally:
            self._release()

    def _release(self) -> None:
        """释放锁。"""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def get_runtime_lock_path(app_root: Path) -> Path:
    """获取 Runtime 安装锁路径。

    :param app_root: 应用根目录。
    :returns: 锁文件路径。
    """
    return app_root / "runtime" / ".runtime-install.lock"


def get_engine_pack_lock_path(app_root: Path) -> Path:
    """获取 Engine Pack 安装锁路径。

    :param app_root: 应用根目录。
    :returns: 锁文件路径。
    """
    return app_root / "runtime" / ".engine-pack-install.lock"
