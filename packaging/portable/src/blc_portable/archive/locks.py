"""跨进程文件锁 — Runtime 和 Engine Pack 安装共用。"""

from __future__ import annotations

from pathlib import Path

from config.file_locks import FileLock as FileLock


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
