"""发布阶段 Worker — compute/commit 真正分离。

prepare_publish_attempt: 持久化前验证 + 创建 UploadAttempt (PREPARED)
execute_remote_upload: 标记 IN_PROGRESS + 执行远程上传
commit_publish_result: 租约保护下根据远程结果更新 attempt 和任务状态

远程结果不确定时持久化 REMOTE_RESULT_UNKNOWN, 禁止自动重试。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import Any

from app.db.models import (
    FinalClip,
    HighlightEvent,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
    UploadAttempt,
    UploadStatus,
    UploadTask,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease, still_owns_lease
from app.pipeline.stage_result import mark_failed

# mark_heartbeat 由 scheduler heartbeat thread 管理, 不在 run_* 中重复写入

_logger = logging.getLogger(__name__)


def _generate_attempt_token(clip_id: int, worker_id: str) -> str:
    """生成上传尝试幂等令牌。"""
    raw = f"{clip_id}:{worker_id}:{uuid.uuid4().hex[:8]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _generate_request_fingerprint(clip_id: int, file_path: str) -> str:
    """生成请求指纹, 用于去重检测。"""
    if file_path and _Path(file_path).exists():
        size = _Path(file_path).stat().st_size
        return hashlib.sha1(f"{clip_id}:{size}".encode()).hexdigest()[:12]
    return hashlib.sha1(str(clip_id).encode()).hexdigest()[:12]


def prepare_publish_attempt(lease: TaskLease) -> dict[str, Any]:
    """准备阶段 — 持久化前验证 + 创建 UploadAttempt (PREPARED)。

    在请求发出前持久化 attempt, 确保崩溃后仍可追踪。

    :param lease: 任务租约。
    :returns: {"attempt_id", "attempt_token", "clip_id", "ready": True} 或 {"error": ...}。
    """
    with get_session() as db:
        if not still_owns_lease(db, lease):
            return {"error": "lease lost before prepare", "permanent": False}

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return {"error": "task not found", "permanent": True}
        clip_id = task.clip_id
        event_id = task.event_id
        if clip_id is None:
            return {"error": "任务缺少 clip_id", "permanent": True}

        event = db.get(HighlightEvent, event_id) if event_id else None
        if event is None or event.review_status not in ReviewStatus.POSITIVE:
            return {"error": "Event 未批准或不存在", "permanent": True}

        clip = db.get(FinalClip, clip_id)
        if clip is None:
            return {"error": f"FinalClip {clip_id} 不存在", "permanent": True}
        if not clip.file_path or not _Path(clip.file_path).exists():
            return {"error": "输出文件缺失", "permanent": True}

        # 检查是否已有 SUCCESS attempt — 直接复用
        from sqlmodel import select

        success_attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.clip_id == clip_id,
                UploadAttempt.status == "success",
            )
        ).first()
        if success_attempt is not None:
            _logger.info("publish_reuse_success: clip=%s attempt=%s", clip_id, success_attempt.attempt_token)
            return {
                "attempt_id": success_attempt.id,
                "attempt_token": success_attempt.attempt_token,
                "clip_id": clip_id,
                "ready": True,
                "already_success": True,
                "remote_id": success_attempt.remote_id,
            }

        # 检查是否有 RECONCILIATION_REQUIRED attempt — 禁止新上传
        reconciliation_attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.clip_id == clip_id,
                UploadAttempt.status == UploadStatus.RECONCILIATION_REQUIRED,
            )
        ).first()
        if reconciliation_attempt is not None:
            _logger.warning(
                "publish_blocked_by_reconciliation: clip=%s attempt=%s",
                clip_id,
                reconciliation_attempt.attempt_token,
            )
            return {
                "error": "已有 RECONCILIATION_REQUIRED attempt, 禁止自动重试",
                "permanent": True,
            }

        # 确保 UploadTask 存在
        upload_task = db.exec(select(UploadTask).where(UploadTask.clip_id == clip_id)).first()
        if upload_task is None:
            upload_task = UploadTask(
                clip_id=clip_id,
                uploader="auto",
                status=UploadStatus.QUEUED,
            )
            db.add(upload_task)
            db.flush()
            db.refresh(upload_task)

        # 生成 attempt 令牌和请求指纹
        worker_id = lease.worker_id if hasattr(lease, "worker_id") else "unknown"
        attempt_token = _generate_attempt_token(clip_id, worker_id)
        request_fingerprint = _generate_request_fingerprint(clip_id, clip.file_path or "")

        attempt = UploadAttempt(
            upload_task_id=upload_task.id or 0,
            attempt_token=attempt_token,
            platform="bilibili",
            account_id=None,
            clip_id=clip_id,
            status="prepared",
            request_fingerprint=request_fingerprint,
            created_by_worker=worker_id,
            lease_token=lease.lease_token,
        )
        db.add(attempt)
        db.flush()
        db.refresh(attempt)

        db.commit()

        _logger.info(
            "publish_attempt_prepared: attempt=%s clip=%s token=%s",
            attempt.id,
            clip_id,
            attempt_token,
        )

        return {
            "attempt_id": attempt.id,
            "attempt_token": attempt_token,
            "clip_id": clip_id,
            "ready": True,
        }


def execute_remote_upload(attempt_token: str) -> dict[str, Any]:
    """执行远程上传 — 标记 IN_PROGRESS → 执行 → 返回结果。

    :param attempt_token: UploadAttempt 的幂等令牌。
    :returns: 结构化结果 dict。
    """
    with get_session() as db:
        from sqlmodel import select

        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == attempt_token)).first()
        if attempt is None:
            return {"error": "attempt not found", "permanent": True}

        if attempt.status == "success":
            _logger.info("publish_skip_already_success: attempt=%s", attempt_token)
            return {
                "attempt_id": attempt.id,
                "attempt_token": attempt_token,
                "outcome": "success",
                "remote_id": attempt.remote_id,
                "already_completed": True,
            }

        if attempt.status == UploadStatus.RECONCILIATION_REQUIRED:
            _logger.warning("publish_skip_reconciliation: attempt=%s", attempt_token)
            return {"error": "attempt in RECONCILIATION_REQUIRED", "permanent": True}

        # 标记 IN_PROGRESS
        attempt.status = "in_progress"
        attempt.started_at = datetime.now(UTC)
        db.add(attempt)
        db.commit()
        clip_id = attempt.clip_id

    # 执行远程上传
    try:
        from app.publishing.uploader import enqueue_and_upload

        upload_task = enqueue_and_upload(clip_id)
    except Exception as exc:
        error_msg = str(exc).lower()
        if "timeout" in error_msg or "timed out" in error_msg:
            return {
                "attempt_token": attempt_token,
                "outcome": "remote_result_unknown",
                "error_type": "timeout",
                "error_message": str(exc),
            }
        return {
            "attempt_token": attempt_token,
            "outcome": "failed_permanent" if "permission" in error_msg else "failed_retryable",
            "error_type": "exception",
            "error_message": str(exc),
        }

    if upload_task is None:
        return {
            "attempt_token": attempt_token,
            "outcome": "remote_result_unknown",
            "error_type": "no_result",
            "error_message": "upload_task 为空",
        }

    ustatus = upload_task.status
    if ustatus is None or ustatus == "":
        return {
            "attempt_token": attempt_token,
            "outcome": "remote_result_unknown",
            "error_type": "empty_status",
            "error_message": "upload_task status empty",
        }

    return {
        "attempt_token": attempt_token,
        "upload_task_id": upload_task.id or 0,
        "outcome": (
            "success"
            if ustatus == UploadStatus.SUCCESS
            else "failed_retryable"
            if ustatus in (UploadStatus.QUEUED, UploadStatus.UPLOADING, UploadStatus.FAILED)
            else "failed_permanent"
        ),
        "upload_status": ustatus,
        "upload_error": upload_task.last_error,
        "remote_id": upload_task.remote_id,
    }


def commit_publish_result(lease: TaskLease, attempt_token: str, compute_result: dict[str, Any]) -> None:
    """提交阶段 — 租约校验后持久化 attempt 结果 + 推进 Task。

    :param lease: 任务租约。
    :param attempt_token: UploadAttempt 的幂等令牌。
    :param compute_result: execute_remote_upload 的输出。
    """
    outcome = compute_result.get("outcome", "unknown")

    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.warning("stale_result_discarded: publish task=%s 已失去租约", lease.task_id)
            return

        from sqlmodel import select

        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == attempt_token)).first()
        if attempt is None:
            _logger.error("commit_publish: attempt not found token=%s", attempt_token)
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        now = datetime.now(UTC)

        if outcome == "success":
            attempt.status = "success"
            attempt.finished_at = now
            attempt.remote_id = compute_result.get("remote_id")
            db.add(attempt)

            # 更新 UploadTask
            upload_task = db.get(UploadTask, attempt.upload_task_id)
            if upload_task is not None:
                upload_task.status = UploadStatus.SUCCESS
                upload_task.remote_id = compute_result.get("remote_id")
                db.add(upload_task)

            from app.pipeline.stage_result import enqueue_next, mark_completed

            mark_completed(task, 0)
            enqueue_next(task, TaskStatus.COMPLETED)
            db.add(task)
            _logger.info("publish_commit_success: task=%s clip=%s", lease.task_id, attempt.clip_id)

        elif outcome == "remote_result_unknown":
            attempt.status = UploadStatus.RECONCILIATION_REQUIRED
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type", "unknown")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)

            # 不推进任务状态 — 等待人工对冲
            _logger.warning(
                "publish_commit_reconciliation: task=%s clip=%s attempt=%s",
                lease.task_id,
                attempt.clip_id,
                attempt_token,
            )

        elif outcome == "failed_retryable":
            attempt.status = "failed_retryable"
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)

            mark_failed(
                task,
                compute_result.get("error_message", "publish failed (retryable)"),
                permanent=False,
            )
            db.add(task)

        elif outcome == "failed_permanent":
            attempt.status = "failed_permanent"
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)

            mark_failed(
                task,
                compute_result.get("error_message", "publish failed (permanent)"),
                permanent=True,
            )
            db.add(task)

        else:
            attempt.status = "failed_permanent"
            attempt.finished_at = now
            attempt.error_message = f"unknown outcome: {outcome}"
            db.add(attempt)

            mark_failed(task, f"unknown publish outcome: {outcome}", permanent=True)
            db.add(task)

        db.commit()


def run_publish(lease: TaskLease) -> None:
    """发布阶段入口 — prepare → execute → commit。

    心跳由 scheduler 的 heartbeat thread 管理, 不在 run_* 中重复写入。
    """
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        if task.clip_id is None:
            mark_failed(task, "PublishError: 任务缺少 clip_id", permanent=True)
            db.add(task)
            db.commit()
            return

    # prepare
    prepared = prepare_publish_attempt(lease)
    if "error" in prepared:
        commit_publish_result(lease, "", {"outcome": "failed_permanent", "error_message": prepared["error"]})
        return
    if prepared.get("already_success"):
        # 已有 SUCCESS, 跳过
        commit_publish_result(
            lease,
            prepared["attempt_token"],
            {"outcome": "success", "remote_id": prepared.get("remote_id")},
        )
        return

    # execute
    result = execute_remote_upload(prepared["attempt_token"])
    commit_publish_result(lease, prepared["attempt_token"], result)
