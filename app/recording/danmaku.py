"""宿主弹幕采集生命周期及按实际连接区间保存的场次证据。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from loguru import logger
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.core.async_cleanup import complete_cleanup
from app.core.config import settings
from app.db.entities import AppSetting, Danmaku, DanmakuType
from app.db.session import get_session
from app.plugins.live_source import (
    DanmakuEvent,
    DanmakuSource,
    DanmakuStatus,
    LiveSource,
    SourceRoom,
    SourceTemporaryError,
)
from app.recording.metadata import read_metadata


class CaptureInterval(BaseModel):
    """已确认连通的 UTC 区间，断连和正常停止均关闭区间。"""

    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime
    end: AwareDatetime | None = None


class DanmakuEvidence(BaseModel):
    """状态与历史覆盖分开，避免断连抹掉历史或缺失被当作零事件。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    status: DanmakuStatus
    lag_s: float = 0.0
    intervals: list[CaptureInterval] = Field(default_factory=list)
    ended_at: AwareDatetime | None = None
    confirmed_until: AwareDatetime | None = None
    interrupted: bool = False


def read_evidence(db: Session, session_id: int) -> DanmakuEvidence | None:
    """读取场次证据；没有记录的旧场次不推断为成功采集。"""
    return read_metadata(db, f"session_danmaku:{session_id}", DanmakuEvidence)


def freeze_interrupted_captures() -> None:
    """启动任务之前冻结遗留连接，仅认可崩溃前最后一次持久观测。"""
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith("session_danmaku:"))).all()
        for row in rows:
            evidence = read_metadata(db, row.key, DanmakuEvidence)
            if evidence is None or evidence.ended_at is not None:
                continue
            for interval in evidence.intervals:
                if interval.end is None:
                    interval.end = max(interval.start, evidence.confirmed_until or interval.start)
            evidence.ended_at = evidence.confirmed_until or datetime.now(UTC)
            evidence.interrupted = True
            if evidence.status in {DanmakuStatus.AVAILABLE, DanmakuStatus.CONNECTING}:
                evidence.status = DanmakuStatus.FAILED
            row.value = evidence.model_dump_json()
            row.updated_at = datetime.now(UTC)
            db.add(row)


class DanmakuCapture:
    """只管理可选弹幕，失败不会中断音视频及后续分析。"""

    def __init__(self, source: LiveSource, room: SourceRoom, db_room_id: int, session_id: int) -> None:
        from app.sources.bilibili.source import BilibiliSource

        self.room = room
        self.db_room_id = db_room_id
        self.session_id = session_id
        # 内置适配保留原有礼物、SC、进场及采样单位；不向外部契约暴露数据库。
        self.collector: DanmakuSource | None = (
            source.danmaku_for_session(session_id)
            if isinstance(source, BilibiliSource)
            else source
            if isinstance(source, DanmakuSource)
            else None
        )
        status = DanmakuStatus.CONNECTING if self.collector is not None else DanmakuStatus.UNSUPPORTED
        if not settings.collect_danmaku:
            status = DanmakuStatus.DISABLED
        self.evidence = DanmakuEvidence(
            status=status,
            lag_s=settings.danmaku_event_lag_s if isinstance(source, BilibiliSource) else 0.0,
        )
        self._task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stopping = False

    def _save_sync(self, payload: str) -> None:
        with get_session() as db:
            key = f"session_danmaku:{self.session_id}"
            row = db.get(AppSetting, key) or AppSetting(key=key)
            row.value = payload
            row.updated_at = datetime.now(UTC)
            db.add(row)

    async def _write(self, operation: Callable[[], None]) -> None:
        # SQLite 锁等待隔离到工作线程；取消不能越过已经开始的写入。
        await complete_cleanup(asyncio.to_thread(operation))

    async def _save(self) -> None:
        self.evidence.confirmed_until = datetime.now(UTC)
        payload = self.evidence.model_dump_json()
        await self._write(lambda: self._save_sync(payload))

    async def _state(self, value: DanmakuStatus) -> None:
        async with self._state_lock:
            if not isinstance(value, DanmakuStatus) or value in {DanmakuStatus.UNSUPPORTED, DanmakuStatus.DISABLED}:
                raise ValueError("采集器只能报告 connecting、available 或 failed")
            if self._stopping or self.evidence.ended_at is not None or value == self.evidence.status:
                return
            now = datetime.now(UTC)
            if self.evidence.intervals and self.evidence.intervals[-1].end is None:
                self.evidence.intervals[-1].end = now
            if value == DanmakuStatus.AVAILABLE:
                self.evidence.intervals.append(CaptureInterval(start=now))
            self.evidence.status = value
            await self._save()

    async def _emit(self, event: DanmakuEvent) -> None:
        if self._stopping or self.evidence.ended_at is not None:
            return
        # 不接受插件自定义子类中的金额/热度字段，不把平台单位混到普通文本计数。
        if type(event) is not DanmakuEvent:
            raise ValueError("弹幕必须为公共 DanmakuEvent")
        event = DanmakuEvent.model_validate(event.model_dump())
        await self._state(DanmakuStatus.AVAILABLE)

        def insert() -> None:
            with get_session() as db:
                db.add(
                    Danmaku(
                        session_id=self.session_id,
                        room_id=self.db_room_id,
                        ts=event.occurred_at,
                        msg_type=DanmakuType.DANMAKU,
                        content=event.content,
                        user=event.user_name,
                        value=1.0,
                    )
                )

        await self._write(insert)

    async def start(self) -> None:
        """先落证据状态，再启动可选采集任务。"""
        await self._save()
        if self.evidence.status == DanmakuStatus.CONNECTING:
            self._task = asyncio.create_task(self._run())
            self._heartbeat_task = asyncio.create_task(self._checkpoint_coverage())

    async def _checkpoint_coverage(self) -> None:
        while self._task is not None and not self._task.done():
            await asyncio.sleep(5)
            async with self._state_lock:
                if self.evidence.status == DanmakuStatus.AVAILABLE and not self._stopping:
                    await self._save()

    async def _run(self) -> None:
        assert self.collector is not None
        for attempt in range(3):
            try:
                await self._state(DanmakuStatus.CONNECTING)
                await self.collector.collect_danmaku(self.room, self._emit, self._state)
                await self._state(DanmakuStatus.FAILED)
                return
            except asyncio.CancelledError:
                raise
            except SourceTemporaryError as exc:
                await self._state(DanmakuStatus.FAILED)
                if attempt < 2:
                    await asyncio.sleep(max(2**attempt, exc.retry_after or 0.0))
            except Exception as exc:
                # 外部插件边界：记录类型，不回显可能带凭据的异常正文。
                await self._state(DanmakuStatus.FAILED)
                logger.warning("弹幕采集失败 session={} error={}", self.session_id, type(exc).__name__)
                return

    async def stop(self) -> None:
        """等待采集取消和连接释放后冻结覆盖，保留最后状态及已观测区间。"""
        self._stopping = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.evidence.ended_at is None:
            now = datetime.now(UTC)
            self.evidence.ended_at = now
            if self.evidence.intervals and self.evidence.intervals[-1].end is None:
                self.evidence.intervals[-1].end = now
            await self._save()
