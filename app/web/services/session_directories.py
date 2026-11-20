# ruff: noqa: D100
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlmodel import select

from app.core.config import settings
from app.core.osutil import open_path
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession
from app.db.session import get_session
from app.recording.session_storage import OWNER_PREFIX, check_directory, get_locations, session_identity

DirectoryKind = Literal["raw", "danmaku", "legacy_raw"]


class SessionDirectory(BaseModel):
    """场次已登记的目录位置及其当前可用性。"""

    kind: DirectoryKind
    label: str
    path: str
    available: bool


class SessionDirectories(BaseModel):
    """多个存储位置可分别选择，无桌面时仍提供路径。"""

    directories: list[SessionDirectory]
    selected: DirectoryKind | None = None
    opened: bool = False


def session_directories(session_id: int, kind: DirectoryKind | None = None) -> SessionDirectories:
    """解析场次的物理目录并按请求在服务主机打开。"""
    root = Path(settings.storage_root).expanduser().resolve()
    locations: list[tuple[DirectoryKind, str, Path]] = []
    with get_session() as db:
        session = db.get(RecordingSession, session_id)
        if session is None:
            raise LookupError("场次不存在")
        layout = get_locations(db, session_id)
        owner = db.get(AppSetting, f"{OWNER_PREFIX}{session_id}")
        raw = layout.session_dir(session_id, root)
        chat = layout.session_dir(session_id, root, danmaku=True)
        local = db.get(AppSetting, f"local_source:{session_id}")
        record = json.loads(local.value) if local else {}
        room = db.get(LiveRoom, session.room_id)
        import_id = layout.import_id or (record.get("import_id") if room and room.platform == "local" else None)
        if owner or not import_id:
            identity = owner.value if owner else None
            if owner is None or chat.is_dir():
                check_directory(session_id, identity, session.error_message, layout)
            if raw != chat and raw.is_dir() and session_identity(raw) != identity:
                raise ValueError("场次原片目录身份与登记不符，请核对存储副本")
        if import_id:
            locations.append(("raw", "原片", layout.import_dir(import_id, root) / "segments"))
            imported_chat = layout.import_dir(import_id, root, danmaku=True)
            if record.get("has_comments") or (
                not local and any(item.is_file() for item in imported_chat.glob("comments.*"))
            ):
                locations.append(("danmaku", "弹幕", imported_chat))
        else:
            raw_base = layout.base(layout.raw, root)
            flat = False
            if owner is None:
                references = db.exec(select(RawSegment.file_path).where(RawSegment.session_id == session_id))
                flat = any((root / reference).parent == raw_base for reference in references)
            if raw.is_dir() or not flat:
                locations.append(("raw", "原片", raw))
            if flat:
                locations.append(("legacy_raw", "旧版原片（共享目录）", raw_base))
            if chat != raw or chat.is_dir() or not flat:
                locations.append(("danmaku", "弹幕", chat))
    directories: dict[Path, SessionDirectory] = {}
    selection = None
    for key, label, path in locations:
        if path.resolve() != path:
            raise ValueError("场次存储目录不能经过链接")
        if path not in directories:
            directories[path] = SessionDirectory(kind=key, label=label, path=str(path), available=path.is_dir())
        else:
            directories[path].label += "、" + label
        if kind == key:
            selection = directories[path]
    if kind is not None and selection is None:
        raise LookupError("场次没有登记该类目录")
    available = [directory for directory in directories.values() if directory.available]
    if selection is None and len(available) == 1:
        selection = available[0]
    result = SessionDirectories(directories=list(directories.values()))
    if selection:
        result.selected = selection.kind
        if selection.available:
            result.opened = open_path(selection.path, existing_directory=True)
    return result
