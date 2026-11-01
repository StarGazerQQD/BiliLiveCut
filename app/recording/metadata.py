"""统一刷新房间资料，并保存每场直播的标题观测事实。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar
from weakref import WeakKeyDictionary

from loguru import logger
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.sanitize import safe_exception_summary
from app.db.entities import AppSetting, LiveRoom, RecordingSession, SessionStatus
from app.db.session import get_session
from app.plugins.live_source import SourceError, SourceRoom
from app.sources.registry import source_registry
from app.sources.rooms import room_source


class RoomMetadata(BaseModel):
    """资料抓取状态；普通房间更新时间不能冒充标题成功查询时间。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    attempted_at: AwareDatetime | None = None
    observed_at: AwareDatetime | None = None
    error: str | None = None
    source: SourceRoom | None = None
    source_revision: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    next_retry_at: AwareDatetime | None = None
    failure_kind: str | None = None


class SessionMetadata(BaseModel):
    """开录缓存、最后可信观测及冻结时刻；缺失证据时不伪造开录标题。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    start_title: str | None = None
    start_state: Literal["fresh", "stale", "unavailable"] = "unavailable"
    last_title: str | None = None
    observed_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    change_count: int = Field(default=0, ge=0)


class TitleObservation(BaseModel):
    """本系统观测到的标题变化，不代表平台修改的精确时刻。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    title: str = Field(min_length=1)
    observed_at: AwareDatetime


_Model = TypeVar("_Model", bound=BaseModel)
_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = WeakKeyDictionary()


def read_metadata(db: Session, key: str, model: type[_Model]) -> _Model | None:
    """读取严格校验的版本化元数据；损坏记录显示未知并保留原文件。"""
    row = db.get(AppSetting, key)
    if row is None:
        return None
    try:
        return model.model_validate_json(row.value)
    except ValidationError:
        logger.warning("元数据结构无效 key={}", key)
        return None


def _save(db: Session, key: str, value: BaseModel) -> None:
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = value.model_dump_json()
    row.updated_at = datetime.now(UTC)
    db.add(row)


def metadata_state(metadata: RoomMetadata, title: str | None) -> str:
    """按最近成功观测及失败状态显示新鲜、缓存或未知。"""
    if not title:
        return "unavailable"
    if metadata.error or metadata.observed_at is None:
        return "stale"
    age = (datetime.now(UTC) - metadata.observed_at).total_seconds()
    return "fresh" if age <= settings.room_metadata_refresh_interval_s * 2 else "stale"


def room_metadata_view(db: Session, room: LiveRoom) -> dict[str, object]:
    """返回不包含凭据的房间资料查询状态。"""
    metadata = read_metadata(db, f"room_metadata:{room.id}", RoomMetadata) or RoomMetadata()
    return {**metadata.model_dump(mode="json"), "state": metadata_state(metadata, room.title)}


def begin_session_metadata(session_id: int) -> None:
    """只创建一次开录快照；恢复及断流重连不覆盖既有记录。"""
    with get_session() as db:
        key = f"session_metadata:{session_id}"
        if db.get(AppSetting, key) is not None:
            return
        session = db.get(RecordingSession, session_id)
        room = db.get(LiveRoom, session.room_id) if session else None
        if room is None:
            return
        metadata = read_metadata(db, f"room_metadata:{room.id}", RoomMetadata) or RoomMetadata()
        _save(
            db,
            key,
            SessionMetadata(
                start_title=room.title,
                last_title=room.title,
                start_state=metadata_state(metadata, room.title),
                observed_at=metadata.observed_at,
            ),
        )


def end_session_metadata(session_id: int) -> None:
    """冻结已存在的场次快照，禁止以后改名改写历史。"""
    with get_session() as db:
        key = f"session_metadata:{session_id}"
        metadata = read_metadata(db, key, SessionMetadata)
        if metadata and metadata.ended_at is None:
            metadata.ended_at = datetime.now(UTC)
            _save(db, key, metadata)


def session_title_at(db: Session, session_id: int, observed_at: datetime | None = None) -> str | None:
    """返回场次最后标题，或给定素材时刻之前已观测到的标题。"""
    metadata = read_metadata(db, f"session_metadata:{session_id}", SessionMetadata)
    if metadata is None:
        return None
    if observed_at is None:
        return metadata.last_title
    instant = observed_at.replace(tzinfo=UTC) if observed_at.tzinfo is None else observed_at
    title = metadata.start_title
    rows = db.exec(
        select(AppSetting)
        .where(AppSetting.key.startswith(f"session_title_change:{session_id}:"))
        .order_by(AppSetting.key)
    ).all()
    for row in rows:
        observation = read_metadata(db, row.key, TitleObservation)
        if observation and observation.observed_at <= instant:
            title = observation.title
    return title


def _source_revision(db: Session, room: LiveRoom) -> str:
    binding = db.get(AppSetting, f"source_room:{room.id}")
    data = [room.platform, room.room_id, room.input_url, binding.value if binding else None]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def _source_unchanged(db: Session, room: LiveRoom | None, revision: str, identity: SourceRoom | None) -> bool:
    if room is None or _source_revision(db, room) != revision:
        return False
    if identity is None:
        return True
    try:
        return room_source(room, db) == identity
    except SourceError:
        return False


async def refresh_room_metadata(db_id: int) -> None:
    """串行化同房间请求，有界等待，只有房号匹配的非空详情才算成功。"""
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(db_id, asyncio.Lock()):
        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if room is None or room.platform == "local":
                return
            metadata = read_metadata(db, f"room_metadata:{db_id}", RoomMetadata) or RoomMetadata()
            revision = _source_revision(db, room)
            identity: SourceRoom | None = None
            identity_error: SourceError | None = None
            try:
                identity = room_source(room, db)
            except SourceError as exc:
                identity_error = exc
        now = datetime.now(UTC)
        if metadata.source_revision not in {None, revision}:
            metadata = RoomMetadata()
        if metadata.next_retry_at is not None and now < metadata.next_retry_at:
            return
        previous_failure = metadata.failure_kind
        metadata.attempted_at = now
        metadata.source_revision, metadata.source = revision, identity
        budget = settings.room_metadata_refresh_timeout_s
        stage = "room_identity"
        detail_failure = None
        try:
            if identity_error is not None:
                raise identity_error
            assert identity is not None
            stage = "source_room_info"
            async with asyncio.timeout(budget):
                info = await source_registry.get_room_info(identity)
            detail_failure = info.metadata_failure
            if detail_failure is not None:
                stage = "source_metadata"
                raise SourceError(detail_failure.message, retry_after=detail_failure.retry_after)
            stage = "validate_title"
            if not info.title or not info.title.strip():
                raise ValueError("房间详情暂未提供有效标题")
        except (TimeoutError, SourceError, ValueError) as exc:
            diagnostic = f"stage={stage} budget_s={budget:g} {safe_exception_summary(exc)}"
            metadata.error = f"标题刷新失败：{diagnostic}"
            code = (
                detail_failure.code
                if detail_failure is not None
                else exc.code
                if isinstance(exc, SourceError)
                else type(exc).__name__
            )
            metadata.failure_kind = f"{stage}:{code}"
            metadata.consecutive_failures += 1
            interval = settings.room_metadata_refresh_interval_s
            delay = min(interval * 2 ** min(metadata.consecutive_failures - 1, 10), max(interval, 600))
            if isinstance(exc, SourceError) and exc.retry_after is not None:
                delay = max(delay, exc.retry_after)
            retry_now = datetime.now(UTC)
            latest = datetime.max.replace(tzinfo=UTC)
            metadata.next_retry_at = (
                latest if delay >= (latest - retry_now).total_seconds() else retry_now + timedelta(seconds=delay)
            )
            with get_session(operation="room_metadata_failure") as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if not _source_unchanged(db, db.get(LiveRoom, db_id), revision, identity):
                    return
                _save(db, f"room_metadata:{db_id}", metadata)
            if previous_failure != metadata.failure_kind:
                logger.warning(
                    "标题刷新失败 room={} platform={} retry_s={} {}", db_id, room.platform, delay, diagnostic
                )
            return

        now = datetime.now(UTC)
        with get_session(operation="room_metadata_success") as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            room = db.get(LiveRoom, db_id)
            if not _source_unchanged(db, room, revision, identity):
                return
            assert room is not None
            room.title = info.title.strip()
            if info.uploader_name:
                room.uploader_name = info.uploader_name.strip()
            room.updated_at = now
            db.add(room)
            metadata.observed_at = info.observed_at
            metadata.error = None
            failures = metadata.consecutive_failures
            metadata.consecutive_failures = 0
            metadata.next_retry_at = None
            metadata.failure_kind = None
            _save(db, f"room_metadata:{db_id}", metadata)
            sessions = db.exec(
                select(RecordingSession).where(
                    RecordingSession.room_id == db_id,
                    RecordingSession.ended_at.is_(None),
                    RecordingSession.status.in_(
                        [
                            SessionStatus.STARTING,
                            SessionStatus.RECORDING,
                            SessionStatus.RECONNECTING,
                            SessionStatus.RECONNECTED,
                            SessionStatus.STOPPING,
                            SessionStatus.FINALIZING,
                        ]
                    ),
                )
            ).all()
            for session in sessions:
                key = f"session_metadata:{session.id}"
                snapshot = read_metadata(db, key, SessionMetadata) or SessionMetadata()
                if snapshot.ended_at is not None:
                    continue
                if snapshot.last_title != room.title:
                    snapshot.change_count += 1
                    _save(
                        db,
                        f"session_title_change:{session.id}:{snapshot.change_count:08d}",
                        TitleObservation(title=room.title, observed_at=info.observed_at),
                    )
                snapshot.last_title = room.title
                snapshot.observed_at = info.observed_at
                _save(db, key, snapshot)
        if failures:
            logger.info("标题刷新恢复 room={} prior_failures={}", db_id, failures)
