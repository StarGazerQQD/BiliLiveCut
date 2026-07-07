"""渲染阶段 Worker — compute/commit 分离 (V0.1.14)."""

from __future__ import annotations

import re
import time
from pathlib import Path as _Path

from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error, is_retryable
from app.db.models import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed, mark_heartbeat


def _is_render_error_permanent(exc: Exception) -> bool:
    """从渲染异常中提取 FFmpeg 错误类型, 判断是否永久失败。"""
    msg = str(exc)
    m = re.search(r"\[([A-Z_]+)\]", msg)
    if m:
        type_name = m.group(1)
        try:
            error_type = FfmpegErrorType[type_name]
            return not is_retryable(error_type)
        except KeyError:
            pass
    if isinstance(exc, RuntimeError):
        stderr_marker = "]: "
        idx = msg.find(stderr_marker)
        extracted_stderr = msg[idx + len(stderr_marker):] if idx != -1 else msg
        error_type = classify_ffmpeg_error(-1, extracted_stderr)
        if error_type != FfmpegErrorType.UNKNOWN:
            return not is_retryable(error_type)
    return False


def render_compute(task_id: int) -> dict:
    """纯渲染计算 — 无数据库写入。

    :returns: {"clip_id", "file_path", "duration_s"} 或 {"error", "permanent"}。
    """
    from app.pipeline.orchestrator import produce_clip

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None or task.candidate_id is None:
            return {"error": "task or candidate_id missing", "permanent": True}
        cid = task.candidate_id

    try:
        clip = produce_clip(cid, auto_upload=False)
    except Exception as exc:
        permanent = _is_render_error_permanent(exc)
        return {"error": f"RenderError: {exc}", "permanent": permanent}

    if clip is None:
        return {"error": "clip rendering returned no result", "permanent": False}

    out_exists = clip.file_path and _Path(clip.file_path).exists()
    out_size_ok = out_exists and _Path(clip.file_path).stat().st_size > 1024
    if not out_exists:
        return {"error": "output file missing", "permanent": False}
    if not out_size_ok:
        detail = (
            f"output too small ({_Path(clip.file_path).stat().st_size} bytes)" if clip.file_path else "no output path"
        )
        return {"error": f"RenderFailedError: {detail}", "permanent": False}

    if clip.duration_s is not None and clip.duration_s < 1.0:
        return {"error": f"output duration too short ({clip.duration_s:.1f}s)", "permanent": False}

    return {
        "clip_id": clip.id,
        "file_path": clip.file_path,
        "duration_s": clip.duration_s,
    }


def commit_render(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """提交渲染结果 — 单一事务 + 租约校验。"""
    import logging

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.warning("stale_result_discarded: task=%s 已失去租约, 丢弃渲染结果", lease.task_id)
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        if "error" in compute_result:
            mark_failed(
                task,
                compute_result["error"],
                permanent=compute_result.get("permanent", False),
            )
            db.add(task)
            return

        mark_completed(task, ms)
        enqueue_next(task, TaskStatus.RENDERED, clip_id=compute_result.get("clip_id"))
        db.add(task)


def run_render(lease: TaskLease) -> None:
    """渲染阶段入口 — heartbeat → compute → commit。"""
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.candidate_id is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = render_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_render(lease, compute_result, ms_val)
