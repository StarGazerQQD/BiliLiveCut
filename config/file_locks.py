# ruff: noqa: D100
from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path


class FileLock:
    """Launcher 与应用共用的操作系统排他锁，退出时只释放句柄。"""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    @contextmanager
    def acquire(self, timeout: float = 120.0) -> Iterator[None]:
        """获取永久路径上的锁；timeout 为零时立即报告已有持有者。"""
        if self._fd is not None:
            raise RuntimeError("FileLock 不支持重入")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, timeout)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            lock = partial(msvcrt.locking, fd, msvcrt.LK_NBLCK, 1)
            unlock = partial(msvcrt.locking, fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            lock = partial(fcntl.flock, fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = partial(fcntl.flock, fd, fcntl.LOCK_UN)
        acquired = False
        try:
            # Windows 可锁定 EOF 之后的字节，无须改写其它持有者的锁文件。
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    lock()
                    acquired = True
                    self._fd = fd
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"无法获取锁: {self._lock_path} (超时 {timeout}s)") from exc
                    time.sleep(min(0.1, remaining))
            yield
        finally:
            try:
                if acquired:
                    os.lseek(fd, 0, os.SEEK_SET)
                    unlock()
            finally:
                os.close(fd)
                self._fd = None
            # 保留路径，避免等待者与新进程分别锁定不同 inode。
