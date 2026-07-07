"""转写阶段 Worker — compute/commit 分离 (V0.1.14.2)."""

from __future__ import annotations

import time

from app.db.models import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_heartbeat


def transcribe_compute(task_id: int) -> dict:
    """仅执行转写计算, 不写入 Task 状态。

    调用 transcribe_segment 进行语音识别, 收集计算结果。

    :param task_id: SegmentTask ID。
    :returns: {"segment_id": int}
    """
    from app.analysis.transcribe import transcribe_segment

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"segment_id": -1}
        segment_id = task.segment_id
    transcribe_segment(segment_id)
    return {"segment_id": segment_id}


def commit_transcript(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """单事务提交转写结果, 先校验租约。

    :param lease: 任务租约。
    :param compute_result: transcribe_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    import logging

    _logger = logging.getLogger(__name__)
    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()
            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.TRANSCRIBED)
            db.add(task)
    except LeaseLostError:
        _logger.warning("stale_result_discarded: transcript task=%s 已失去租约", lease.task_id)


def run_transcribe(lease: TaskLease) -> None:
    """执行转写阶段: 计算与提交分离。

    :param lease: 任务租约。
    """
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = transcribe_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_transcript(lease, compute_result, ms_val)
