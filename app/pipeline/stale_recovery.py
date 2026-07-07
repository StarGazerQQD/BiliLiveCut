"""Stale 任务恢复 — 心跳超时回退 + 孤立片段发现 + Clip PENDING 恢复。"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from app.db.models import (
    ClipVariant,
    RawSegment,
    RenderStatus,
    SegmentTask,
    TaskStatus,
    UploadAttempt,
    UploadStatus,
)
from app.db.models import SegmentStatus as OldStatus
from app.db.session import get_session
from app.pipeline.lifecycle import now_utc

_logger = logging.getLogger(__name__)

_STALE_TIMEOUT_S: int = int(os.environ.get("STALE_TIMEOUT_S", "120"))


def resume_stage(failed_stage: str | None) -> str:
    """根据失败阶段返回应回退到的排队阶段。

    :param failed_stage: 失败时的活跃阶段。
    :returns: 正确的排队阶段。
    """
    if failed_stage is None:
        return TaskStatus.QUEUED_FOR_TRANS
    mapping = {
        TaskStatus.TRANSCRIBING: TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.ANALYZING: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.RENDERING: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.PUBLISHING: TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.TRANSCRIBED: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.CANDIDATE_CREATED: TaskStatus.QUEUED_FOR_RENDER,
    }
    return mapping.get(failed_stage, TaskStatus.QUEUED_FOR_TRANS)


def recover_stale() -> None:
    """心跳超时的活跃任务回退到排队状态。

    发布任务特殊处理:
    - 已有 UploadAttempt 且状态为 in_progress/reconciliation_required → 不重新排队
    - 仅 PREPARED 或无限 attempt → 允许重新排队
    """
    stale_threshold = now_utc() - timedelta(seconds=_STALE_TIMEOUT_S)

    with get_session() as db:
        stale = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.ANALYZING,
                        TaskStatus.RENDERING,
                        TaskStatus.PUBLISHING,
                    ]
                ),
                SegmentTask.heartbeat_at.is_not(None),
                SegmentTask.heartbeat_at < stale_threshold,
            )
        ).all()

        published_skipped = 0
        for task in stale:
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
                    _logger.warning(
                        "stale_publish_skipped: task=%s clip=%s attempt=%s status=%s",
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

            res = resume_stage(task.failed_stage or task.stage)
            task.stage = res
            task.claimed_by = None
            task.claimed_at = None
            task.heartbeat_at = None
            task.lease_token = None
            task.next_retry_at = None
            db.add(task)

        if stale:
            skipped_msg = f" (跳过 {published_skipped} 个发布任务)" if published_skipped else ""
            _logger.warning("Stale 恢复: 回退 %d 个心跳超时任务。%s", len(stale) - published_skipped, skipped_msg)
        db.commit()


def recover_orphans() -> None:
    """恢复孤立任务: stale 恢复 + 无心跳中间状态回退 + 孤立片段任务创建。"""
    from app.pipeline.stage_result import make_idempotency_key, make_pipeline_key, make_stage_key

    recover_stale()
    with get_session() as db:
        stuck = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.ANALYZING,
                        TaskStatus.RENDERING,
                        TaskStatus.PUBLISHING,
                    ]
                ),
                SegmentTask.heartbeat_at.is_(None),
            )
        ).all()
        for task in stuck:
            res = resume_stage(task.stage)
            task.stage = res
            task.started_at = None
            task.next_retry_at = None
            task.claimed_by = None
            db.add(task)
        if stuck:
            _logger.info("恢复: 回退 %d 个旧格式中间状态任务。", len(stuck))

        existing_ids = {t.segment_id for t in db.exec(select(SegmentTask.segment_id)).all()}
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
                idempotency_key=make_idempotency_key(seg.id, "recorded"),
            )
            db.add(t)
        if orphan_segs:
            _logger.info("恢复: 为 %d 个孤立片段创建任务。", len(orphan_segs))


def recover_pending_clips() -> int:
    """恢复 PENDING 状态的 ClipVariant — 扫描文件系统, 标记 READY 或 FAILED。

    场景:
    - ClipVariant.render_status == QUEUED (PENDING)
    - partial 存在 → 未完成, 标记 FAILED 等待重试
    - formal 存在 → 文件已就位, 标记 DONE (READY)
    - backup 存在 → 非正常状态, 恢复 backup → formal, 标记 FAILED
    - 无任何文件 → 标记 FAILED

    :returns: 恢复的 ClipVariant 数量。
    """
    recovered = 0
    with get_session() as db:
        pending_variants = db.exec(select(ClipVariant).where(ClipVariant.render_status == RenderStatus.QUEUED)).all()

        for var in pending_variants:
            file_path = var.file_path
            formal_exists = file_path and Path(file_path).exists()

            if formal_exists:
                # 文件已就位 — 标记 READY
                var.render_status = RenderStatus.DONE
                db.add(var)
                recovered += 1
                _logger.info(
                    "clip_recovery_mark_ready: variant=%s event=%s path=%s",
                    var.id,
                    var.event_id,
                    file_path,
                )
            elif var.backup_path and Path(var.backup_path).exists():
                # backup 存在 — 恢复 backup, 标记 FAILED
                try:
                    Path(var.backup_path).rename(file_path)
                    _logger.info(
                        "clip_recovery_restore_backup: variant=%s backup=%s -> formal=%s",
                        var.id,
                        var.backup_path,
                        file_path,
                    )
                except OSError:
                    _logger.warning("clip_recovery_restore_failed: variant=%s", var.id)
                var.render_status = RenderStatus.FAILED
                db.add(var)
                recovered += 1
            else:
                # 无文件 — 标记 FAILED
                var.render_status = RenderStatus.FAILED
                db.add(var)
                recovered += 1

        if recovered:
            db.commit()
            _logger.info("clip_recovery: 恢复 %d 个 PENDING ClipVariant", recovered)

    return recovered
