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

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db.entities import (
    ClipStatus,
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
    "in_progress": {"success", "failed_retryable", "failed_permanent", "remote_result_unknown"},
    "remote_result_unknown": {"reconciliation_required"},
    "reconciliation_required": {"success", "failed_permanent"},
    "failed_retryable": {"prepared"},  # can be retried as new attempt
}


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


def _atomic_claim_upload_task(db: Session, upload_task_id: int, worker_id: str) -> int | None:
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
               AND NOT EXISTS (
                   SELECT 1 FROM upload_tasks other
                   WHERE other.clip_id = upload_tasks.clip_id AND other.id != upload_tasks.id
                   AND other.uploader != 'manual'
                   AND other.status IN ('preparing', 'uploading', 'success', 'reconciliation_required')
               )
               RETURNING publish_generation"""
        ),
        params={"worker_id": worker_id, "task_id": upload_task_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    db.flush()
    return int(row[0]) if row[0] is not None else None


def prepare_upload_attempt(upload_task_id: int, worker_id: str, lease_token: str | None = None) -> dict[str, Any]:
    """为 Web、CLI 和 Worker 原子领取同一上传任务并持久化请求前记录。"""
    from app.publishing.uploader import precheck_clip

    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        upload_task = db.get(UploadTask, upload_task_id)
        if upload_task is None:
            return {"error": "上传任务不存在", "permanent": True}
        clip_id = upload_task.clip_id
        success = db.exec(
            select(UploadAttempt).where(UploadAttempt.clip_id == clip_id, UploadAttempt.status == UploadStatus.SUCCESS)
        ).first()
        if success is not None:
            return {
                "attempt_id": success.id,
                "attempt_token": success.attempt_token,
                "publish_generation": success.publish_generation,
                "clip_id": clip_id,
                "ready": True,
                "already_success": True,
                "remote_id": success.remote_id,
            }
        blocked = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.clip_id == clip_id,
                UploadAttempt.status.in_(
                    ["prepared", "in_progress", "remote_result_unknown", "reconciliation_required"]
                ),
            )
        ).first()
        if blocked is not None:
            return {"error": "上传执行中或需要核对平台结果，不能重复发起", "permanent": False}
        if upload_task.status not in {"queued", "failed", "failed_retryable"}:
            return {"error": f"上传任务不能领取: {upload_task.status}", "permanent": True}
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            upload_task.status = "failed_permanent"
            upload_task.last_error = "切片不存在"
            db.add(upload_task)
            return {"error": "切片不存在", "permanent": True}
        precheck = precheck_clip(clip_id)
        if not precheck.ok:
            upload_task.status = UploadStatus.SKIPPED
            upload_task.last_error = ";".join(precheck.reasons)
            db.add(upload_task)
            return {"error": upload_task.last_error, "permanent": True}
        generation = _atomic_claim_upload_task(db, upload_task_id, worker_id)
        if generation is None:
            return {"error": "同一成片的上传已被占用", "permanent": False}
        attempt = UploadAttempt(
            upload_task_id=upload_task_id,
            publish_generation=generation,
            attempt_token=_generate_attempt_token(),
            platform="bilibili",
            clip_id=clip_id,
            status="prepared",
            started_at=datetime.now(UTC),
            request_fingerprint=_generate_stable_fingerprint(clip),
            created_by_worker=worker_id,
            lease_token=lease_token,
        )
        db.add(attempt)
        db.flush()
        return {
            "attempt_id": attempt.id,
            "attempt_token": attempt.attempt_token,
            "publish_generation": generation,
            "clip_id": clip_id,
            "ready": True,
        }


def prepare_publish_attempt(lease: TaskLease) -> dict[str, Any]:
    """验证流水线租约和审核状态，再进入共用上传准备流程。"""
    from app.publishing.uploader import enqueue_upload, get_uploader

    with get_session() as db:
        if not still_owns_lease(db, lease):
            return {"error": "lease lost before prepare", "permanent": False}
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.clip_id is None:
            return {"error": "任务不存在或缺少 clip_id", "permanent": True}
        event = db.get(HighlightEvent, task.event_id) if task.event_id else None
        if event is None or event.review_status not in ReviewStatus.POSITIVE:
            return {"error": "Event 未批准或不存在", "permanent": True}
        clip_id = task.clip_id
        upload_task = db.exec(
            select(UploadTask)
            .where(UploadTask.clip_id == clip_id, UploadTask.uploader == get_uploader().name)
            .order_by(UploadTask.id)
        ).first()
    if upload_task is None:
        upload_task = enqueue_upload(clip_id)
    if upload_task.id is None:
        return {"error": "上传任务缺少 ID", "permanent": True}
    return prepare_upload_attempt(upload_task.id, lease.worker_id, lease.lease_token)


def execute_remote_upload(attempt_token: str) -> dict[str, Any]:
    """原子标记一次请求已发出，只调用上传器一次，保留不确定结果。"""
    from sqlalchemy import DateTime, bindparam, text

    from app.publishing.uploader import classify_upload_error, get_uploader

    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == attempt_token)).first()
        if attempt is None:
            return {"error": "attempt not found", "permanent": True}
        base = {
            "attempt_token": attempt_token,
            "publish_generation": attempt.publish_generation,
            "upload_task_id": attempt.upload_task_id,
            "clip_id": attempt.clip_id,
        }
        if attempt.status == "success":
            return {**base, "outcome": "success", "remote_id": attempt.remote_id, "already_completed": True}
        claimed = db.exec(
            text(
                "UPDATE upload_attempts SET status='in_progress', started_at=:now "
                "WHERE id=:id AND status='prepared' RETURNING id"
            ).bindparams(bindparam("now", type_=DateTime(timezone=True))),
            params={"id": attempt.id, "now": datetime.now(UTC)},
        ).first()
        if claimed is None:
            return {**base, "error": f"attempt cannot execute: {attempt.status}", "permanent": False}
        clip = db.get(FinalClip, attempt.clip_id)
        if clip is None:
            return {**base, "outcome": "failed_permanent", "error_message": "切片不存在"}
        payload = {"id": clip.id, "file_path": clip.file_path, "title": clip.title, "description": clip.description}
        upload_task = db.get(UploadTask, attempt.upload_task_id)
        if upload_task is not None:
            upload_task.status = UploadStatus.UPLOADING
            upload_task.attempts += 1
            upload_task.updated_at = datetime.now(UTC)
            db.add(upload_task)
    uploader = get_uploader()
    if uploader.name == "manual":
        return {**base, "outcome": "failed_permanent", "error_message": "自动上传已关闭，请使用手动发布"}
    try:
        result = uploader.upload(payload)
    except Exception as exc:  # noqa: BLE001 - 第三方上传器边界必须保留未知结果，不能重发
        classified = classify_upload_error(exc)
        return {
            **base,
            "outcome": classified.outcome,
            "error_type": classified.error_type,
            "error_message": classified.error_message,
            "request_may_have_been_sent": classified.request_may_have_been_sent,
        }
    outcome = result.outcome or ("success" if result.success else "remote_result_unknown")
    return {
        **base,
        "outcome": outcome,
        "remote_id": result.remote_id,
        "error_message": None if result.success else result.message,
        "request_may_have_been_sent": result.request_may_have_been_sent,
    }


def commit_publish_result(attempt_token: str, publish_generation: int, compute_result: dict[str, Any]) -> None:
    """按尝试令牌及代数持久化远程结果，数据库提交失败时记录成功日志。"""
    outcome = compute_result.get("outcome")
    if outcome is None or compute_result.get("publish_generation", publish_generation) != publish_generation:
        return
    with get_session() as db:
        attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.attempt_token == attempt_token,
                UploadAttempt.publish_generation == publish_generation,
            )
        ).first()
        if attempt is None:
            return
        if attempt.status == "success":
            outcome = "success"
        elif not _validate_transition(attempt.status, outcome):
            return
        if outcome == "failed_retryable" and compute_result.get("request_may_have_been_sent", False):
            outcome = "remote_result_unknown"
        target = UploadStatus.RECONCILIATION_REQUIRED if outcome == "remote_result_unknown" else outcome
        now = datetime.now(UTC)
        attempt.status = target
        attempt.finished_at = now
        attempt.remote_id = attempt.remote_id or compute_result.get("remote_id")
        attempt.remote_url = attempt.remote_url or compute_result.get("remote_url")
        attempt.error_type = compute_result.get("error_type")
        attempt.error_message = compute_result.get("error_message")
        db.add(attempt)
        upload_task = db.get(UploadTask, attempt.upload_task_id)
        if upload_task is not None and upload_task.publish_generation == publish_generation:
            upload_task.status = target
            upload_task.remote_id = attempt.remote_id
            upload_task.last_error = attempt.error_message
            upload_task.claimed_by = None
            upload_task.updated_at = now
            db.add(upload_task)
        if outcome == "success":
            clip = db.get(FinalClip, attempt.clip_id)
            if clip is not None:
                clip.status = ClipStatus.PUBLISHED
                db.add(clip)
        journal_data = {
            "attempt_token": attempt_token,
            "publish_generation": publish_generation,
            "upload_task_id": attempt.upload_task_id,
            "clip_id": attempt.clip_id,
            "remote_id": attempt.remote_id or "",
            "remote_url": attempt.remote_url,
        }
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            if outcome != "success":
                raise
            from app.publishing.journal import write_remote_success

            if not write_remote_success(**journal_data):
                _logger.critical("publish_result_not_saved: attempt=%s 必须人工核对平台结果", attempt_token)
                raise
            _logger.exception("publish_db_commit_failed: attempt=%s 已保留成功日志", attempt_token)


def commit_publish_and_advance(
    lease: TaskLease,
    attempt_token: str,
    publish_generation: int,
    compute_result: dict[str, Any],
) -> None:
    """仅根据已持久化的尝试结果推进仍由本租约拥有的任务。"""
    with get_session() as db:
        if not still_owns_lease(db, lease):
            return
        task = db.get(SegmentTask, lease.task_id)
        attempt = db.exec(
            select(UploadAttempt).where(
                UploadAttempt.attempt_token == attempt_token,
                UploadAttempt.publish_generation == publish_generation,
            )
        ).first()
        if task is None or attempt is None:
            return
        if attempt.status == "success":
            mark_completed(task, 0)
            enqueue_next(task, TaskStatus.COMPLETED)
        elif attempt.status == "reconciliation_required":
            enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
            task.last_error = "平台结果未知，必须核对后确认，禁止自动重投"
        elif attempt.status in {"failed_permanent", "failed_retryable"}:
            mark_failed(task, attempt.error_message or "publish failed", permanent=attempt.status == "failed_permanent")
        else:
            return
        task.claimed_by = None
        task.lease_token = None
        db.add(task)


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
        # prepare 失败 — 不走空 token 路径; 直接标记 task failed
        _logger.warning("publish_prepare_failed: %s", prepared["error"])
        with get_session() as db:
            task = db.get(SegmentTask, lease.task_id)
            if task is not None and still_owns_lease(db, lease):
                mark_failed(task, prepared["error"], permanent=prepared.get("permanent", True))
                db.add(task)
                db.commit()
        return
    if prepared.get("already_success"):
        token = prepared.get("attempt_token", "")
        gen = prepared.get("publish_generation", 0)
        commit_publish_result(token, gen, {"outcome": "success", "remote_id": prepared.get("remote_id")})
        commit_publish_and_advance(lease, token, gen, {"outcome": "success"})
        return

    # execute
    token = prepared.get("attempt_token", "")
    gen = prepared.get("publish_generation", 0)
    result = execute_remote_upload(token)
    commit_publish_result(token, gen, result)
    commit_publish_and_advance(lease, token, gen, result)
