# ruff: noqa: D100
from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import Callable
from functools import partial

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.core.execution import run_sync
from app.core.media_usage import media_operation
from app.core.sanitize import safe_exception_summary
from app.db.entities import AppSetting
from app.db.errors import temporary_database_error
from app.db.session import get_session
from app.recording import intents
from app.recording.recovery import recover_owned_recording
from app.recording.writer_ownership import SessionWriter

_POLL_S = 5.0
_PAGE_SIZE = 20


def _writer_busy(error: OSError) -> bool:
    if os.name == "nt":
        return getattr(error, "winerror", None) in {32, 33}
    return isinstance(error, BlockingIOError) and error.errno in {errno.EAGAIN, errno.EWOULDBLOCK}


@media_operation()
def settle_exited_recording(record: intents.RecordingExecution, finalize: Callable[[Session, int], object]) -> bool:
    """只接管已经停止的旧场次，不修改房间、新意图或原有录制错误。"""
    try:
        writer = SessionWriter.recover(record.writer)
    except OSError as exc:
        if _writer_busy(exc):
            return False
        raise
    with writer:
        return recover_owned_recording(record, finalize)


class RecordingSettlement:
    """应用生命周期内唯一的收尾补做循环，有界续页且停止后不再领取。"""

    def __init__(self, finalize: Callable[[Session, int], object]) -> None:
        self._finalize = finalize
        self._after = ""
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._reported: dict[int, str] = {}

    def scan(self) -> list[int]:
        """推进本页扫描位置，包括仍在录制及故障记录，避免前项阻挡后项。"""
        with get_session(operation="recording_settlement_scan") as db:
            rows = db.exec(
                select(AppSetting)
                .where(AppSetting.key.startswith(intents.SESSION_PREFIX), AppSetting.key > self._after)
                .order_by(AppSetting.key)
                .limit(_PAGE_SIZE)
            ).all()
        completed = []
        for row in rows:
            if self._stop.is_set():
                break
            self._after = row.key
            session_id = int(row.key.removeprefix(intents.SESSION_PREFIX))
            try:
                record = intents.RecordingExecution.model_validate_json(row.value)
                if record.session_id != session_id:
                    raise ValueError("录制执行键与场次身份不一致")
                if not record.settled and settle_exited_recording(record, self._finalize):
                    completed.append(session_id)
                    if session_id in self._reported:
                        logger.info("录制收尾补做完成 session={}", session_id)
                    self._reported.pop(session_id, None)
            except (SQLAlchemyError, OSError, ValueError) as exc:
                detail = safe_exception_summary(exc)
                if self._reported.get(session_id) != detail:
                    logger.error("录制收尾尚未完成 session={} error={}", session_id, detail)
                    self._reported[session_id] = detail
        if len(rows) < _PAGE_SIZE:
            self._after = ""
        return completed

    async def start(self) -> None:
        """启动时仍严格证明全部旧 writer 退出，再开启同进程周期补做。"""
        from app.recording.danmaku_evidence import freeze_interrupted_captures
        from app.recording.recovery import recover_pending_recordings

        if self._task is not None:
            raise RuntimeError("录制收尾服务已经启动")
        self._stop.clear()
        await run_sync(partial(recover_pending_recordings, self._finalize))
        await run_sync(freeze_interrupted_captures)
        self._task = asyncio.create_task(self._run(), name="recording-settlement")
        self._task.add_done_callback(self._observe)

    def _observe(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.opt(exception=error).error("录制收尾服务停止，尚未完成的记录已保留")

    async def _run(self) -> None:
        database_failed = False
        while not self._stop.is_set():
            try:
                await run_sync(self.scan)
                if database_failed:
                    logger.info("录制收尾扫描数据库已恢复")
                database_failed = False
            except SQLAlchemyError as exc:
                if not temporary_database_error(exc):
                    raise
                if not database_failed:
                    logger.warning("录制收尾扫描暂时失败，下一轮重试 error={}", safe_exception_summary(exc))
                database_failed = True
            try:
                async with asyncio.timeout(_POLL_S):
                    await self._stop.wait()
            except TimeoutError:
                pass

    def request_stop(self) -> None:
        """停止领取新页，已经进入的单次同步操作仍须真正返回。"""
        self._stop.set()

    async def stop(self) -> None:
        """等待实际补做结束；持久未完成项留给下一次启动。"""
        self.request_stop()
        if self._task is not None:
            await self._task
            self._task = None
