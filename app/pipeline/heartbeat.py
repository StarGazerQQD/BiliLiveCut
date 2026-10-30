# ruff: noqa: D100
from __future__ import annotations

import threading
from time import monotonic

from loguru import logger
from sqlalchemy import and_, or_, update
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from app.db import session as database
from app.db.entities import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lifecycle import _WORKER_ID, now_utc, shutdown_event

_HEARTBEAT_POLL_S: int = 5
_BATCH_SIZE = 64
_CONDITION = threading.Condition()
_REGISTRATIONS: set[_HeartbeatStop] = set()
_worker: threading.Thread | None = None


class _HeartbeatStop(threading.Event):
    def __init__(self, task_id: int, lease_token: str, expected_stage: str | None) -> None:
        super().__init__()
        self.task_id = task_id
        self.lease_token = lease_token
        self.expected_stage = expected_stage
        self.engine = database.engine
        self.shutdown = shutdown_event
        self.due = monotonic() + 0.01
        self.in_flight = False

    def set(self) -> None:
        """撤销登记并等待已发出的写入提交，防止清除后被旧批次重新续租。"""
        with _CONDITION:
            super().set()
            _CONDITION.notify_all()
            while self.in_flight:
                _CONDITION.wait()
            _REGISTRATIONS.discard(self)


def _retire(registration: _HeartbeatStop) -> None:
    threading.Event.set(registration)
    _REGISTRATIONS.discard(registration)


def _write_batch(batch: list[_HeartbeatStop]) -> set[_HeartbeatStop]:
    conditions = [
        and_(
            SegmentTask.id == item.task_id,
            SegmentTask.claimed_by == _WORKER_ID,
            SegmentTask.lease_token == item.lease_token,
            *([SegmentTask.stage == item.expected_stage] if item.expected_stage is not None else []),
        )
        for item in batch
    ]
    with get_session() as db:
        # 引擎切换后，旧登记不能写入新数据库。
        if db.get_bind() is not batch[0].engine or batch[0].engine is not database.engine:
            return set(batch)
        result = db.execute(
            update(SegmentTask)
            .where(or_(*conditions))
            .values(heartbeat_at=now_utc())
            .returning(SegmentTask.id, SegmentTask.lease_token, SegmentTask.stage)
            .execution_options(synchronize_session=False)
        )
        updated = set(result.all())
        ended = {
            item
            for item in batch
            if not any(
                task_id == item.task_id
                and token == item.lease_token
                and (item.expected_stage is None or stage == item.expected_stage)
                for task_id, token, stage in updated
            )
        }
        for item in ended:
            _log_heartbeat_end(db, item.task_id, item.expected_stage)
    return ended


def _coordinate() -> None:
    global _worker
    while True:
        with _CONDITION:
            for item in tuple(_REGISTRATIONS):
                if item.is_set() or item.shutdown.is_set() or item.engine is not database.engine:
                    _retire(item)
            if not _REGISTRATIONS:
                _worker = None
                _CONDITION.notify_all()
                return
            now = monotonic()
            due = sorted(_REGISTRATIONS, key=lambda item: item.due)
            delay = due[0].due - now
            if delay > 0:
                _CONDITION.wait(min(delay, 0.25))
                continue
            batch = [item for item in due if item.due <= now][:_BATCH_SIZE]
            for item in batch:
                item.in_flight = True
        ended: set[_HeartbeatStop] = set()
        try:
            ended = _write_batch(batch)
        except SQLAlchemyError:
            logger.bind(_skip_db=True).exception(
                "heartbeat_failed tasks={} count={} 心跳批写失败，将在下一周期重试",
                [item.task_id for item in batch],
                len(batch),
            )
        finally:
            with _CONDITION:
                for item in batch:
                    item.in_flight = False
                    item.due = monotonic() + _HEARTBEAT_POLL_S
                    if item in ended:
                        _retire(item)
                _CONDITION.notify_all()


def _log_heartbeat_end(db: Session, task_id: int, expected_stage: str | None) -> None:
    """按实际任务状态解释条件失败，不把正常取消或推进误报为接管。"""
    task = db.get(SegmentTask, task_id)
    stage = task.stage if task is not None else None
    if task is None or stage not in {
        TaskStatus.TRANSCRIBING,
        TaskStatus.ANALYZING,
        TaskStatus.RENDERING,
        TaskStatus.PUBLISHING,
    }:
        logger.info(
            "heartbeat_stopped task={} stage={} expected_stage={} reason=state_changed",
            task_id,
            stage,
            expected_stage,
        )
    else:
        logger.warning(
            "lease_lost task={} stage={} expected_stage={} observed_worker={} reason=ownership_conditions_failed",
            task_id,
            stage,
            expected_stage,
            task.claimed_by,
        )


def start_heartbeat_thread(
    task_id: int,
    lease_token: str | None = None,
    expected_stage: str | None = None,
) -> threading.Event:
    """将租约登记到共享批写线程，返回可停止且等待在途提交的 Event。"""
    global _worker
    if not lease_token:
        raise ValueError("心跳更新必须提供租约 token")
    stop = _HeartbeatStop(task_id, lease_token, expected_stage)
    with _CONDITION:
        _REGISTRATIONS.add(stop)
        if _worker is None:
            _worker = threading.Thread(target=_coordinate, daemon=True, name="lease-heartbeats")
            _worker.start()
        _CONDITION.notify_all()
    return stop


def clear_heartbeat_if_own(task_id: int, lease_token: str | None = None) -> None:
    """条件清除 heartbeat, 必须携带租约。

    在任务完成后将 heartbeat_at 设为 NULL, 防止被 stale recovery 误判。
    使用租约条件 UPDATE, 租约已转移时跳过。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌。
    """
    if not lease_token:
        logger.debug("heartbeat_clear_refused task={} reason=missing_lease", task_id)
        return
    with _CONDITION:
        matches = [item for item in _REGISTRATIONS if item.task_id == task_id and item.lease_token == lease_token]
    for item in matches:
        item.set()
    try:
        with get_session() as db:
            if lease_token:
                result = db.exec(
                    sa_text(
                        """UPDATE segment_tasks
                           SET heartbeat_at = NULL
                           WHERE id = :task_id
                             AND claimed_by = :worker_id
                             AND lease_token = :lease_token"""
                    ),
                    params={
                        "task_id": task_id,
                        "worker_id": _WORKER_ID,
                        "lease_token": lease_token,
                    },
                )
                if result.rowcount == 0:
                    logger.debug("clear_heartbeat_if_own: task={} 租约已转移, 跳过清除", task_id)
                    return
                logger.debug("clear_heartbeat_if_own: task={} heartbeat 已清除", task_id)
    except SQLAlchemyError:
        logger.exception("heartbeat_clear_failed: task={} 心跳清理失败", task_id)
