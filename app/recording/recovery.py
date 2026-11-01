# ruff: noqa: D100
from __future__ import annotations

import csv
import math
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import Session, select

from app.clipping.core import probe_media
from app.core.media_usage import media_operation
from app.core.paths import resolve_storage_path, session_raw_dir
from app.db.entities import LiveRoom, RawSegment, RecordingSession, SessionStatus, utcnow
from app.db.session import get_session
from app.recording import intents
from app.recording.segments import register_segment
from app.recording.writer_ownership import SessionWriter


def _attempt_files(attempt: intents.RecordingAttempt, directory: Path) -> tuple[list[Path], list[str]]:
    if (
        resolve_storage_path(attempt.directory) != directory
        or resolve_storage_path(attempt.segment_list).parent != directory
    ):
        raise ValueError("持久录制线路与当前场次目录不一致")
    pattern = re.compile(rf"{re.escape(attempt.prefix)}[0-9]{{5,}}\.ts")
    files: set[Path] = set()
    errors = []
    listing = resolve_storage_path(attempt.segment_list)
    if listing.exists():
        with listing.open(encoding="utf-8", newline="") as handle:
            try:
                for row in csv.reader(handle, strict=True):
                    if len(row) != 3:
                        errors.append(f"清单行不完整：{listing.name}")
                        continue
                    candidate = directory / row[0]
                    if candidate.resolve().parent != directory or not pattern.fullmatch(candidate.name):
                        errors.append(f"清单含不属于已接纳线路的路径：{listing.name}")
                        continue
                    files.add(candidate)
            except (csv.Error, UnicodeError):
                errors.append(f"清单解析失败，保留原文件：{listing.name}")
    files.update(path for path in directory.glob(f"{attempt.prefix}*.ts") if pattern.fullmatch(path.name))
    return sorted(files), errors


@media_operation()
def recover_pending_recordings(finalize: Callable[[Session, int], object]) -> list[int]:
    """在工作线程及调度启动前证明旧 writer 退出、清算尾片并提交旧场终态。"""
    with get_session() as db:
        records = intents.pending_executions(db)
    settled = []
    for record in sorted(records, key=lambda item: item.session_id):
        # 原证明不存在、被替换或仍由实际 writer 占用时，启动必须失败，不能报告就绪。
        with SessionWriter.recover(record.writer):
            directory = session_raw_dir(record.session_id).resolve()
            with get_session() as db:
                registered = {
                    str(resolve_storage_path(value))
                    for value in db.exec(
                        select(RawSegment.file_path).where(RawSegment.session_id == record.session_id)
                    ).all()
                }
            errors: list[str] = []
            for attempt in record.attempts:
                paths, failures = _attempt_files(attempt, directory)
                errors.extend(failures)
                for path in paths:
                    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
                        errors.append(f"尾片缺失或路径已改变：{path.name}")
                        continue
                    canonical = str(path.resolve())
                    if canonical in registered:
                        continue
                    before = path.stat()
                    if before.st_size == 0:
                        errors.append(f"空尾片已保留：{path.name}")
                        continue
                    duration, width, height = probe_media(str(path))
                    after = path.stat()
                    if _identity(before) != _identity(after):
                        raise RuntimeError(f"清算期间媒体被修改：{path.name}")
                    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
                        errors.append(f"尾片不可播放，原文件已保留：{path.name}")
                        continue
                    with get_session() as db:
                        if db.get_bind().dialect.name == "sqlite":
                            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                        if _identity(path.stat()) != _identity(before):
                            raise RuntimeError(f"尾片登记前文件被修改：{path.name}")
                        register_segment(
                            db,
                            record.session_id,
                            path,
                            duration=duration,
                            ended_at=datetime.fromtimestamp(before.st_mtime, UTC),
                            size=before.st_size,
                        )
                    registered.add(canonical)
            with get_session() as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                current = intents.load_execution(db, record.session_id)
                if current is None or current.writer != record.writer or current.attempts != record.attempts:
                    raise RuntimeError("清算期间录制执行身份已改变")
                if current.settled:
                    continue
                session = db.get(RecordingSession, record.session_id)
                if session is None:
                    raise ValueError("录制执行缺少所属场次")
                if not current.finalized:
                    from app.analysis.room_config import load_room_config

                    intent = intents.load_intent(db, record.room_id)
                    stopped = (
                        intent is not None and intent.generation == record.generation and intent.state == "stopped"
                    )
                    paused = load_room_config(db.get(LiveRoom, record.room_id)).get("recording_paused", False)
                    session.status = (
                        (SessionStatus.PAUSED if paused else SessionStatus.STOPPED)
                        if stopped
                        else SessionStatus.INTERRUPTED
                    )
                    session.ended_at = utcnow()
                    session.error_message = "服务异常中断；旧 writer 已退出，已清算可用尾片"
                if errors:
                    session.error_message = (session.error_message or "") + "；部分尾片需人工检查"
                db.add(session)
                room = db.get(LiveRoom, record.room_id)
                if room is not None:
                    room.enabled = False
                    db.add(room)
                current.recovery_errors = errors
                current.finalized = True
                intents.save(db, current)
                finalize(db, record.session_id)
            settled.append(record.session_id)
            for error in errors:
                logger.warning("录制清算 session={} detail={}", record.session_id, error)
    return settled


def _identity(stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
