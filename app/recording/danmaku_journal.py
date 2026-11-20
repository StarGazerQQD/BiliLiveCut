# ruff: noqa: D100
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlmodel import Session

from app.core.recording_storage import RecordingStorage
from app.db.entities import AppSetting, Danmaku, LiveRoom, RecordingSession
from app.db.session import get_session
from app.recording.danmaku_archive import archive_path
from app.recording.danmaku_evidence import DanmakuEvidence, freeze_evidence, read_evidence, save_evidence
from app.recording.session_storage import (
    CURSOR_PREFIX,
    JOURNAL_COLLISION,
    JournalHeader,
    check_archive_header,
    check_journal_header,
    get_locations,
    session_identity,
    verify_storage,
)
from app.sources.rooms import room_source

_MAX_LINE_BYTES = 64 * 1024 * 1024
_BATCH_BYTES = 1024 * 1024


class JournalWriteError(OSError):
    """本地补做文件写入不确定，禁止作为上游网络故障重连。"""


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["message"] = "message"
    room_id: int
    ts: datetime
    msg_type: str
    user: str | None
    content: str | None
    value: float = Field(allow_inf_nan=False)


class _Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["observation"] = "observation"
    evidence: DanmakuEvidence


_Record = Annotated[_Message | _Observation, Field(discriminator="kind")]
_record_adapter = TypeAdapter(_Record)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    identity: str
    offset: int = Field(ge=0)
    closed: bool = False
    unconfirmed_tail_bytes: int = Field(default=0, ge=0)
    unconfirmed_tail_sha256: str | None = None


def journal_path(session_id: int, locations: RecordingStorage | None = None) -> Path:
    """独立保存已决定入库的数据，不改变原始弹幕档案协议。"""
    expected = archive_path(session_id, locations).with_name("danmaku-pending.jsonl")
    if expected.resolve() != expected:
        raise ValueError("弹幕补做记录路径不能重定向")
    return expected


def _cursor(db: Session, session_id: int) -> _Cursor | None:
    row = db.get(AppSetting, f"{CURSOR_PREFIX}{session_id}")
    return _Cursor.model_validate_json(row.value) if row is not None else None


def journal_closed(db: Session, session_id: int) -> bool:
    """已有补做记录必须消费到终态；旧场次仍使用原有收尾协议。"""
    cursor = _cursor(db, session_id)
    return cursor is None or cursor.closed


class DanmakuJournal:
    """场次内追加选中行和观测；游标与实际消息同事务提交，重放不重新采样。"""

    def __init__(
        self, session_id: int, *, storage_identity: str | None = None, locations: RecordingStorage | None = None
    ) -> None:
        self.session_id = session_id
        self._storage_identity = storage_identity
        self._locations = locations or RecordingStorage()
        self._lock = Lock()
        self._header: JournalHeader | None = None
        self._failure: OSError | ValueError | None = None

    def _append(self, records: list[_Message | _Observation]) -> None:
        if not records:
            return
        with self._lock:
            if self._failure is not None:
                raise JournalWriteError("弹幕补做写入曾失败，已停止追加并保留原文件供恢复") from self._failure
            try:
                if self._locations.source_session_id is not None:
                    raise ValueError("历史引用场次的补做记录只读")
                path = journal_path(self.session_id, self._locations)
                if session_identity(path.parent) != self._storage_identity:
                    raise ValueError("弹幕补做写入目录的场次身份不一致")
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._header is None:
                    if path.exists():
                        raise RuntimeError(JOURNAL_COLLISION)
                    header = JournalHeader(
                        version=2 if self._storage_identity else 1,
                        identity=self._storage_identity or uuid4().hex,
                        session_id=self.session_id,
                    )
                    with path.open("xb") as stream:
                        stream.write((header.model_dump_json() + "\n").encode())
                    self._header = header
                # 写入或关闭失败后禁止再追加，避免污染未确认的半行尾部。
                with path.open("a+b") as stream:
                    stream.seek(0)
                    if check_journal_header(stream, self.session_id, self._storage_identity) != self._header:
                        raise ValueError("已打开的弹幕补做记录被替换")
                    stream.seek(0, 2)
                    for record in records:
                        data = (record.model_dump_json() + "\n").encode()
                        if len(data) > _MAX_LINE_BYTES:
                            raise ValueError("弹幕补做记录超过单条安全大小")
                        stream.write(data)
            except (OSError, ValueError) as exc:
                self._failure = exc
                raise JournalWriteError("弹幕补做写入失败，已停止追加并保留原文件供恢复") from exc

    def append_messages(self, rows: list[Danmaku]) -> None:
        """保存原接收时间、平台单位及首次采样结果，不按消息内容去重。"""
        self._append(
            [
                _Message(
                    room_id=row.room_id,
                    ts=row.ts,
                    msg_type=row.msg_type,
                    user=row.user,
                    content=row.content,
                    value=row.value,
                )
                for row in rows
            ]
        )

    def append_observation(self, evidence: DanmakuEvidence) -> None:
        """调用者在接收门内生成快照，闭合区间和覆盖水位按相同顺序保存。"""
        self._append([_Observation(evidence=evidence)])


def drain_journal(session_id: int, *, limit: int = 100, close: bool = False) -> bool:
    """消费一批；文件解析在事务外，并发消费者通过事务内游标复核避免重复。"""
    with get_session() as db:
        locations = get_locations(db, session_id)
    if locations.source_session_id is not None:
        raise ValueError("历史引用场次不能重放在线补做任务")
    path = journal_path(session_id, locations)
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        with get_session() as db:
            verify_storage(db, session_id)
            if _cursor(db, session_id) is not None:
                raise ValueError("已登记的弹幕补做记录缺失") from None
        return True
    with stream:
        with get_session() as db:
            owner = verify_storage(db, session_id)
            header = check_journal_header(stream, session_id, owner)
            previous = _cursor(db, session_id)
            legacy = db.get(RecordingSession, session_id) if owner is None else None
            room = db.get(LiveRoom, legacy.room_id) if legacy else None
        verified_external_source = previous is not None and room is not None and room.platform != "bilibili"
        if legacy is not None and previous is None:
            try:
                archived = archive_path(session_id).open("rb")
            except FileNotFoundError:
                pass
            else:
                with archived:
                    source = check_archive_header(archived, session_id, None, legacy.started_at)
                    if room and (
                        source.platform != room.platform
                        or (
                            room.platform == "bilibili"
                            and room.room_id is not None
                            and source.source_id != str(room.room_id)
                        )
                    ):
                        raise ValueError("旧弹幕档案与数据库的直播来源不一致")
                    if room and room.platform != "bilibili":
                        if source.source_id != room_source(room).source_id:
                            raise ValueError("旧弹幕档案与数据库的直播来源不一致")
                        verified_external_source = True
        if previous is not None and previous.identity != header.identity:
            raise ValueError("弹幕补做记录已被替换")
        offset = previous.offset if previous is not None else stream.tell()
        stream.seek(0, 2)
        boundary = stream.tell()
        if offset > boundary:
            raise ValueError("弹幕补做记录已被截断")
        stream.seek(offset)
        if previous is not None and previous.unconfirmed_tail_bytes:
            tail = stream.read(previous.unconfirmed_tail_bytes + 1)
            if (
                len(tail) != previous.unconfirmed_tail_bytes
                or hashlib.sha256(tail).hexdigest() != previous.unconfirmed_tail_sha256
            ):
                raise ValueError("已隔离的弹幕不完整尾行发生变化，原记录需检查")
            return True
        records: list[_Message | _Observation] = []
        unconfirmed_tail: bytes | None = None
        size = 0
        messages = 0
        while stream.tell() < boundary and messages < max(1, limit) and size < _BATCH_BYTES:
            line = stream.readline(min(_MAX_LINE_BYTES + 1, boundary - stream.tell()))
            if len(line) > _MAX_LINE_BYTES:
                raise ValueError("弹幕补做记录超过单条安全大小，原文件已保留")
            if not line.endswith(b"\n"):
                if close:
                    unconfirmed_tail = line
                stream.seek(-len(line), 1)
                break
            record = _record_adapter.validate_json(line)
            if legacy is not None:
                if isinstance(record, _Message):
                    expected_room = room.room_id if room and room.platform == "bilibili" else legacy.room_id
                    if room and expected_room is not None and record.room_id != expected_room:
                        raise ValueError("旧弹幕补做记录的房间与原场次不符")
                    # 外部源保存的是平台事件时间，缓冲消息可能早于本地开始接收。
                    times = [] if verified_external_source else [record.ts]
                else:
                    evidence = record.evidence
                    times = [item.start for item in evidence.intervals]
                    times.extend(t for t in (evidence.confirmed_until, evidence.ended_at) if t is not None)
                start = legacy.started_at.replace(tzinfo=legacy.started_at.tzinfo or UTC)
                if any(t.replace(tzinfo=t.tzinfo or UTC) < start for t in times):
                    raise ValueError("旧弹幕补做记录早于当前场次开始")
            records.append(record)
            messages += isinstance(record, _Message)
            size += len(line)
        end = stream.tell()
    finished = end == boundary or unconfirmed_tail is not None
    if not records and not close:
        # 活跃写入的未完整尾行尚不可消费，等下一次追加或定时检查，避免忙轮询。
        return True
    with get_session(operation="danmaku_replay") as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if verify_storage(db, session_id) != owner:
            raise ValueError("弹幕补做期间场次存储身份已改变")
        current = _cursor(db, session_id)
        if current != previous:
            return False
        closed = previous.closed if previous is not None else False
        for record in records:
            if closed:
                raise ValueError("弹幕补做终态后出现新的记录")
            if isinstance(record, _Message):
                db.add(Danmaku(session_id=session_id, **record.model_dump(exclude={"kind"})))
            else:
                save_evidence(db, session_id, record.evidence)
                closed = record.evidence.ended_at is not None
        db.flush()
        if close and finished and not closed:
            freeze_evidence(db, session_id)
            closed = True
        if unconfirmed_tail is not None:
            evidence = read_evidence(db, session_id) or DanmakuEvidence(
                status="failed", ended_at=datetime.now(UTC), interrupted=True
            )
            evidence.archive_error = "弹幕补做末行写入中断，完整前缀已恢复；不完整原始字节已保留，不能确认该行消息。"
            evidence.interrupted = True
            save_evidence(db, session_id, evidence)
        key = f"{CURSOR_PREFIX}{session_id}"
        row = db.get(AppSetting, key) or AppSetting(key=key)
        row.value = _Cursor(
            identity=header.identity,
            offset=end,
            closed=closed,
            unconfirmed_tail_bytes=len(unconfirmed_tail) if unconfirmed_tail is not None else 0,
            unconfirmed_tail_sha256=hashlib.sha256(unconfirmed_tail).hexdigest()
            if unconfirmed_tail is not None
            else None,
        ).model_dump_json()
        row.updated_at = datetime.now(UTC)
        db.add(row)
    if unconfirmed_tail is not None:
        logger.error(
            "弹幕补做尾行写入中断 session={} unconfirmed_bytes={}；完整前缀已恢复，原文件已保留",
            session_id,
            len(unconfirmed_tail),
        )
    return finished
