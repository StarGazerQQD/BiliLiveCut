"""任务阶段结果操作 (V0.1.14.2)。

Worker 和阶段 worker 共享的任务状态变更函数。
拆分自 task_worker.py, 避免阶段 workers 与 task_worker 循环引用。
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from app.db.models import SegmentTask, TaskStatus

_RETRY_BASE_S = 10
_RETRY_MAX_S = 600
_RETRY_JITTER_S = 5

# ═══════════════════════════════════════════════════
# 状态转换矩阵
# ═══════════════════════════════════════════════════

_VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.RECORDED: {TaskStatus.QUEUED_FOR_TRANS},
    TaskStatus.QUEUED_FOR_TRANS: {TaskStatus.TRANSCRIBING},
    TaskStatus.TRANSCRIBING: {TaskStatus.TRANSCRIBED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSCRIBED: {TaskStatus.QUEUED_FOR_ANALYSIS},
    TaskStatus.QUEUED_FOR_ANALYSIS: {TaskStatus.ANALYZING},
    TaskStatus.ANALYZING: {TaskStatus.CANDIDATE_CREATED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.CANDIDATE_CREATED: {TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.APPROVED, TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.APPROVED_WAITING_RENDER, TaskStatus.QUEUED_FOR_RENDER},
    TaskStatus.APPROVED_WAITING_RENDER: {TaskStatus.QUEUED_FOR_RENDER, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_RENDER: {TaskStatus.RENDERING},
    TaskStatus.RENDERING: {TaskStatus.RENDERED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.RENDERED: {TaskStatus.AWAITING_PUBLISH_CONFIRMATION, TaskStatus.QUEUED_FOR_PUBLISH},
    TaskStatus.AWAITING_PUBLISH_CONFIRMATION: {TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_PUBLISH: {TaskStatus.PUBLISHING},
    TaskStatus.PUBLISHING: {TaskStatus.COMPLETED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSIENT_FAILED: {
        TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.FAILED,
    },
    TaskStatus.STALE: {
        TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.QUEUED_FOR_PUBLISH,
    },
}


def can_transition(current: str, target: str) -> bool:
    """判断状态转换是否合法。"""
    return target in _VALID_TRANSITIONS.get(current, set())


def active_stage(queued_stage: str) -> str:
    """将排队阶段映射到活跃阶段。"""
    return {
        TaskStatus.QUEUED_FOR_TRANS: TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS: TaskStatus.ANALYZING,
        TaskStatus.QUEUED_FOR_RENDER: TaskStatus.RENDERING,
        TaskStatus.QUEUED_FOR_PUBLISH: TaskStatus.PUBLISHING,
    }.get(queued_stage, queued_stage)


# ═══════════════════════════════════════════════════
# 幂等键生成
# ═══════════════════════════════════════════════════


def _now() -> datetime:
    return datetime.now(UTC)


def make_pipeline_key(segment_id: int) -> str:
    """流程级幂等键: 创建后永不修改。"""
    return f"pipeline:{segment_id}"


def make_stage_key(segment_id: int, stage: str) -> str:
    """阶段级幂等键: enqueue_next 时更新。"""
    return f"stage:{segment_id}:{stage}"


def make_idempotency_key(segment_id: int, stage: str) -> str:
    """[后向兼容] 旧幂等键。"""
    return f"{segment_id}:{stage}"


def _jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    return base + random.uniform(0, jitter_s)


# ═══════════════════════════════════════════════════
# 任务状态操作
# ═══════════════════════════════════════════════════


def mark_active(task: SegmentTask) -> None:
    """唯一 attempts++ 点:仅在 _pop_and_claim 成功后调用。"""
    task.attempts += 1
    task.started_at = _now()
    task.heartbeat_at = _now()
    task.last_error = None


def mark_heartbeat(task: SegmentTask) -> None:
    """更新任务心跳时间,防止被 stale recovery 误判。"""
    task.heartbeat_at = _now()


def mark_completed(task: SegmentTask, processing_ms: int | None = None) -> None:
    """标记任务完成,记录总耗时。"""
    if processing_ms is None and task.started_at is not None:
        processing_ms = int((_now() - task.started_at).total_seconds() * 1000)
    task.processing_time_ms = processing_ms
    task.completed_at = _now()
    task.heartbeat_at = None


def mark_failed(task: SegmentTask, error: str, permanent: bool = False) -> None:
    """标记任务失败,记录失败阶段和错误信息。"""
    task.last_error = error[:1000]
    task.error_is_permanent = permanent
    task.failed_stage = task.stage
    task.heartbeat_at = None
    if permanent:
        task.stage = TaskStatus.FAILED
        task.completed_at = _now()
        try:
            from app.notify.webhook import notify_task_failed

            notify_task_failed(task.id, task.failed_stage, error[:200])
        except Exception:
            pass
    else:
        delay = _jitter(min(_RETRY_BASE_S * (2 ** max(task.attempts - 1, 0)), _RETRY_MAX_S))
        task.next_retry_at = _now() + timedelta(seconds=delay)
        task.stage = TaskStatus.TRANSIENT_FAILED


def enqueue_next(
    task: SegmentTask,
    next_stage: str,
    candidate_id: int | None = None,
    event_id: int | None = None,
    clip_id: int | None = None,
) -> None:
    """将任务推进到下一阶段,重置 attempts 并计算完成时间。

    pipeline_key 仅在首次创建时设置,此后永不修改。
    stage_key 随阶段更新,用于阶段级幂等。
    """
    current = task.stage
    if not can_transition(current, next_stage):
        raise ValueError(f"非法转换: {current} -> {next_stage}")
    task.stage = next_stage
    task.stage_key = make_stage_key(task.segment_id, next_stage)
    task.idempotency_key = make_idempotency_key(task.segment_id, next_stage)
    task.attempts = 0
    task.last_error = None
    task.error_is_permanent = False
    task.next_retry_at = None
    task.claimed_by = None
    task.claimed_at = None
    task.heartbeat_at = None
    task.lease_token = None
    if next_stage in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        task.completed_at = _now()
        if task.created_at:
            task.total_elapsed_ms = int((_now() - task.created_at).total_seconds() * 1000)
    if candidate_id is not None:
        task.candidate_id = candidate_id
    if event_id is not None:
        task.event_id = event_id
    if clip_id is not None:
        task.clip_id = clip_id
