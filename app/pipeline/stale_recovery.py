"""Stale 任务恢复 — 心跳超时回退 + 孤立片段发现 + Clip PENDING 恢复。"""

from __future__ import annotations

from datetime import UTC, timedelta
from pathlib import Path

from loguru import logger
from sqlmodel import or_, select

from app.core.config import settings
from app.core.media_usage import media_reference_scope
from app.db.entities import (
    ClipVariant,
    RawSegment,
    RenderStatus,
    SegmentTask,
    TaskStatus,
    UploadAttempt,
    UploadStatus,
)
from app.db.entities import SegmentStatus as OldStatus
from app.db.session import get_session
from app.pipeline.lifecycle import now_utc
from app.pipeline.stage_result import enqueue_next, make_stage_key, release_lease


def resume_stage(failed_stage: str | None) -> str:
    """根据失败阶段返回应回退到的排队阶段。

    :param failed_stage: 失败时的活跃阶段。
    :returns: 正确的排队阶段。
    """
    if failed_stage is None:
        return TaskStatus.QUEUED_FOR_TRANS
    mapping = {
        TaskStatus.QUEUED_FOR_ANALYSIS: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.QUEUED_FOR_PUBLISH: TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.APPROVED_WAITING_RENDER: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.AWAITING_PUBLISH_CONFIRMATION: TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
        TaskStatus.REVIEWED_WAITING_ACTION: TaskStatus.REVIEWED_WAITING_ACTION,
        TaskStatus.AWAITING_REVIEW: TaskStatus.AWAITING_REVIEW,
        TaskStatus.APPROVED: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.TRANSCRIBING: TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.ANALYZING: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.RENDERING: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.PUBLISHING: TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.TRANSCRIBED: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.CANDIDATE_CREATED: TaskStatus.CANDIDATE_CREATED,
        TaskStatus.RENDERED: TaskStatus.RENDERED,
    }
    return mapping.get(failed_stage, TaskStatus.QUEUED_FOR_TRANS)


def recover_stale() -> None:
    """心跳超时的活跃任务回退到排队状态。

    发布任务特殊处理:
    - 已有 UploadAttempt 且状态为 in_progress/reconciliation_required → 不重新排队
    - 仅 PREPARED 或无限 attempt → 允许重新排队
    """
    stale_threshold = now_utc() - timedelta(seconds=settings.stale_timeout_s)

    with get_session() as db:
        # 在读取心跳前锁定 SQLite 写事务，避免读取后心跳刷新或租约转移仍被回收。
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        active = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.ANALYZING,
                        TaskStatus.RENDERING,
                        TaskStatus.PUBLISHING,
                        TaskStatus.CANCELLING,
                    ]
                ),
            )
        ).all()
        # 比较实际 UTC 时刻，不依赖 SQLite TEXT 的分隔符或时区表示。
        stale = [
            task
            for task in active
            if (last_seen := task.heartbeat_at or task.claimed_at or task.started_at or task.created_at) is not None
            and (last_seen.replace(tzinfo=UTC) if last_seen.tzinfo is None else last_seen.astimezone(UTC))
            < stale_threshold
        ]

        published_skipped = 0
        cleanup_pending = 0
        for task in stale:
            if task.stage == TaskStatus.CANCELLING:
                from app.analysis.transcription.process_pool import get_process_pool
                from app.pipeline.lifecycle import _WORKER_ID, execution_active

                if (
                    task.claimed_by == _WORKER_ID
                    and task.lease_token
                    and (
                        execution_active(task.id, task.lease_token)
                        or get_process_pool().has_active_task(task.id, task.lease_token)
                    )
                ):
                    cleanup_pending += 1
                    continue
                enqueue_next(task, TaskStatus.CANCELLED)
                db.add(task)
                logger.info("stale_cancellation_settled task={} session={}", task.id, task.session_id)
                continue
            # 发布任务特殊处理: 检查 UploadAttempt
            if task.stage == TaskStatus.PUBLISHING and task.clip_id is not None:
                last_attempt = db.exec(
                    select(UploadAttempt)
                    .where(
                        UploadAttempt.clip_id == task.clip_id,
                    )
                    .order_by(UploadAttempt.id.desc())
                ).first()

                if last_attempt is not None and last_attempt.status in (
                    "in_progress",
                    UploadStatus.RECONCILIATION_REQUIRED,
                ):
                    logger.warning(
                        "stale_publish_skipped: task={} clip={} attempt={} status={}",
                        task.id,
                        task.clip_id,
                        last_attempt.attempt_token,
                        last_attempt.status,
                    )
                    # 不重新排队 — 等待人工处理
                    published_skipped += 1
                    continue

                # RECONCILIATION_REQUIRED 的 attempt 不重排队
                if last_attempt is not None and last_attempt.status == UploadStatus.RECONCILIATION_REQUIRED:
                    published_skipped += 1
                    continue

            res = resume_stage(task.stage)
            task.stage = res
            task.stage_key = make_stage_key(task.segment_id, res)
            task.claimed_by = None
            task.claimed_at = None
            task.heartbeat_at = None
            task.lease_token = None
            task.next_retry_at = None
            db.add(task)

        # 修复历史失败路径留下的租约；只清理已结算的终态，不接管活动任务。
        terminal = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.TRANSIENT_FAILED]
                ),
                or_(
                    SegmentTask.claimed_by.is_not(None),
                    SegmentTask.claimed_at.is_not(None),
                    SegmentTask.heartbeat_at.is_not(None),
                    SegmentTask.lease_token.is_not(None),
                ),
            )
        ).all()
        for task in terminal:
            if any((task.claimed_by, task.claimed_at, task.heartbeat_at, task.lease_token)):
                logger.warning(
                    "terminal_lease_released task={} session={} stage={}", task.id, task.session_id, task.stage
                )
                release_lease(task)
                db.add(task)
        if stale:
            skipped_msg = f" (跳过 {published_skipped} 个发布任务)" if published_skipped else ""
            logger.warning(
                "recover_stale: 回退或结算 {} 个心跳超时任务。{} 等待取消资源收尾={}",
                len(stale) - published_skipped - cleanup_pending,
                skipped_msg,
                cleanup_pending,
            )
        db.commit()


@media_reference_scope()
def recover_orphans() -> None:
    """恢复孤立任务: stale 恢复 + 孤立片段任务创建。"""
    from app.pipeline.stage_result import make_pipeline_key, make_stage_key

    recover_stale()
    with get_session() as db:
        existing_ids = set(db.exec(select(SegmentTask.segment_id)).all())
        orphan_segs = db.exec(
            select(RawSegment).where(
                RawSegment.status == OldStatus.RECORDED,
                ~RawSegment.id.in_(existing_ids) if existing_ids else True,
            )
        ).all()
        for seg in orphan_segs:
            pipeline_key = make_pipeline_key(seg.id)
            stage_key = make_stage_key(seg.id, "recorded")
            t = SegmentTask(
                segment_id=seg.id,
                session_id=seg.session_id,
                stage=TaskStatus.RECORDED,
                pipeline_key=pipeline_key,
                stage_key=stage_key,
            )
            db.add(t)
        if orphan_segs:
            logger.info("恢复: 为 {} 个孤立片段创建任务。", len(orphan_segs))


def release_stopped_computations(worker_id: str) -> int:
    """撤销关闭宽限后计算线程的租约，迟到的 LLM/ASR 结果不得写入。"""
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        tasks = db.exec(
            select(SegmentTask).where(
                SegmentTask.claimed_by == worker_id,
                SegmentTask.stage.in_([TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING]),
            )
        ).all()
        for task in tasks:
            logger.warning(
                "shutdown_lease_revoked task={} segment={} session={} stage={} worker={} lease={} started_at={}",
                task.id,
                task.segment_id,
                task.session_id,
                task.stage,
                worker_id,
                task.lease_token,
                task.started_at,
            )
            task.stage = resume_stage(task.stage)
            task.stage_key = make_stage_key(task.segment_id, task.stage)
            release_lease(task)
            db.add(task)
        return len(tasks)


def recover_pending_clips() -> int:
    """恢复 PENDING 状态的 ClipVariant — 扫描文件系统, 标记 READY 或 FAILED。

    场景:
    - ClipVariant.render_status == QUEUED (PENDING)
    - partial 存在 → 未完成, 标记 FAILED 等待重试
    - formal 存在 → 文件已就位, 标记 DONE (READY)
    - 无任何文件 → 标记 FAILED

    :returns: 恢复的 ClipVariant 数量。
    """
    recovered = 0
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        pending_variants = db.exec(select(ClipVariant).where(ClipVariant.render_status == RenderStatus.QUEUED)).all()

        for var in pending_variants:
            file_path = var.file_path
            formal_exists = (
                file_path and Path(file_path).is_file() and (not var.cover_path or Path(var.cover_path).is_file())
            )

            if formal_exists:
                # 文件已就位 — 标记 READY
                var.render_status = RenderStatus.DONE
                db.add(var)
                recovered += 1
                logger.info(
                    "clip_recovery_mark_ready: variant={} event={} path={}",
                    var.id,
                    var.event_id,
                    file_path,
                )
            else:
                # 无文件 — 标记 FAILED
                var.render_status = RenderStatus.FAILED
                db.add(var)
                recovered += 1

        if recovered:
            db.commit()
            logger.info("clip_recovery: 恢复 {} 个 PENDING ClipVariant", recovered)

    return recovered


def recover_stale_upload_attempts() -> int:
    """恢复过期尝试：未发出的 PREPARED 可重试，IN_PROGRESS 需核对。

    当 Attempt 超过阈值仍处于 IN_PROGRESS, 不得直接重试。
    必须转为 RECONCILIATION_REQUIRED (不能证明平台未成功)。

    Returns: 恢复的 Attempt 数量。
    """
    from datetime import UTC, datetime

    threshold = datetime.now(UTC) - timedelta(seconds=settings.upload_attempt_stale_s)

    with get_session() as db:
        stale_attempts = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.status.in_(["prepared", "in_progress"]),
                UploadAttempt.started_at < threshold,
            )
        ).all()

        recovered = 0
        for attempt in stale_attempts:
            attempt.status = (
                "failed_retryable" if attempt.status == "prepared" else UploadStatus.RECONCILIATION_REQUIRED
            )
            attempt.finished_at = datetime.now(UTC)
            attempt.error_type = "stale_timeout"
            attempt.error_message = f"Attempt stale after {settings.upload_attempt_stale_s}s"
            db.add(attempt)
            from app.db.entities import UploadTask

            upload_task = db.get(UploadTask, attempt.upload_task_id)
            if upload_task is not None and upload_task.publish_generation == attempt.publish_generation:
                upload_task.status = attempt.status
                upload_task.claimed_by = None
                upload_task.last_error = attempt.error_message
                upload_task.updated_at = datetime.now(UTC)
                db.add(upload_task)
            recovered += 1
            logger.warning(
                "upload_attempt_stale_recovery: attempt={} clip={} → {}",
                attempt.attempt_token,
                attempt.clip_id,
                attempt.status,
            )

        if recovered:
            db.commit()
            logger.info("upload_attempt_recovery: recovered={}", recovered)

    return recovered


@media_reference_scope()
def sync_segment_task_from_attempt() -> int:
    """使用最新尝试或已确认成功的证据，同步上传、成片及全部相关发布任务。"""
    from app.db.entities import ClipStatus, FinalClip, UploadTask
    from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed

    synced = 0
    with get_session() as db:
        latest: dict[int, UploadAttempt] = {}
        attempts = db.exec(
            select(UploadAttempt).order_by(UploadAttempt.created_at.desc(), UploadAttempt.id.desc())
        ).all()
        for attempt in attempts:
            if attempt.clip_id not in latest or attempt.status == UploadStatus.SUCCESS:
                latest[attempt.clip_id] = attempt
        for attempt in latest.values():
            if attempt.status not in {"success", "reconciliation_required", "failed_permanent"}:
                continue
            upload = db.get(UploadTask, attempt.upload_task_id)
            if upload is not None and upload.publish_generation == attempt.publish_generation:
                upload.status = attempt.status
                upload.remote_id = attempt.remote_id
                upload.last_error = attempt.error_message
                upload.claimed_by = None
                db.add(upload)
            if attempt.status == UploadStatus.SUCCESS:
                clip = db.get(FinalClip, attempt.clip_id)
                if clip is not None:
                    clip.status = ClipStatus.PUBLISHED
                    db.add(clip)
            tasks = db.exec(
                select(SegmentTask).where(
                    SegmentTask.clip_id == attempt.clip_id,
                    SegmentTask.stage.in_(
                        [
                            TaskStatus.PUBLISHING,
                            TaskStatus.QUEUED_FOR_PUBLISH,
                            TaskStatus.RENDERED,
                            TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
                        ]
                    ),
                )
            ).all()
            for task in tasks:
                if attempt.status == UploadStatus.SUCCESS:
                    mark_completed(task)
                    enqueue_next(task, TaskStatus.COMPLETED)
                elif attempt.status == UploadStatus.RECONCILIATION_REQUIRED:
                    if task.stage == TaskStatus.AWAITING_PUBLISH_CONFIRMATION:
                        continue
                    enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
                    task.last_error = "平台结果未知，必须核对后确认，禁止自动重投"
                else:
                    mark_failed(task, attempt.error_message or "publish failed", permanent=True)
                task.claimed_by = None
                task.lease_token = None
                db.add(task)
                synced += 1
    return synced


def full_recovery() -> dict[str, int]:
    """执行全量恢复: stale task + upload attempt + clip + sync。"""
    return {
        "stale_tasks": recover_stale() or 0,
        "upload_attempts": recover_stale_upload_attempts(),
        "pending_clips": recover_pending_clips(),
        "task_sync": sync_segment_task_from_attempt(),
    }
