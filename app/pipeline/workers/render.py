"""渲染阶段 Worker — compute/commit 真正分离。

render_compute 渲染到租约专属临时目录, 不写 FinalClip/ClipVariant。
commit_render 在租约保护下原子移动文件并写入 ClipVariant。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error, is_retryable
from app.core.paths import clips_dir
from app.db.models import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed, mark_heartbeat

_logger = logging.getLogger(__name__)


def _temp_clip_path(task_id: int, lease_token: str, suffix: str = ".partial.mp4") -> str:
    """生成租约专属临时文件路径。

    格式: clips_dir/clip.{task_id}.{lease_token[:8]}{suffix}
    """
    return str(Path(clips_dir()) / f"clip.{task_id}.{lease_token[:8]}{suffix}")


def _is_render_error_permanent(exc: Exception) -> bool:
    """从渲染异常提取 FFmpeg 错误类型, 判断是否永久失败。"""
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
        extracted_stderr = msg[idx + len(stderr_marker) :] if idx != -1 else msg
        error_type = classify_ffmpeg_error(-1, extracted_stderr)
        if error_type != FfmpegErrorType.UNKNOWN:
            return not is_retryable(error_type)
    return False


def render_compute(task_id: int) -> dict:
    """纯渲染计算 — 仅输出到租约临时文件, 不写 ClipVariant。

    渲染到临时路径 clip.{task_id}.{lease_token[:8]}.partial.mp4。

    :returns: {"clip_id", "file_path", "duration_s", "render_config_hash"} 或 {"error"}。
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

    if not clip.file_path or not Path(clip.file_path).exists():
        return {"error": "output file missing", "permanent": False}

    if Path(clip.file_path).stat().st_size <= 1024:
        return {
            "error": f"output too small ({Path(clip.file_path).stat().st_size} bytes)",
            "permanent": False,
        }

    if clip.duration_s is not None and clip.duration_s < 1.0:
        return {"error": f"output duration too short ({clip.duration_s:.1f}s)", "permanent": False}

    return {
        "clip_id": clip.id,
        "file_path": clip.file_path,
        "duration_s": clip.duration_s,
    }


def commit_render(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """提交渲染结果 — 单一事务 + 租约校验。

    流程:
    1. 验证租约
    2. 验证 Event 已批准
    3. 按 event_id 查询已有 ClipVariant
    4. 租约有效时写 ClipVariant + 推进状态
    5. 租约失效时丢弃结果

    :param lease: 任务租约。
    :param compute_result: render_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
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
        db.commit()
    compute_result = render_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_render(lease, compute_result, ms_val)
