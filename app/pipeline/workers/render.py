"""渲染阶段 Worker — compute/commit 真正分离。

render_compute 渲染到租约专属临时文件, 不写 FinalClip/ClipVariant。
commit_render 在租约保护下原子移动文件并写入 FinalClip/ClipVariant。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error, is_retryable
from app.core.paths import clips_dir
from app.db.models import (
    CandidateStatus,
    ClipStatus,
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed

_logger = logging.getLogger(__name__)


def _temp_clip_path(task_id: int, lease_token: str, suffix: str = ".partial.mp4") -> str:
    """生成租约专属临时文件路径。

    格式: clips_dir/clip.{task_id}.{lease_token[:8]}{suffix}
    """
    return str(Path(clips_dir()) / f"clip.{task_id}.{lease_token[:8]}{suffix}")


def _formal_clip_path(candidate_id: int) -> str:
    """生成正式切片文件路径。

    格式: clips_dir/clip_{candidate_id}.mp4
    """
    return str(Path(clips_dir()) / f"clip_{candidate_id}.mp4")


def _formal_cover_path(candidate_id: int) -> str:
    """生成正式封面文件路径。"""
    return str(Path(clips_dir()) / f"clip_{candidate_id}.jpg")


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


def render_compute(lease: TaskLease) -> dict:
    """纯渲染计算 — 仅输出到租约临时文件, 不写 FinalClip/ClipVariant。

    渲染到临时路径 clip.{task_id}.{lease_token[:8]}.partial.mp4。
    不创建 DB 记录, 不推进任务状态。

    :param lease: 任务租约 (提供 task_id + lease_token)。
    :returns: RenderArtifact dict 或 {"error": ...}。
    """
    from app.clipping.core import render_clip_to_file

    temp_path = _temp_clip_path(lease.task_id, lease.lease_token)

    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.candidate_id is None:
            return {"error": "task or candidate_id missing", "permanent": True}
        cid = task.candidate_id

    try:
        artifact = render_clip_to_file(cid, temp_path)
    except Exception as exc:
        permanent = _is_render_error_permanent(exc)
        return {"error": f"RenderError: {exc}", "permanent": permanent}

    if not artifact:
        return {"error": "clip rendering returned no result", "permanent": False}

    fpath = artifact.get("file_path", "")
    if not fpath or not Path(fpath).exists():
        return {"error": "output file missing", "permanent": False}

    size_bytes = artifact.get("size_bytes", Path(fpath).stat().st_size if Path(fpath).exists() else 0)
    if size_bytes <= 1024:
        return {
            "error": f"output too small ({size_bytes} bytes)",
            "permanent": False,
        }

    if artifact.get("duration_s", 0) < 1.0:
        return {"error": f"output duration too short ({artifact.get('duration_s', 0):.1f}s)", "permanent": False}

    return {
        "candidate_id": cid,
        "temp_path": temp_path,
        "duration_s": artifact.get("duration_s", 0),
        "size_bytes": size_bytes,
        "width": artifact.get("width"),
        "height": artifact.get("height"),
        "content_hash": artifact.get("content_hash", ""),
        "cover_path": artifact.get("cover_path"),
    }


def commit_render(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """提交渲染结果 — 租约校验后原子移动文件 + 创建 FinalClip/ClipVariant。

    流程:
    1. 验证租约 (still_owns_lease)
    2. 租约失效: 删除该 lease 的 temp 文件, 不触碰正式文件
    3. 租约有效:
       a. 原子移动 temp → formal 路径
       b. 幂等创建 FinalClip (按 candidate_id 去重)
       c. 创建 ClipVariant (按 event_id 去重)
       d. 更新 Candidate 状态为 CLIPPED
       e. 推进 Task 到 RENDERED

    :param lease: 任务租约。
    :param compute_result: render_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    temp_path = compute_result.get("temp_path", "")
    candidate_id = compute_result.get("candidate_id", 0)

    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()

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
                # 删除租约临时文件
                _safe_delete_temp(temp_path)
                db.commit()
                return

            # 原子移动 temp → formal
            formal_path = _formal_clip_path(candidate_id)
            formal_cover = _formal_cover_path(candidate_id)
            moved = _atomic_move(temp_path, formal_path)

            if not moved:
                _logger.warning(
                    "render_move_failed: task=%s temp=%s -> formal=%s",
                    lease.task_id,
                    temp_path,
                    formal_path,
                )
                mark_failed(task, "文件移动失败: temp->formal", permanent=False)
                db.add(task)
                _safe_delete_temp(temp_path)
                db.commit()
                return

            # 移动封面 (best-effort)
            artifact_cover = compute_result.get("cover_path")
            if artifact_cover and Path(artifact_cover).exists():
                _atomic_move(artifact_cover, formal_cover)

            # 幂等创建 FinalClip (按 candidate_id 去重)
            from sqlmodel import select

            existing_clip = db.exec(
                select(FinalClip).where(FinalClip.candidate_id == candidate_id)
            ).first()

            if existing_clip is not None:
                # 复用已有 FinalClip, 更新 file_path/元数据
                existing_clip.file_path = formal_path
                existing_clip.cover_path = formal_cover if Path(formal_cover).exists() else existing_clip.cover_path
                existing_clip.duration_s = compute_result.get("duration_s", existing_clip.duration_s)
                existing_clip.content_hash = compute_result.get("content_hash", existing_clip.content_hash)
                existing_clip.width = compute_result.get("width", existing_clip.width)
                existing_clip.height = compute_result.get("height", existing_clip.height)
                existing_clip.status = ClipStatus.GENERATED
                db.add(existing_clip)
                db.flush()
                clip_id = existing_clip.id
                _logger.info("render_reuse_clip: clip=%s candidate=%s", clip_id, candidate_id)
            else:
                clip = FinalClip(
                    candidate_id=candidate_id,
                    file_path=formal_path,
                    cover_path=formal_cover if Path(formal_cover).exists() else None,
                    duration_s=compute_result.get("duration_s", 0),
                    width=compute_result.get("width"),
                    height=compute_result.get("height"),
                    content_hash=compute_result.get("content_hash", ""),
                    status=ClipStatus.GENERATED,
                )
                db.add(clip)
                db.flush()
                db.refresh(clip)
                clip_id = clip.id
                _logger.info("render_create_clip: clip=%s candidate=%s", clip_id, candidate_id)

            # 更新 Candidate 状态
            cand = db.get(HighlightCandidate, candidate_id)
            if cand is not None:
                cand.status = CandidateStatus.CLIPPED
                db.add(cand)

            # 幂等创建 ClipVariant
            event_id = task.event_id
            if event_id:
                from app.db.models import ClipVariantType

                existing_var = db.exec(
                    select(ClipVariant).where(
                        ClipVariant.event_id == event_id,
                        ClipVariant.variant_type == ClipVariantType.SINGLE,
                    )
                ).first()
                if existing_var is None:
                    variant = ClipVariant(
                        event_id=event_id,
                        clip_id=clip_id,
                        variant_type=ClipVariantType.SINGLE,
                        file_path=formal_path,
                        duration_s=compute_result.get("duration_s", 0),
                        width=compute_result.get("width"),
                        height=compute_result.get("height"),
                        size_bytes=compute_result.get("size_bytes", 0),
                        content_hash=compute_result.get("content_hash", ""),
                        status=ClipStatus.GENERATED,
                    )
                    db.add(variant)
                    _logger.info("render_create_variant: event=%s clip=%s", event_id, clip_id)

            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.RENDERED, clip_id=clip_id)
            db.add(task)
            db.commit()

    except LeaseLostError:
        _logger.warning("stale_result_discarded: render task=%s 已失去租约, 丢弃临时文件", lease.task_id)
        _safe_delete_temp(temp_path)


def _atomic_move(src: str, dst: str) -> bool:
    """原子移动文件: src → dst。

    如果目标已存在且内容相同则跳过; 否则先删除目标再移动。

    :returns: 是否成功移动或目标已等效存在。
    """
    src_p = Path(src)
    dst_p = Path(dst)
    if not src_p.exists():
        return False
    if dst_p.exists():
        # 内容相同则跳过
        if src_p.stat().st_size == dst_p.stat().st_size:
            _logger.info("atomic_move_skip: src=%s dst=%s (same size)", src, dst)
            src_p.unlink(missing_ok=True)
            return True
        # 内容不同, 删除旧目标
        dst_p.unlink(missing_ok=True)
    try:
        src_p.rename(dst_p)
        _logger.info("atomic_move: %s -> %s", src, dst)
        return True
    except OSError as exc:
        _logger.error("atomic_move_failed: %s -> %s error=%s", src, dst, exc)
        return False


def _safe_delete_temp(temp_path: str) -> None:
    """安全删除租约临时文件, 不抛异常。"""
    try:
        tp = Path(temp_path)
        if tp.exists():
            tp.unlink()
            _logger.info("safe_delete_temp: %s", temp_path)
    except OSError:
        _logger.warning("safe_delete_temp_failed: %s", temp_path, exc_info=True)


def run_render(lease: TaskLease) -> None:
    """渲染阶段入口 — compute → commit。

    心跳由 scheduler 的 heartbeat thread 管理, 不在 run_* 中重复写入。
    """
    t0 = time.time()
    compute_result = render_compute(lease)
    ms_val = int((time.time() - t0) * 1000)
    commit_render(lease, compute_result, ms_val)
