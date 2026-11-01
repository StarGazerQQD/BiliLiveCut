"""存储路径管理。

集中定义并按需创建运行产物目录,避免各模块硬编码路径。
所有目录均位于 :data:`Settings.storage_root` 之下。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.config import settings


def resolve_storage_path(value: str | Path) -> Path:
    """解析存储引用；相对值限定于当前根，既有绝对值保留原位置。"""
    text = str(value)
    if not text or text == "." or "\x00" in text:
        raise ValueError("媒体路径不能为空")
    path = Path(text)
    if path.is_absolute():
        return path.resolve()
    if PureWindowsPath(text).drive or "\\" in text or ".." in PurePosixPath(text).parts:
        raise ValueError("相对媒体路径必须是存储根内的 POSIX 路径")
    root = Path(settings.storage_root).expanduser().resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("相对媒体路径不能越过存储根")
    return resolved


def to_storage_relative(value: str | Path) -> str:
    """为受管物理路径保存相对引用，保留显式外部输出的绝对位置。"""
    path = Path(value)
    root = Path(settings.storage_root).expanduser().resolve()
    physical = resolve_storage_path(value)
    if path.is_absolute() and path.is_relative_to(root) and not physical.is_relative_to(root):
        raise ValueError("媒体文件不能通过符号链接越过存储根")
    if physical.is_relative_to(root):
        relative = physical.relative_to(root)
        if relative == Path("."):
            raise ValueError("媒体路径不能是存储根本身")
        return relative.as_posix()
    return str(physical)


def storage_path_aliases(value: str | Path) -> tuple[str, ...]:
    """列出同一物理文件的当前规范引用，供幂等登记和引用保护使用。"""
    path = resolve_storage_path(value)
    return tuple(dict.fromkeys((str(value), str(path), path.as_posix(), to_storage_relative(path))))


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
    root = storage_root()
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError("存储目录不能通过链接越过存储根")
    path.mkdir(parents=True, exist_ok=True)
    if not path.resolve().is_relative_to(root):
        raise ValueError("存储目录不能通过链接越过存储根")
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


def _session_directory(root: Path, session_id: int | None) -> Path:
    if session_id is not None and (type(session_id) is not int or session_id <= 0):
        raise ValueError("场次 ID 必须为正整数")
    root = root.resolve()
    name = f"session_{session_id}" if session_id is not None else "unassigned"
    path = root / name
    if path.resolve() != path:
        raise ValueError("场次目录不能通过链接重定向")
    path.mkdir(parents=True, exist_ok=True)
    if path.resolve() != path:
        raise ValueError("场次目录不能通过链接重定向")
    return path


def session_clips_dir(session_id: int | None) -> Path:
    """返回受控的场次成品目录；无关联的历史产物使用 unassigned。"""
    return _session_directory(clips_dir(), session_id)


def session_upload_dir(session_id: int | None) -> Path:
    """返回场次专属待上传清单目录。"""
    return _session_directory(ready_to_upload_dir(), session_id)


def logs_dir() -> Path:
    """日志目录 ``storage/logs``。"""
    return _subdir("logs")


def session_raw_dir(session_id: int) -> Path:
    """返回某录制会话的原始片段子目录 ``storage/raw/session_<id>``。

    :param session_id: 录制会话主键。
    :returns: 会话专属目录的 :class:`~pathlib.Path`。
    """
    return _session_directory(raw_dir(), session_id)
