"""发布阶段 Worker — compute/commit 真正分离。

prepare_publish_attempt: 原子占用 UploadTask + 创建 PREPARED Attempt (generation-keys)
execute_remote_upload: 标记 IN_PROGRESS + 执行远程上传
commit_publish_result: 按 Attempt Token + Generation 提交结果 (状态机校验)
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import Any

from sqlmodel import select

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
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed

_logger = logging.getLogger(__name__)

# ── Status transition validation ────────────────────────────

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "prepared": {"in_progress", "cancelled", "failed_retryable"},
    "in_progress": {"success", "failed_permanent", "remote_result_unknown"},
    "remote_result_unknown": {"reconciliation_required"},
    "reconciliation_required": {"success", "failed_permanent"},
    "failed_retryable": {"prepared"},  # can be retried as new attempt
}

_TERMINAL_STATUSES = {"success", "failed_permanent", "cancelled", "reconciliation_required"}


def _validate_transition(current: str, target: str) -> bool:
    """验证状态转换是否合法。

    :param current: 当前状态。
    :param target: 目标状态。
    :returns: True 表示合法。
    """
    allowed = _VALID_TRANSITIONS.get(current, set())
    return target in allowed


def _generate_attempt_token() -> str:
    """生成追踪令牌 (仅用于追踪, 不承担业务排他)。"""
    return uuid.uuid4().hex[:16]


def _generate_stable_fingerprint(clip: FinalClip) -> str:
    """生成稳定请求指纹 (不含随机值/时间/Worker)。

    包含: content_hash + file_size + title + description + tags。
    """
    parts = [
        clip.content_hash or "",
        str(clip.file_path and _Path(clip.file_path).stat().st_size or 0),
        clip.title or "",
        clip.description or "",
        clip.tags_json or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _atomic_claim_upload_task(db, upload_task_id: int, worker_id: str) -> int | None:
    """原子占用 UploadTask — 使用条件 SQL UPDATE。

    只有 status IN (QUEUED, FAILED_RETRYABLE) 且 claimed_by IS NULL
    的 upload_task 才能被占用。

    :param db: SQLModel Session。
    :param upload_task_id: UploadTask ID。
    :param worker_id: Worker ID。
    :returns: 新的 publish_generation, 或 None (占用失败)。
    """
    from sqlalchemy import text

    result = db.exec(
        text(
            """UPDATE upload_tasks SET
               status = 'preparing',
               claimed_by = :worker_id,
               publish_generation = publish_generation + 1
               WHERE id = :task_id
               AND status IN ('queued', 'failed', 'failed_retryable')
               AND (claimed_by IS NULL OR claimed_by = '')
               RETURNING publish_generation"""
        ),
        {"worker_id": worker_id, "task_id": upload_task_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    db.flush()
    return int(row[0]) if row[0] is not None else None


def prepare_publish_attempt(lease: TaskLease) -> dict[str, Any]:
    """准备阶段 — 占 UploadTask → 创建 UploadAttempt (PREPARED)。

    在请求发出前持久化 attempt, 确保崩溃后仍可追踪。

    :param lease: 任务租约。
    :returns: {"attempt_id", "attempt_token", "publish_generation", "ready": True} 或 {"error": ...}。
    """
    worker_id = lease.worker_id

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

        # 检查是否有 SUCCESS attempt — 直接复用
        success_attempt = db.exec(
            select(UploadAttempt).where(UploadAttempt.clip_id == clip_id, UploadAttempt.status == UploadStatus.SUCCESS)
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

        # 检查是否有 RECONCILIATION_REQUIRED — 禁止新上传
        reconciliation_attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.clip_id == clip_id,
                UploadAttempt.status == UploadStatus.RECONCILIATION_REQUIRED,
            )
        ).first()
        if reconciliation_attempt is not None:
            _logger.warning("publish_blocked_by_reconciliation: clip=%s", clip_id)
            return {"error": "已有 RECONCILIATION_REQUIRED attempt, 禁止自动重试", "permanent": True}

        # 确保 UploadTask 存在
        upload_task = db.exec(select(UploadTask).where(UploadTask.clip_id == clip_id)).first()
        if upload_task is None:
            upload_task = UploadTask(clip_id=clip_id, uploader="auto", status=UploadStatus.QUEUED)
            db.add(upload_task)
            db.flush()
            db.refresh(upload_task)

        ut_id = upload_task.id
        if ut_id is None:
            return {"error": "UploadTask ID is None after flush", "permanent": True}

        # 原子占用
        generation = _atomic_claim_upload_task(db, ut_id, worker_id)
        if generation is None:
            return {"error": "UploadTask 已被其他 Worker 占用", "permanent": False}

        # 生成 attempt
        attempt_token = _generate_attempt_token()
        request_fingerprint = _generate_stable_fingerprint(clip) if clip else ""

        attempt = UploadAttempt(
            upload_task_id=ut_id,
            publish_generation=generation,
            attempt_token=attempt_token,
            platform="bilibili",
            account_id=None,
            clip_id=clip_id,
            status="prepared",
            started_at=datetime.now(UTC),
            request_fingerprint=request_fingerprint,
            created_by_worker=worker_id,
            lease_token=lease.lease_token,
        )
        db.add(attempt)
        db.flush()
        db.refresh(attempt)

        db.commit()

        _logger.info(
            "publish_attempt_prepared: attempt=%s clip=%s gen=%s token=%s",
            attempt.id,
            clip_id,
            generation,
            attempt_token,
        )

        return {
            "attempt_id": attempt.id,
            "attempt_token": attempt_token,
            "publish_generation": generation,
            "clip_id": clip_id,
            "ready": True,
        }


def execute_remote_upload(attempt_token: str) -> dict[str, Any]:
    """执行远程上传 — 状态机校验 → 标记 IN_PROGRESS → 执行 → 返回结构化结果。

    :param attempt_token: UploadAttempt 追踪令牌。
    :returns: 结构化结果 dict, 含 outcome 和 request_may_have_been_sent。
    """
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == attempt_token)).first()
        if attempt is None:
            return {"error": "attempt not found", "permanent": True}

        if attempt.status == "success":
            _logger.info("publish_skip_already_success: attempt=%s", attempt_token)
            return {
                "attempt_id": attempt.id,
                "attempt_token": attempt_token,
                "publish_generation": attempt.publish_generation,
                "outcome": "success",
                "remote_id": attempt.remote_id,
                "already_completed": True,
            }

        if attempt.status in _TERMINAL_STATUSES:
            if attempt.status == "reconciliation_required":
                _logger.warning("publish_skip_reconciliation: attempt=%s", attempt_token)
                return {"error": "attempt in RECONCILIATION_REQUIRED", "permanent": True}
            return {"error": f"attempt in terminal state: {attempt.status}", "permanent": True}

        # 验证状态转换
        if not _validate_transition(attempt.status, "in_progress"):
            return {"error": f"非法状态转换: {attempt.status} -> in_progress", "permanent": True}

        # 标记 IN_PROGRESS
        attempt.status = "in_progress"
        attempt.started_at = datetime.now(UTC)
        db.add(attempt)
        db.commit()
        clip_id = attempt.clip_id
        generation = attempt.publish_generation

    # 执行远程上传
    request_may_have_been_sent = False
    try:
        from app.publishing.uploader import enqueue_and_upload

        upload_task = enqueue_and_upload(clip_id)
    except Exception as exc:
        error_msg = str(exc).lower()
        request_may_have_been_sent = any(
            kw in error_msg for kw in ("timeout", "timed out", "reset", "broken pipe", "eof", "tls")
        )
        if request_may_have_been_sent:
            return {
                "attempt_token": attempt_token,
                "publish_generation": generation,
                "outcome": "remote_result_unknown",
                "error_type": "network_error",
                "error_message": str(exc),
                "request_may_have_been_sent": True,
            }
        return {
            "attempt_token": attempt_token,
            "publish_generation": generation,
            "outcome": "failed_permanent" if "permission" in error_msg else "failed_retryable",
            "error_type": "exception",
            "error_message": str(exc),
            "request_may_have_been_sent": False,
        }

    if upload_task is None:
        return {
            "attempt_token": attempt_token,
            "publish_generation": generation,
            "outcome": "remote_result_unknown",
            "error_type": "no_result",
            "error_message": "upload_task 为空",
            "request_may_have_been_sent": True,
        }

    ustatus = upload_task.status or ""
    if ustatus in ("", "queued", "uploading"):
        return {
            "attempt_token": attempt_token,
            "publish_generation": generation,
            "outcome": "remote_result_unknown",
            "error_type": "incomplete_status",
            "error_message": f"upload_task status={ustatus}",
            "request_may_have_been_sent": True,
        }

    return {
        "attempt_token": attempt_token,
        "publish_generation": generation,
        "upload_task_id": upload_task.id or 0,
        "outcome": "success" if ustatus == UploadStatus.SUCCESS else "failed_retryable",
        "upload_status": ustatus,
        "upload_error": upload_task.last_error,
        "remote_id": upload_task.remote_id,
    }


def commit_publish_result(
    attempt_token: str,
    publish_generation: int,
    compute_result: dict[str, Any],
) -> None:
    """提交阶段 — 按 Attempt Token + Generation 提交远程结果。

    不再依赖 SegmentTask lease 来持久化远程结果。
    远程一旦发出, 结果提交不应被 lease 丢失阻断。

    :param attempt_token: UploadAttempt 追踪令牌。
    :param publish_generation: Attempt 发布代数。
    :param compute_result: execute_remote_upload 的输出。
    """
    outcome = compute_result.get("outcome", "unknown")
    gen = compute_result.get("publish_generation", publish_generation)

    with get_session() as db:
        # 按 token + generation 查询 attempt
        attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.attempt_token == attempt_token,
                UploadAttempt.publish_generation == gen,
            )
        ).first()
        if attempt is None:
            _logger.warning("commit_publish: attempt not found token=%s gen=%s", attempt_token, gen)
            return

        now = datetime.now(UTC)
        remote_id = compute_result.get("remote_id")

        if outcome == "success":
            if attempt.status == "success":
                _logger.info("publish_already_success: attempt=%s", attempt_token)
                return
            if not _validate_transition(attempt.status, "success"):
                _logger.error("publish_invalid_transition: attempt=%s %s->success", attempt_token, attempt.status)
                return

            attempt.status = "success"
            attempt.finished_at = now
            attempt.remote_id = remote_id
            db.add(attempt)

            upload_task = db.get(UploadTask, attempt.upload_task_id)
            if upload_task is not None:
                upload_task.status = UploadStatus.SUCCESS
                upload_task.remote_id = remote_id
                db.add(upload_task)

            _logger.info(
                "publish_commit_success: attempt=%s clip=%s remote=%s", attempt_token, attempt.clip_id, remote_id
            )

        elif outcome == "remote_result_unknown":
            if not _validate_transition(attempt.status, "remote_result_unknown"):
                _logger.error("publish_invalid_transition: %s->unknown from %s", attempt_token, attempt.status)
                return

            attempt.status = "remote_result_unknown"
            db.add(attempt)
            db.flush()
            # 自动转换到 RECONCILIATION_REQUIRED
            attempt.status = UploadStatus.RECONCILIATION_REQUIRED
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type", "unknown")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)
            _logger.warning("publish_commit_reconciliation: attempt=%s clip=%s", attempt_token, attempt.clip_id)

        elif outcome == "failed_retryable":
            if not _validate_transition(attempt.status, "failed_retryable"):
                return
            attempt.status = "failed_retryable"
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)

        elif outcome == "failed_permanent":
            if not _validate_transition(attempt.status, "failed_permanent"):
                return
            attempt.status = "failed_permanent"
            attempt.finished_at = now
            attempt.error_type = compute_result.get("error_type")
            attempt.error_message = compute_result.get("error_message")
            db.add(attempt)

        else:
            attempt.status = "failed_permanent"
            attempt.finished_at = now
            attempt.error_message = f"unknown outcome: {outcome}"
            db.add(attempt)

        db.commit()


def commit_publish_and_advance(
    lease: TaskLease,
    attempt_token: str,
    publish_generation: int,
    compute_result: dict[str, Any],
) -> None:
    """提交并推进 Task — 在 commit_publish_result 完成后推进 SegmentTask。

    此函数仍要求 lease 有效 (用于推进 Task),
    但结果持久化已在 commit_publish_result 中独立完成。

    :param lease: 任务租约 (用于推进 Task)。
    """
    outcome = compute_result.get("outcome", "unknown")

    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.info("publish_task_advance_skipped: lease lost, 结果已持久化")
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        if outcome == "success":
            mark_completed(task, 0)
            enqueue_next(task, TaskStatus.COMPLETED)
            db.add(task)
        elif outcome == "failed_permanent":
            mark_failed(task, compute_result.get("error_message", "publish failed"), permanent=True)
            db.add(task)
        # remote_result_unknown / failed_retryable → 不推进 Task

        db.commit()


# ── run 入口 ───────────────────────────────────────────────


def run_publish(lease: TaskLease) -> None:
    """发布阶段入口 — prepare → execute → commit。

    心跳由 scheduler 的 heartbeat thread 管理。
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
        # prepare 失败 — 不走空 token 路径
        _logger.warning("publish_prepare_failed: %s", prepared["error"])
        commit_publish_result(
            "prepare-failed-" + uuid.uuid4().hex[:8],
            -1,
            {
                "outcome": "failed_permanent",
                "error_message": prepared["error"],
            },
        )
        return
    if prepared.get("already_success"):
        token = prepared.get("attempt_token", "")
        gen = prepared.get("publish_generation", 0)
        commit_publish_result(token, gen, {"outcome": "success", "remote_id": prepared.get("remote_id")})
        return

    # execute
    token = prepared.get("attempt_token", "")
    gen = prepared.get("publish_generation", 0)
    result = execute_remote_upload(token)
    commit_publish_result(token, gen, result)
    commit_publish_and_advance(lease, token, gen, result)
