"""内置 Web 后台作业处理器。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.web.services.background_jobs import JobContext, WebJobManager


def register_job_handlers(manager: WebJobManager) -> None:
    """向作业管理器注册全部内置处理器。"""
    manager.register("candidate_render", _render_candidate)
    manager.register("review_rerender", _rerender_review)
    manager.register("collection_render", _render_collection)
    manager.register("clip_upload", _upload_clip)
    manager.register("upload_retry", _retry_upload)


def _render_candidate(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.web.services.candidates import approve_candidate_sync

    candidate_id = int(payload["candidate_id"])
    context.report(5, "正在批准候选")
    clip_id = approve_candidate_sync(
        candidate_id,
        progress_callback=context.report,
        cancel_check=context.cancelled,
    )
    if clip_id is None:
        raise RuntimeError("候选出片失败")
    return {"candidate_id": candidate_id, "clip_id": clip_id}


def _rerender_review(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.pipeline.orchestrator import produce_clip

    candidate_id = int(payload["candidate_id"])
    context.report(5, "正在准备审核版本")
    clip = produce_clip(
        candidate_id,
        auto_upload=False,
        start_ts=datetime.fromisoformat(str(payload["start_ts"])),
        end_ts=datetime.fromisoformat(str(payload["end_ts"])),
        output_suffix=str(payload["version"]),
        progress_callback=context.report,
        cancel_check=context.cancelled,
        render_variants=False,
    )
    if clip is None:
        raise RuntimeError("审核版本渲染失败")
    return {
        "candidate_id": candidate_id,
        "clip_id": clip.id,
        "file_path": clip.file_path,
        "version": payload["version"],
    }


def _render_collection(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.pipeline.collection import render_collection

    context.report(5, "正在准备合集素材")
    result = render_collection(
        int(payload["topic_id"]),
        [int(value) for value in payload["event_ids"]],
        payload.get("chapter_titles"),
        bool(payload.get("include_chapter_cards", True)),
        progress_callback=context.report,
        cancel_check=context.cancelled,
    )
    if result is None:
        raise RuntimeError("合集渲染失败，需至少两个可用成片")
    return {"variant_id": result.id, "file_path": result.file_path, "duration_s": result.duration_s}


def _upload_clip(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.publishing.uploader import enqueue_and_upload

    context.report(10, "正在执行上传预检")
    context.check_cancelled()
    task = enqueue_and_upload(int(payload["clip_id"]))
    context.check_cancelled()
    return {"task_id": task.id, "status": task.status, "error": task.last_error}


def _retry_upload(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.publishing.uploader import process_upload_task

    context.report(10, "正在重新执行上传")
    context.check_cancelled()
    task = process_upload_task(int(payload["upload_task_id"]))
    context.check_cancelled()
    return {"task_id": task.id, "status": task.status, "error": task.last_error}
