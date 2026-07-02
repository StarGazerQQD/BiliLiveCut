"""存储路径管理。

集中定义并按需创建运行产物目录,避免各模块硬编码路径。
所有目录均位于 :data:`Settings.storage_root` 之下。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings


def storage_root() -> Path:
    """返回存储根目录(绝对路径),不存在则创建。

    :returns: 存储根目录的 :class:`~pathlib.Path`。
    """
    root = Path(settings.storage_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _subdir(name: str) -> Path:
    """返回存储根下的子目录,不存在则创建。

    :param name: 子目录名称。
    :returns: 子目录的 :class:`~pathlib.Path`。
    """
    path = storage_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def raw_dir() -> Path:
    """原始录制片段目录 ``storage/raw``。"""
    return _subdir("raw")


def clips_dir() -> Path:
    """成品切片目录 ``storage/clips``。"""
    return _subdir("clips")


def ready_to_upload_dir() -> Path:
    """待上传产物目录 ``storage/ready_to_upload``。"""
    return _subdir("ready_to_upload")


def logs_dir() -> Path:
    """日志目录 ``storage/logs``。"""
    return _subdir("logs")


def session_raw_dir(session_id: int) -> Path:
    """返回某录制会话的原始片段子目录 ``storage/raw/session_<id>``。

    :param session_id: 录制会话主键。
    :returns: 会话专属目录的 :class:`~pathlib.Path`。
    """
    return _subdir(f"raw/session_{session_id}")
