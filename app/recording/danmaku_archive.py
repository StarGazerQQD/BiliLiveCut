"""Append-only per-session archives, separate from sampled analysis records."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal

from loguru import logger
from sqlalchemy import String, cast
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.core.config import settings
from app.core.recording_storage import RecordingStorage
from app.core.sanitize import safe_exception_summary
from app.db.entities import AppSetting, RecordingSession
from app.plugins.live_source import SourceRoom
from app.recording.session_storage import (
    LOCATION_PREFIX,
    OWNER_PREFIX,
    check_archive_header,
    check_directory,
    get_locations,
    session_identity,
    verify_storage,
)


def archive_path(session_id: int, locations: RecordingStorage | None = None) -> Path:
    """Resolve the fixed archive path without creating directories or escaping storage."""
    if session_id <= 0:
        raise ValueError("场次编号必须为正整数")
    if locations is None:
        from app.db.session import get_session

        with get_session() as db:
            locations = get_locations(db, session_id)
    expected = locations.session_dir(session_id, Path(settings.storage_root), danmaku=True) / "danmaku.jsonl"
    path = expected.resolve()
    if path != expected:
        raise ValueError("弹幕档案路径不能重定向到其他位置")
    return path


def archive_view(session_id: int) -> dict[str, object]:
    """Report archive availability without creating files or exposing local paths."""
    try:
        path = archive_path(session_id)
        if not path.is_file():
            return {"available": False}
        with path.open("rb") as stream:
            _verify_archive(stream, session_id)
        return {"available": True}
    except (OSError, ValueError):
        return {"available": False}


def archive_views(db: Session, session_ids: tuple[int, ...]) -> dict[int, dict[str, object]]:
    """按页核对档案归属，页面内的场次共用一次数据库查询。"""
    result: dict[int, dict[str, object]] = {sid: {"available": False} for sid in session_ids}
    location = aliased(AppSetting)
    rows = db.exec(
        select(
            RecordingSession.id,
            RecordingSession.error_message,
            RecordingSession.started_at,
            AppSetting.value,
            location.value,
        )
        .outerjoin(AppSetting, AppSetting.key == OWNER_PREFIX + cast(RecordingSession.id, String))
        .outerjoin(location, location.key == LOCATION_PREFIX + cast(RecordingSession.id, String))
        .where(RecordingSession.id.in_(session_ids))
    ).all()
    for sid, error, started, owner, location_json in rows:
        try:
            locations = RecordingStorage.model_validate_json(location_json) if location_json else RecordingStorage()
            path = archive_path(sid, locations)
            if not path.is_file():
                continue
            check_directory(sid, owner, error, locations)
            with path.open("rb") as stream:
                check_archive_header(stream, sid, owner, started)
        except (ValueError, OSError):
            continue
        result[sid] = {"available": True}
    return result


class DanmakuArchive:
    """Flush complete JSONL records without changing database sampling semantics."""

    def __init__(
        self,
        session_id: int,
        room: SourceRoom,
        *,
        storage_identity: str | None = None,
        locations: RecordingStorage | None = None,
    ) -> None:
        self.session_id = session_id
        self._storage_identity = storage_identity
        self._locations = locations or RecordingStorage()
        self.room = room
        self.error: str | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        """Create an identifiable empty-session archive, preserving existing contents."""
        with self._lock:
            if self._started or self.error:
                return
            self._write([self._record("session", {})])
            self._started = True

    def append(self, payloads: list[dict[str, object]], *, event_format: Literal["raw", "normalized"]) -> None:
        """Persist received messages before sampling; file failure leaves capture running."""
        with self._lock:
            if self.error or not payloads:
                return
            records = [] if self._started else [self._record("session", {})]
            records.extend(self._record(event_format, payload) for payload in payloads)
            self._write(records)
            self._started = True

    def _record(self, kind: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "version": 1,
            **({"storage_identity": self._storage_identity} if kind == "session" and self._storage_identity else {}),
            "kind": kind,
            "session_id": self.session_id,
            "platform": self.room.platform,
            "source_id": self.room.source_id,
            "received_at": datetime.now(UTC).isoformat(),
            "payload": payload,
        }

    def _write(self, records: list[dict[str, object]]) -> None:
        try:
            path = archive_path(self.session_id, self._locations)
            if session_identity(path.parent) != self._storage_identity:
                raise ValueError("弹幕档案写入目录的场次身份不一致")
            path.parent.mkdir(parents=True, exist_ok=True)
            data = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records)
            # 每批关闭文件即刷新用户态缓存；完整旧行在中断后仍可独立读取。
            with path.open("a+b") as stream:
                if stream.tell():
                    stream.seek(0)
                    header = check_archive_header(stream, self.session_id, self._storage_identity)
                    if (header.platform, header.source_id) != (self.room.platform, self.room.source_id):
                        raise ValueError("弹幕档案直播源不一致")
                    stream.seek(0, 2)
                elif self._started:
                    raise ValueError("已开始写入的弹幕档案被截断")
                stream.write(data.encode("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            self.error = f"弹幕原始档写入失败（{safe_exception_summary(exc)}），本场已停止写档；数据库采集继续。"
            # 原始消息可能含用户数据及签名链接，不让 diagnose 展开局部变量。
            logger.error(
                "danmaku_archive_failed: session={} raw/session_{}/danmaku.jsonl error={}",
                self.session_id,
                self.session_id,
                safe_exception_summary(exc),
            )


def _verify_archive(stream: BinaryIO, session_id: int) -> None:
    from app.db.session import get_session

    with get_session() as db:
        owner = verify_storage(db, session_id)
        row = db.get(RecordingSession, session_id) if owner is None else None
    check_archive_header(stream, session_id, owner, row.started_at if row else None)


def open_archive_snapshot(session_id: int) -> tuple[BinaryIO, int]:
    """Open a bounded snapshot ending at a complete JSONL line during live append."""
    path = archive_path(session_id)
    if not path.parent.is_dir():
        from app.db.session import get_session

        with get_session() as db:
            if db.get(AppSetting, f"{OWNER_PREFIX}{session_id}") is not None:
                raise OSError("已登记的弹幕存储位置不可用")
    stream = path.open("rb")
    try:
        _verify_archive(stream, session_id)
        stream.seek(0, 2)
        boundary = stream.tell()
        while boundary:
            start = max(0, boundary - 65536)
            stream.seek(start)
            tail = stream.read(boundary - start)
            newline = tail.rfind(b"\n")
            if newline >= 0:
                boundary = start + newline + 1
                break
            boundary = start
        stream.seek(0)
        return stream, boundary
    except (OSError, ValueError):
        stream.close()
        raise
