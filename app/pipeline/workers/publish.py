"""发布阶段 Worker — compute/commit 分离 (V0.1.14)."""

from __future__ import annotations

from pathlib import Path as _Path

from app.db.models import FinalClip, HighlightEvent, ReviewStatus, SegmentTask
from app.db.session import get_session
from app.pipeline.lease import TaskLease, still_owns_lease
from app.pipeline.stage_result import mark_failed, mark_heartbeat


def publish_compute(task_id: int) -> dict:
    """纯发布计算 — 无数据库状态写入。

    :returns: upload task info dict, error dict, 或 remote_result_unknown dict。
    """
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
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

    try:
        from app.publishing.uploader import enqueue_and_upload

        upload_task = enqueue_and_upload(clip_id)
    except Exception as exc:
        error_msg = str(exc).lower()
        if "timeout" in error_msg or "timed out" in error_msg:
            return {"remote_result_unknown": True}
        return {"error": f"PublishError: {exc}", "permanent": False}

    if upload_task is None:
        return {"error": "upload_task 为空", "permanent": False}

    ustatus = upload_task.status
    if ustatus is None or ustatus == "":
        return {"remote_result_unknown": True}

    return {
        "upload_task_id": upload_task.id or 0,
        "upload_status": ustatus,
        "upload_error": upload_task.last_error,
        "remote_id": upload_task.remote_id,
    }


def commit_publish(lease: TaskLease, compute_result: dict) -> None:
    """提交发布结果 — 单一事务 + 租约校验。"""
    import logging

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.warning("stale_result_discarded: task=%s 已失去租约, 丢弃发布结果", lease.task_id)
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        if compute_result.get("remote_result_unknown"):
            _logger.warning("remote_result_unknown: task=%s 上传结果未知, 保持 PUBLISHING 状态", lease.task_id)
            return

        if "error" in compute_result:
            mark_failed(
                task,
                compute_result["error"],
                permanent=compute_result.get("permanent", False),
            )
            db.add(task)
            return

        from app.pipeline.approval import apply_upload_result

        apply_upload_result(
            task_id=lease.task_id,
            upload_task_id=compute_result.get("upload_task_id", 0),
            upload_status=compute_result.get("upload_status", ""),
            upload_error=compute_result.get("upload_error"),
            remote_id=compute_result.get("remote_id"),
        )


def run_publish(lease: TaskLease) -> None:
    """发布阶段入口 — heartbeat → compute → commit。"""
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        if task.clip_id is None:
            mark_failed(task, "PublishError: 任务缺少 clip_id", permanent=True)
            db.add(task)
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = publish_compute(lease.task_id)
    commit_publish(lease, compute_result)
