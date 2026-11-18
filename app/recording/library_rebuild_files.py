# ruff: noqa: D100
from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel
from sqlmodel import SQLModel

from app.core.recording_storage import RecordingStorage
from app.db.entities import Danmaku, LiveRoom, RawSegment, RecordingSession
from app.plugins.live_source import DanmakuEvent, SourceRoom
from app.recording.danmaku_journal import _MAX_LINE_BYTES, _Message, _record_adapter
from app.recording.library_rebuild_rows import _setting, put_setting
from app.recording.session_storage import (
    LOCATION_PREFIX,
    OWNER_PREFIX,
    ArchiveHeader,
    check_archive_header,
    check_journal_header,
    session_identity,
)
from app.sources.bilibili.danmaku import parse_message
from app.sources.rooms import SourceBinding, _identity_key

MEDIA_SUFFIXES = {".ts", ".flv", ".mp4", ".mkv", ".m4s", ".webm", ".mov"}


def _insert(db: sqlite3.Connection, model: SQLModel) -> int:
    values = model.model_dump(exclude={"id"})
    parameters = [
        v.astimezone(UTC).replace(tzinfo=None).isoformat(" ")
        if isinstance(v, datetime) and v.tzinfo
        else v.isoformat(" ")
        if isinstance(v, datetime)
        else v
        for v in values.values()
    ]
    cursor = db.execute(
        f"INSERT INTO {model.__tablename__}({','.join(values)}) VALUES({','.join('?' for _ in values)})", parameters
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _regular(path: Path) -> Path:
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"历史来源文件缺失或重定向：{path.name}")
    return path


def _records(stream: BinaryIO) -> Iterator[bytes]:
    while line := stream.readline(_MAX_LINE_BYTES + 1):
        if len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise ValueError("弹幕记录超长或末行不完整，请核对离线副本")
        yield line


class _ArchiveEvent(BaseModel):
    session_id: int
    platform: str
    source_id: str
    received_at: datetime
    kind: str
    payload: dict[str, object]


def _events(
    archive: Path,
    journal: Path,
    header: ArchiveHeader | None,
    physical_id: int,
    owner: str | None,
    session_id: int,
    room_id: int,
    platform: str,
) -> Iterator[Danmaku]:
    if journal.is_file():
        with _regular(journal).open("rb") as stream:
            check_journal_header(stream, physical_id, owner)
            for line in _records(stream):
                item = _record_adapter.validate_json(line)
                if isinstance(item, _Message):
                    if platform == "bilibili" and header and str(item.room_id) != header.source_id:
                        raise ValueError("补做弹幕的房间号与原档不一致")
                    yield Danmaku(
                        session_id=session_id,
                        room_id=item.room_id if platform == "bilibili" else room_id,
                        **item.model_dump(exclude={"kind", "room_id"}),
                    )
        return
    if header:
        with _regular(archive).open("rb") as stream:
            check_archive_header(stream, physical_id, owner)
            for line in _records(stream):
                event = _ArchiveEvent.model_validate_json(line)
                if (event.session_id, event.platform, event.source_id) != (
                    physical_id,
                    header.platform,
                    header.source_id,
                ):
                    raise ValueError("弹幕原档混有不同场次或房间的消息")
                if event.kind == "raw" and platform == "bilibili":
                    parsed = parse_message(event.payload)
                    if parsed:
                        kind, user, content, value = parsed
                        yield Danmaku(
                            session_id=session_id,
                            room_id=int(header.source_id),
                            ts=event.received_at,
                            msg_type=kind,
                            user=user,
                            content=content,
                            value=value,
                        )
                elif event.kind == "normalized":
                    message = DanmakuEvent.model_validate(event.payload)
                    yield Danmaku(
                        session_id=session_id,
                        room_id=room_id,
                        ts=message.occurred_at,
                        user=message.user_name,
                        content=message.content,
                    )
                else:
                    raise ValueError("原档含未支持的弹幕记录格式")


def _media(directory: Path, missing: set[Path]) -> list[tuple[Path, float | None]]:
    if not directory.is_dir():
        return []
    durations: dict[str, float] = {}
    for listing in sorted(directory.glob("*segments.csv")):
        with _regular(listing).open(encoding="utf-8-sig", newline="") as stream:
            for entry in csv.reader(stream):
                if len(entry) != 3:
                    raise ValueError(f"分段清单格式无效：{listing.name}")
                name, start, end = entry
                duration = float(end) - float(start)
                if not math.isfinite(duration) or duration <= 0 or Path(name).name != name:
                    raise ValueError(f"分段清单含无效文件名或时长：{listing.name}")
                durations[name] = duration
                if not (directory / name).is_file():
                    missing.add(directory / name)
    return [
        (_regular(path), durations.get(path.name))
        for path in sorted(directory.iterdir())
        if path.suffix.lower() in MEDIA_SUFFIXES
    ]


def _archive_room(db: sqlite3.Connection, header: ArchiveHeader | None) -> int:
    if header and header.platform == "bilibili":
        if not re.fullmatch(r"[1-9][0-9]*", header.source_id):
            raise ValueError("弹幕档案中的 Bilibili 房间号无效")
        source = SourceRoom(
            platform="bilibili",
            source_id=header.source_id,
            canonical_url=f"https://live.bilibili.com/{header.source_id}",
        )
        previous = _setting(db, _identity_key(source))
        if previous:
            return SourceBinding.model_validate_json(previous).room_db_id
        room = _insert(
            db, LiveRoom(platform=source.platform, input_url=source.canonical_url, room_id=int(source.source_id))
        )
        binding = SourceBinding(room_db_id=room, room=source).model_dump_json()
        put_setting(db, _identity_key(source), binding)
        put_setting(db, f"source_room:{room}", binding)
        return room
    return _insert(
        db, LiveRoom(platform=header.platform if header else "unknown", input_url=header.source_id if header else "")
    )


def _index_detached_imports(
    db: sqlite3.Connection,
    root: Path,
    raw: Path,
    chat: Path,
    check_cancelled: Callable[[], None] | None,
    missing: set[Path],
) -> int:
    imports = raw / "imports"
    if not imports.is_dir():
        return 0
    if imports.resolve() != imports:
        raise ValueError("本地导入目录不能重定向")
    found = 0
    for directory in sorted(imports.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{32}", directory.name):
            continue
        layout = RecordingStorage(raw=str(raw), danmaku=str(chat), import_id=directory.name, detached_import=True)
        segments = layout.media_root(1, root)
        if segments.resolve() != segments:
            raise ValueError("本地导入分段目录不能重定向")
        media = _media(segments, missing)
        if not media:
            continue
        if check_cancelled:
            check_cancelled()
        room = _insert(db, LiveRoom(platform="local", input_url=f"import:{directory.name}"))
        started = datetime.fromtimestamp(min(path.stat().st_mtime for path, _ in media), UTC)
        sid = _insert(db, RecordingSession(room_id=room, started_at=started, status="archived"))
        put_setting(
            db, f"{LOCATION_PREFIX}{sid}", layout.model_copy(update={"source_session_id": sid}).model_dump_json()
        )
        put_setting(
            db,
            f"library_rebuild:session:{sid}",
            json.dumps({"import_id": directory.name, "session_id": None, "time_source": "file_mtime"}),
        )
        for seq, (path, duration) in enumerate(media):
            _insert(
                db,
                RawSegment(
                    session_id=sid,
                    seq=seq,
                    file_path=str(path),
                    duration_s=duration,
                    size_bytes=path.stat().st_size,
                    status="archived",
                ),
            )
        found += 1
    return found


def index_files(
    db: sqlite3.Connection, root: Path, raw: Path, chat: Path, check_cancelled: Callable[[], None] | None
) -> dict[str, int]:
    """通过剩余的场次目录重建可核查的媒体、弹幕索引，保留原始文件。"""
    ids = set()
    for base in {raw, chat}:
        if base.is_dir():
            for path in base.iterdir():
                if match := re.fullmatch(r"session_([1-9][0-9]*)", path.name):
                    if path.resolve() != path or not path.is_dir():
                        raise ValueError(f"场次目录无效：{path.name}")
                    ids.add(int(match[1]))
    missing: set[Path] = set()
    found = _index_detached_imports(db, root, raw, chat, check_cancelled, missing)
    for physical in sorted(ids):
        if check_cancelled:
            check_cancelled()
        layout = RecordingStorage(raw=str(raw), danmaku=str(chat), source_session_id=physical)
        raw_session = layout.session_dir(physical, root)
        chat_session = layout.session_dir(physical, root, danmaku=True)
        owners = {session_identity(path) for path in {raw_session, chat_session} if path.is_dir()}
        if len(owners) != 1:
            raise ValueError(f"场次 {physical} 的原片和弹幕目录身份冲突")
        owner = owners.pop()
        archive = chat_session / "danmaku.jsonl"
        journal = chat_session / "danmaku-pending.jsonl"
        header = None
        if archive.exists():
            with _regular(archive).open("rb") as stream:
                header = check_archive_header(stream, physical, owner)
        media = _media(raw_session, missing)
        if not media and not header and not journal.is_file():
            continue
        found += 1
        platform = header.platform if header else "unknown"
        room = _archive_room(db, header)
        files = [path for path, _ in media] + ([journal] if journal.is_file() else [])
        started = (
            header.received_at if header else datetime.fromtimestamp(min(path.stat().st_mtime for path in files), UTC)
        )
        sid = _insert(db, RecordingSession(room_id=room, started_at=started, status="archived"))
        put_setting(db, f"{LOCATION_PREFIX}{sid}", layout.model_dump_json())
        if owner:
            put_setting(db, f"{OWNER_PREFIX}{sid}", owner)
        put_setting(
            db,
            f"library_rebuild:session:{sid}",
            json.dumps(
                {
                    "session_id": physical,
                    "raw": str(raw),
                    "danmaku": str(chat),
                    "time_source": "archive_first_received" if header else "file_mtime",
                    "message_source": "journal" if journal.is_file() else "archive" if header else None,
                }
            ),
        )
        for seq, (path, duration) in enumerate(media):
            _insert(
                db,
                RawSegment(
                    session_id=sid,
                    seq=seq,
                    file_path=str(path),
                    duration_s=duration,
                    size_bytes=path.stat().st_size,
                    status="archived",
                ),
            )
        for index, message in enumerate(_events(archive, journal, header, physical, owner, sid, room, platform)):
            if check_cancelled and index % 1024 == 0:
                check_cancelled()
            _insert(db, message)
    if not found:
        raise ValueError("场次目录中没有可引用的媒体或弹幕文件")
    return {"file_sessions": found, "missing_files": len(missing)}
