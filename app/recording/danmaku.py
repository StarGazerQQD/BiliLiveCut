"""宿主弹幕采集生命周期及按实际连接区间保存的场次证据。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from app.core.async_cleanup import complete_cleanup
from app.core.config import settings
from app.core.execution import run_sync
from app.core.sanitize import safe_exception_summary
from app.db.entities import Danmaku, DanmakuType
from app.db.errors import temporary_database_error
from app.plugins.live_source import (
    DanmakuEvent,
    DanmakuSource,
    DanmakuStatus,
    LiveSource,
    SourceRoom,
    SourceTemporaryError,
)
from app.recording.danmaku_archive import DanmakuArchive
from app.recording.danmaku_evidence import (
    CaptureInterval,
    DanmakuEvidence,
    freeze_interrupted_captures as freeze_interrupted_captures,
    read_evidence as read_evidence,
)
from app.recording.danmaku_journal import DanmakuJournal, drain_journal

_DB_BATCH_SIZE = 100
_BATCH_POLL_S = 1.0
_COVERAGE_POLL_S = 5.0


class DanmakuCapture:
    """只管理可选弹幕，失败不会中断音视频及后续分析。"""

    def __init__(self, source: LiveSource, room: SourceRoom, db_room_id: int, session_id: int) -> None:
        from app.sources.bilibili.source import BilibiliSource

        self.room = room
        self.db_room_id = db_room_id
        self.session_id = session_id
        self.archive = DanmakuArchive(session_id, room)
        self.journal = DanmakuJournal(session_id)
        self._state_lock = asyncio.Lock()
        # 内置适配保留原有礼物、SC、进场及采样单位；不向外部契约暴露数据库。
        self.collector: DanmakuSource | None = (
            source.danmaku_for_session(session_id, archive=self.archive, journal=self.journal, gate=self._state_lock)
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
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._replay_task: asyncio.Task[None] | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._wake_replay = asyncio.Event()
        self._terminal_written = False
        self._database_failure = False
        self._queued = 0

    def _save_sync(self, payload: str) -> None:
        self.journal.append_observation(DanmakuEvidence.model_validate_json(payload))

    async def _write(self, operation: Callable[[], None]) -> None:
        # SQLite 锁等待隔离到工作线程；取消不能越过已经开始的写入。
        await complete_cleanup(asyncio.to_thread(operation))

    async def _flush_pending(self) -> None:
        await self._write(lambda: drain_journal(self.session_id, limit=_DB_BATCH_SIZE))

    async def _save(self, *, confirmed_until: datetime | None = None) -> None:
        if self._terminal_written:
            return
        confirmed = (
            confirmed_until
            if confirmed_until is not None
            else (self.evidence.confirmed_until if self.evidence.ended_at is not None else datetime.now(UTC))
        )
        self.evidence.archive_error = self.archive.error or self.evidence.archive_error
        payload = self.evidence.model_copy(update={"confirmed_until": confirmed}).model_dump_json()

        def persist() -> None:
            snapshot = DanmakuEvidence.model_validate_json(payload)
            self._save_sync(payload)
            # 文件提交和本地记号一起完成，取消不能造成终态重复追加。
            self._terminal_written = snapshot.ended_at is not None
            if self.evidence.ended_at is None or snapshot.ended_at is not None:
                self.evidence.confirmed_until = snapshot.confirmed_until

        try:
            await self._write(persist)
        except (OSError, ValueError, RuntimeError):
            self._freeze_failure()
            raise
        self._wake_replay.set()

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
        async with self._state_lock:
            if self._stopping or self.evidence.ended_at is not None:
                return

            def accept() -> None:
                self.archive.append([event.model_dump(mode="json")], event_format="normalized")
                self.journal.append_messages(
                    [
                        Danmaku(
                            session_id=self.session_id,
                            room_id=self.db_room_id,
                            ts=event.occurred_at,
                            msg_type=DanmakuType.DANMAKU,
                            content=event.content,
                            user=event.user_name,
                            value=1.0,
                        )
                    ]
                )

            try:
                await self._write(accept)
            except (OSError, ValueError, RuntimeError):
                self._freeze_failure()
                raise
            if self.evidence.status != DanmakuStatus.AVAILABLE:
                now = datetime.now(UTC)
                self.evidence.status = DanmakuStatus.AVAILABLE
                self.evidence.intervals.append(CaptureInterval(start=now))
                await self._save()
            self._queued += 1
            if self._queued >= _DB_BATCH_SIZE:
                self._queued = 0
                self._wake_replay.set()

    async def start(self) -> None:
        """先落证据状态，再启动可选采集任务。"""
        if self.evidence.status == DanmakuStatus.CONNECTING:
            await self._write(self.archive.start)
        await self._save()
        await self._try_replay()
        if self.evidence.status == DanmakuStatus.CONNECTING:
            self._task = asyncio.create_task(self._run())
            self._heartbeat_task = asyncio.create_task(self._checkpoint_coverage())
            self._replay_task = asyncio.create_task(self._replay())
            self._task.add_done_callback(self._observe_task)
            self._heartbeat_task.add_done_callback(self._observe_task)
            self._replay_task.add_done_callback(self._observe_task)

    def _freeze_failure(self) -> None:
        confirmed = self.evidence.confirmed_until
        self.evidence.status = DanmakuStatus.FAILED
        self.evidence.ended_at = confirmed or datetime.now(UTC)
        self.evidence.interrupted = True
        self.evidence.intervals = [
            CaptureInterval(start=interval.start, end=min(interval.end or confirmed, confirmed))
            for interval in self.evidence.intervals
            if confirmed is not None and interval.start <= confirmed
        ]

    def _observe_task(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._freeze_failure()
            logger.opt(exception=error).error("弹幕后台任务失败 session={}", self.session_id)
            for peer in (self._task, self._heartbeat_task, self._replay_task):
                if peer is not None and peer is not task:
                    peer.cancel()
            if self._failure_task is None:
                self._failure_task = asyncio.create_task(self._save_failure())

    async def _save_failure(self) -> None:
        """单次保存故障终态；数据库不可写时保留内存证据及文件日志。"""
        peers = [task for task in (self._task, self._heartbeat_task, self._replay_task) if task is not None]
        if peers:
            await asyncio.gather(*peers, return_exceptions=True)
        try:
            async with self._state_lock:
                await self._save()
            await self._try_replay()
        except Exception as exc:  # noqa: BLE001 — 失败记录的持久化边界
            logger.opt(exception=exc).error("弹幕失败状态无法保存 session={}", self.session_id)

    async def _checkpoint_coverage(self) -> None:
        loop = asyncio.get_running_loop()
        next_coverage = loop.time() + _COVERAGE_POLL_S
        while self._task is not None and not self._task.done():
            await asyncio.sleep(_BATCH_POLL_S)
            async with self._state_lock:
                if self._stopping:
                    return
                if self.evidence.status == DanmakuStatus.AVAILABLE and loop.time() >= next_coverage:
                    await self._save()
                    next_coverage = loop.time() + _COVERAGE_POLL_S

    async def _try_replay(self) -> bool | None:
        """一次有界写库，暂态失败返回等待；永久错误交由生命周期暴露。"""
        try:
            finished = await run_sync(partial(drain_journal, self.session_id, limit=_DB_BATCH_SIZE))
        except SQLAlchemyError as exc:
            if not temporary_database_error(exc):
                raise
            if not self._database_failure:
                logger.warning(
                    "弹幕数据库暂时不可写，保留补做记录 session={} error={}",
                    self.session_id,
                    safe_exception_summary(exc),
                )
            self._database_failure = True
            return None
        if self._database_failure:
            logger.info("弹幕数据库恢复，继续消费补做记录 session={}", self.session_id)
        self._database_failure = False
        return finished

    async def _replay(self) -> None:
        """数据库等待与接收分离；失败期间仅磁盘积压，后续轮次继续恢复。"""
        delay = _BATCH_POLL_S
        while not self._stopping:
            try:
                async with asyncio.timeout(delay):
                    await self._wake_replay.wait()
            except TimeoutError:
                pass
            self._wake_replay.clear()
            if self._stopping:
                return
            result = await self._try_replay()
            if result is None:
                delay = min(max(delay * 2, _BATCH_POLL_S), 30.0)
                # 新消息不能缩短数据库退避并造成连续请求。
                await asyncio.sleep(delay)
                self._wake_replay.set()
            else:
                delay = _BATCH_POLL_S
                if not result:
                    self._wake_replay.set()

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
                logger.warning(
                    "弹幕暂时失败 session={} attempt={}/3 retry_after={}: {}",
                    self.session_id,
                    attempt + 1,
                    exc.retry_after,
                    safe_exception_summary(exc),
                )
                await self._state(DanmakuStatus.FAILED)
                if attempt < 2:
                    await asyncio.sleep(max(2**attempt, exc.retry_after or 0.0))
            except Exception as exc:
                # 外部插件边界：记录脱敏后的类型和因果链。
                await self._state(DanmakuStatus.FAILED)
                logger.warning("弹幕采集失败 session={} error={}", self.session_id, safe_exception_summary(exc))
                return

    async def stop(self) -> None:
        """等待采集取消和连接释放后冻结覆盖，保留最后状态及已观测区间。"""
        self._stopping = True
        if self._replay_task is not None:
            self._replay_task.cancel()
            await asyncio.gather(self._replay_task, return_exceptions=True)
            self._replay_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._failure_task is not None:
            await self._failure_task
            self._failure_task = None
        async with self._state_lock:
            if self.evidence.ended_at is None:
                now = datetime.now(UTC)
                self.evidence.ended_at = now
                if self.evidence.intervals and self.evidence.intervals[-1].end is None:
                    self.evidence.intervals[-1].end = now
            # 停止时有界保存一次，覆盖旧的延迟写入并允许暂时坏库恢复后的终态落盘。
            await self._save(confirmed_until=self.evidence.ended_at)
        # 停止只尝试一批，不等待数据库恢复；剩余记录由场次收尾服务继续处理。
        await self._try_replay()
