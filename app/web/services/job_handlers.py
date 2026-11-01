"""内置 Web 后台作业处理器。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.sanitize import sanitize_diagnostic

if TYPE_CHECKING:
    from app.jobs.context import JobContext
    from app.jobs.manager import JobManager


def register_job_handlers(manager: JobManager) -> None:
    """向作业管理器注册全部内置处理器。"""
    from app.publishing.job_recovery import recover_upload_job

    manager.register("candidate_render", _render_candidate)
    manager.register("review_rerender", _rerender_review)
    manager.register("collection_render", _render_collection)
    manager.register("clip_upload", _upload_clip, recover=recover_upload_job)
    manager.register("upload_retry", _retry_upload, recover=recover_upload_job)
    manager.register("local_import", _import_recording)
    from app.analysis.editorial_reanalysis import run_editorial_reanalysis

    manager.register("editorial_reanalysis", run_editorial_reanalysis)


def _import_recording(context: JobContext, payload: dict[str, Any]) -> dict[str, object]:
    from app.recording.imports import prepare_import

    return prepare_import(context, str(payload["import_id"]))


def _render_candidate(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.web.services.candidates import approve_candidate_sync

    candidate_id = int(payload["candidate_id"])
    context.report(5, "正在批准候选")
    clip_id = approve_candidate_sync(
        candidate_id,
        reviewed_by=str(payload["reviewed_by"]),
        progress_callback=context.report,
        cancel_check=context.cancelled,
        job_context=context,
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
        output_suffix=f"job_{context.job_id}_{context.attempt}_{payload['version']}",
        progress_callback=context.report,
        cancel_check=context.cancelled,
        render_variants=False,
        job_context=context,
        result_metadata={"version": payload["version"]},
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
        payload["chapter_titles"],
        bool(payload["include_chapter_cards"]),
        progress_callback=context.report,
        cancel_check=context.cancelled,
        job_context=context,
    )
    if result is None:
        raise RuntimeError("合集渲染失败，需至少两个可用成片")
    return {"variant_id": result.id, "file_path": result.file_path, "duration_s": result.duration_s}


def _upload_clip(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.publishing.uploader import enqueue_and_upload

    context.report(10, "正在执行上传预检")
    context.check_cancelled()
    task = enqueue_and_upload(int(payload["clip_id"]), job_attempt=context.token)
    context.check_cancelled()
    if task.status != "success":
        raise RuntimeError(
            f"上传未完成 upload_task_id={task.id} status={task.status}: "
            f"{sanitize_diagnostic(task.last_error or '请核对平台结果')}"
        )
    return {"task_id": task.id, "status": task.status, "error": None}


def _retry_upload(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
    from app.publishing.uploader import process_upload_task

    context.report(10, "正在重新执行上传")
    context.check_cancelled()
    task = process_upload_task(int(payload["upload_task_id"]), job_attempt=context.token)
    context.check_cancelled()
    if task.status != "success":
        raise RuntimeError(
            f"上传未完成 upload_task_id={task.id} status={task.status}: "
            f"{sanitize_diagnostic(task.last_error or '请核对平台结果')}"
        )
    return {"task_id": task.id, "status": task.status, "error": None}
