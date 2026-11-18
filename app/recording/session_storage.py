# ruff: noqa: D100
from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlmodel import Session

from app.core.config import settings
from app.core.recording_storage import RecordingStorage
from app.db.entities import AppSetting, RecordingSession

OWNER_PREFIX = "session_storage_owner:v1:"
CURSOR_PREFIX = "session_danmaku_cursor:v1:"
LOCATION_PREFIX = "session_storage_location:v1:"
OWNER_FILE = ".blc-session-id"
SESSION_TREES = ("raw", "clips", "ready_to_upload")
JOURNAL_COLLISION = "弹幕补做记录已存在，必须先完成原场次恢复"


class JournalHeader(BaseModel):
    """旧版独立日志身份及新版场次身份共用的补做首行。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2] = 1
    identity: str
    session_id: int


def check_journal_header(stream: BinaryIO, session_id: int, owner: str | None) -> JournalHeader:
    """读取首行并核实版本、编号和已登记的场次身份。"""
    first = stream.readline(65536)
    if not first.endswith(b"\n"):
        raise ValueError("弹幕补做记录首行不完整")
    try:
        header = JournalHeader.model_validate_json(first)
    except ValidationError:
        raise ValueError("弹幕补做记录首行无效") from None
    if header.session_id != session_id or (
        (header.version == 2 or owner is not None) and (header.version != 2 or header.identity != owner)
    ):
        raise ValueError("弹幕补做记录与原场次的存储身份不符")
    return header


class ArchiveHeader(BaseModel):
    """只解析用于识别档案的首行字段。"""

    kind: Literal["session"]
    session_id: int
    received_at: datetime
    storage_identity: str | None = None
    platform: str
    source_id: str


def check_archive_header(
    stream: BinaryIO, session_id: int, owner: str | None, started_at: datetime | None = None
) -> ArchiveHeader:
    """在同一个文件描述符上验证档案归属。"""
    first = stream.readline(65536)
    if not first.endswith(b"\n"):
        raise ValueError("弹幕档案首行不完整")
    try:
        header = ArchiveHeader.model_validate_json(first)
    except ValidationError:
        raise ValueError("弹幕档案首行缺少有效场次身份") from None
    if header.session_id != session_id or header.storage_identity != owner:
        raise ValueError("弹幕档案与当前场次身份不一致")
    if (
        owner is None
        and started_at is not None
        and header.received_at.replace(tzinfo=header.received_at.tzinfo or UTC)
        < started_at.replace(tzinfo=started_at.tzinfo or UTC)
    ):
        raise ValueError("弹幕档案早于当前场次开始，请核对历史归属")
    return header


def session_identity(directory: Path) -> str | None:
    """读取随场次目录迁移的身份，不跟随重定向或创建文件。"""
    marker = directory / OWNER_FILE
    if marker.resolve() != marker:
        raise ValueError("场次存储身份路径不能重定向")
    try:
        with marker.open("rb") as stream:
            value = stream.read(34)
    except FileNotFoundError:
        return None
    if not re.fullmatch(rb"[0-9a-f]{32}\n", value):
        raise ValueError("场次存储身份文件损坏")
    return value[:32].decode("ascii")


def get_locations(db: Session, session_id: int) -> RecordingStorage:
    """未登记位置的历史数据始终从默认目录读取。"""
    row = db.get(AppSetting, f"{LOCATION_PREFIX}{session_id}")
    return RecordingStorage.model_validate_json(row.value) if row else RecordingStorage()


def managed_media_root(db: Session, session_id: int) -> Path:
    """按场次限定可导出、清理的原片；兼容旧库直接登记在 raw 下的文件。"""
    root = Path(settings.storage_root)
    locations = get_locations(db, session_id)
    owner = db.get(AppSetting, f"{OWNER_PREFIX}{session_id}")
    if owner is None:
        return RecordingStorage.base("raw", root)
    directory = locations.session_dir(session_id, root)
    if session_identity(directory) != owner.value:
        raise ValueError("原片存储目录身份与数据库不一致")
    allowed = locations.media_root(session_id, root)
    if allowed.resolve() != allowed:
        raise ValueError("原片目录不能重定向")
    return allowed


def bind_new_storage(db: Session, session: RecordingSession, locations: RecordingStorage | None = None) -> str:
    """在场次创建事务中独占新目录，并把同一身份写入数据库。"""
    db.add(session)
    db.flush()
    assert session.id is not None
    candidate = session.id
    root = Path(settings.storage_root).expanduser().resolve()
    locations = locations or RecordingStorage.configured()
    bases = list(dict.fromkeys(locations.base(ref, root) for ref in (locations.raw, locations.danmaku)))
    trees = list(dict.fromkeys([*bases, *(root / name for name in SESSION_TREES)]))
    if any(tree.resolve() != tree for tree in trees):
        raise ValueError("场次存储目录不能重定向")
    for base in bases:
        base.mkdir(parents=True, exist_ok=True)
    while True:
        name = f"session_{candidate}"
        if (
            not any(os.path.lexists(tree / name) for tree in trees)
            and db.get(AppSetting, f"{OWNER_PREFIX}{candidate}") is None
        ):
            try:
                for base in bases:
                    (base / name).mkdir()
            except FileExistsError:
                pass
            else:
                break
        candidate += 1
    # SQLite 分配首个编号时已取得写锁，外部副本中占用的编号只跳过，不重用目录。
    identity = uuid4().hex
    for base in bases:
        with (base / name / OWNER_FILE).open("xb") as stream:
            stream.write((identity + "\n").encode("ascii"))
    session.id = candidate
    db.add(AppSetting(key=f"{OWNER_PREFIX}{candidate}", value=identity))
    if locations != RecordingStorage():
        db.add(AppSetting(key=f"{LOCATION_PREFIX}{candidate}", value=locations.model_dump_json()))
    db.flush()
    return identity


def verify_storage(db: Session, session_id: int) -> str | None:
    """核对数据库与场次目录，拒绝读取已知冲突的历史档案。"""
    owner = db.get(AppSetting, f"{OWNER_PREFIX}{session_id}")
    row = db.get(RecordingSession, session_id) if owner is None else None
    return check_directory(
        session_id, owner.value if owner else None, row.error_message if row else None, get_locations(db, session_id)
    )


def check_directory(
    session_id: int, owner: str | None, error_message: str | None, locations: RecordingStorage
) -> str | None:
    """用已读取的数据库元数据核对场次目录。"""
    directory = locations.session_dir(session_id, Path(settings.storage_root), danmaku=True)
    if session_identity(directory) != owner:
        raise ValueError(f"场次 {session_id} 的数据库和存储目录身份不一致，请核对原数据库及素材副本")
    if owner is None and JOURNAL_COLLISION in (error_message or ""):
        raise ValueError(f"场次 {session_id} 已记录旧弹幕档案冲突，请核对原场次")
    return owner
