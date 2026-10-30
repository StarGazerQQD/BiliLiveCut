# ruff: noqa: D100
from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict


class WriterIdentity(BaseModel):
    """持久登记锁文件身份；路径被移除或替换后不能再证明旧 writer 退出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    device: int
    inode: int


def _open_windows(path: Path, *, create: bool) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    open_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    # 独占共享模式跟随文件对象的所有复制句柄，最后一个 writer 退出才可再次打开。
    handle = open_file(str(path), 0x80000000 | 0x40000000, 0, None, 1 if create else 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise


def _open(path: Path, *, create: bool) -> int:
    if os.name == "nt":
        return _open_windows(path, create=create)
    import fcntl

    descriptor = os.open(path, os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    try:
        if descriptor < 3:
            replacement = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(descriptor)
            descriptor = replacement
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class SessionWriter:
    """宿主和每个真实 FFmpeg 共同持有的 OS 写入占用，不依赖 PID 或 mtime。"""

    def __init__(self, descriptor: int, identity: WriterIdentity) -> None:
        self._descriptor: int | None = descriptor
        self.identity = identity

    @classmethod
    def create(cls, directory: Path) -> SessionWriter:
        """创建新场次独占证明文件，保留文件用于异常退出后的核验。"""
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / f".writer-{uuid4().hex}.lock").resolve()
        descriptor = _open(path, create=True)
        try:
            stat = os.fstat(descriptor)
            return cls(descriptor, WriterIdentity(path=str(path), device=stat.st_dev, inode=stat.st_ino))
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def recover(cls, identity: WriterIdentity) -> SessionWriter:
        """只打开原有证明文件，独占成功且身份相同才确认所有旧 writer 已退出。"""
        descriptor = _open(Path(identity.path), create=False)
        try:
            stat = os.fstat(descriptor)
            if (stat.st_dev, stat.st_ino) != (identity.device, identity.inode):
                raise ValueError("录制 writer 证明文件已被替换，不能自动清算")
            return cls(descriptor, identity)
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def spawn_options(self) -> Iterator[dict[str, Any]]:
        """仅向指定子进程继承私有副本，启动完成或失败后关闭宿主副本。"""
        if self._descriptor is None:
            raise RuntimeError("录制 writer 占用已关闭")
        duplicate = os.dup(self._descriptor)
        try:
            if os.name == "nt":
                import msvcrt

                handle = msvcrt.get_osfhandle(duplicate)
                os.set_handle_inheritable(handle, True)
                startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
                yield {"startupinfo": startup, "close_fds": True}
            else:
                import fcntl

                if duplicate < 3:
                    replacement = fcntl.fcntl(duplicate, fcntl.F_DUPFD_CLOEXEC, 3)
                    os.close(duplicate)
                    duplicate = replacement
                yield {"pass_fds": (duplicate,), "close_fds": True}
        finally:
            os.close(duplicate)

    def close(self) -> None:
        """仅关闭当前句柄，不显式 unlock，也不删除旧 writer 仍可能持有的文件。"""
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> SessionWriter:
        """保持写入或清算期间的独占占用。"""
        return self

    def __exit__(self, *_args: object) -> None:
        """释放当前持有者，继承副本继续保护实际 writer。"""
        self.close()
