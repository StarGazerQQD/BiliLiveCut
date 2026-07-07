"""渲染阶段 Worker — compute/commit 真正分离。

render_compute 渲染到租约专属临时文件, 不写 FinalClip/ClipVariant。
commit_render 在租约保护下原子移动文件 + 正式文件备份 + 补偿恢复。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clipping.paths import build_backup_path, build_final_clip_path, build_lease_partial_path
from app.db.models import (
    CandidateStatus,
    ClipStatus,
    ClipVariant,
    ClipVariantType,
    FinalClip,
    HighlightCandidate,
    RenderStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderArtifact:
    """纯计算产物 — 渲染到临时文件的元数据, 不可变。"""

    task_id: int
    lease_token: str
    temporary_path: str
    formal_path: str
    event_id: int
    variant_type: str
    render_config_hash: str
    content_hash: str
    duration_s: float
    size_bytes: int
    width: int | None
    height: int | None
    cover_path: str | None
    stderr_excerpt: str


def _compute_render_config_hash(event_id: int, variant_type: str, duration_s: float) -> str:
    """计算渲染配置指纹 (稳定, 无随机值/时间/worker 信息)。"""
    import hashlib

    raw = f"{event_id}:{variant_type}:dur={duration_s:.1f}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _compute_content_hash(file_path: str) -> str:
    """计算文件内容 SHA-256 (用于同大小不同内容的区分)。"""
    import hashlib

    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def render_compute(lease: TaskLease) -> dict[str, Any]:
    """纯渲染计算 — 仅输出到租约临时文件, 不写 FinalClip/ClipVariant。

    渲染到临时路径 clip.{task_id}.{lease_token[:8]}.partial.mp4。
    不创建 DB 记录, 不推进任务状态。

    :param lease: 任务租约 (提供 task_id + lease_token)。
    :returns: RenderArtifact dict 或 {"error": ...}。
    """
    from app.clipping.core import render_clip_to_file

    temp_path = build_lease_partial_path(lease.task_id, lease.lease_token)

    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.candidate_id is None:
            return {"error": "task or candidate_id missing", "permanent": True}
        cid = task.candidate_id
        event_id = task.event_id or 0

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
        return {"error": f"output too small ({size_bytes} bytes)", "permanent": False}

    duration_s = artifact.get("duration_s", 0)
    if duration_s < 1.0:
        return {"error": f"output duration too short ({duration_s:.1f}s)", "permanent": False}

    # 计算 content_hash (SHA-256, 用于区分同大小不同内容)
    content_hash = _compute_content_hash(temp_path)
    variant_type = ClipVariantType.SINGLE
    render_config_hash = _compute_render_config_hash(event_id, variant_type, duration_s)

    return {
        "task_id": lease.task_id,
        "lease_token": lease.lease_token,
        "temp_path": temp_path,
        "formal_path": build_final_clip_path(event_id, variant_type, render_config_hash),
        "candidate_id": cid,
        "event_id": event_id,
        "variant_type": variant_type,
        "render_config_hash": render_config_hash,
        "content_hash": content_hash,
        "duration_s": duration_s,
        "size_bytes": size_bytes,
        "width": artifact.get("width"),
        "height": artifact.get("height"),
        "cover_path": artifact.get("cover_path"),
    }


def commit_render(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """提交渲染结果 — 原子文件操作 + 备份 + 状态机。

    流程 (双短事务 + 文件阶段):
    1. 短事务1: 验证 lease → 幂等查询 ClipVariant → 创建 PENDING → 提交
    2. 文件阶段: backup old → atomic move new (os.replace)
    3. 短事务2: 验证 generation → 标记 READY → 推进 Task → 提交
    4. 成功后删除 backup

    :param lease: 任务租约。
    :param compute_result: render_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    temp_path = compute_result.get("temp_path", "")
    formal_path = compute_result.get("formal_path", "")
    event_id = compute_result.get("event_id", 0)
    variant_type = compute_result.get("variant_type", ClipVariantType.SINGLE)
    render_config_hash = compute_result.get("render_config_hash", "")
    content_hash = compute_result.get("content_hash", "")
    candidate_id = compute_result.get("candidate_id", 0)
    duration_s = compute_result.get("duration_s", 0)

    try:
        # ═══ 短事务1: 验证租约 + 创建 PENDING ClipVariant ═══
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()

            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return

            if "error" in compute_result:
                mark_failed(task, compute_result["error"], permanent=compute_result.get("permanent", False))
                db.add(task)
                _safe_delete_temp(temp_path)
                db.commit()
                return

            # 幂等查询 ClipVariant (含 render_config_hash)
            from sqlmodel import select as _sel

            existing_var = db.exec(
                _sel(ClipVariant).where(
                    ClipVariant.event_id == event_id,
                    ClipVariant.variant_type == variant_type,
                    ClipVariant.render_config_hash == render_config_hash,
                )
            ).first()

            if existing_var is not None:
                if existing_var.render_status == RenderStatus.DONE:
                    _logger.info("render_reuse_variant: variant=%s event=%s (already READY)", existing_var.id, event_id)
                    _safe_delete_temp(temp_path)
                    _update_task_and_commit(db, task, existing_var.id, ms)
                    return

                variant = existing_var
                variant.generation += 1
                variant.render_status = RenderStatus.QUEUED
                variant.file_path = formal_path
                variant.file_hash = content_hash
                variant.render_config_hash = render_config_hash
                variant.duration_s = duration_s
                db.add(variant)
                db.flush()
            else:
                variant = ClipVariant(
                    event_id=event_id,
                    candidate_id=candidate_id,
                    variant_type=variant_type,
                    render_config_hash=render_config_hash,
                    file_path=formal_path,
                    file_hash=content_hash,
                    duration_s=duration_s,
                    render_status=RenderStatus.QUEUED,
                    generation=1,
                )
                db.add(variant)
                db.flush()
                db.refresh(variant)

            variant_id = variant.id
            variant_generation = variant.generation
            db.commit()

        # ═══ 文件阶段: backup → atomic move ═══
        backup_path = _backup_and_move(temp_path, formal_path, variant_id, variant_generation)

        if backup_path is None and not Path(formal_path).exists():
            _logger.warning("render_move_failed: task=%s temp=%s -> formal=%s", lease.task_id, temp_path, formal_path)
            with get_session() as db:
                task = db.get(SegmentTask, lease.task_id)
                if task is not None:
                    mark_failed(task, "文件移动失败: temp->formal", permanent=False)
                    db.add(task)
                    var = db.get(ClipVariant, variant_id)
                    if var is not None:
                        var.render_status = RenderStatus.FAILED
                        db.add(var)
                    db.commit()
            _safe_delete_temp(temp_path)
            return

        # ═══ 短事务2: 标记 READY + 推进 Task ═══
        try:
            with get_session() as db:
                var = db.get(ClipVariant, variant_id)
                if var is None:
                    _mark_move_failed_and_restore(formal_path, backup_path, variant_id, lease.task_id)
                    return

                if var.generation != variant_generation:
                    _logger.warning(
                        "render_generation_stale: variant=%s expected_gen=%s actual_gen=%s",
                        variant_id,
                        variant_generation,
                        var.generation,
                    )
                    _safe_delete_temp(formal_path)
                    return

                if var.render_status == RenderStatus.DONE:
                    _safe_delete_temp(temp_path)
                    _remove_backup(backup_path)
                    return

                var.render_status = RenderStatus.DONE
                var.backup_path = None
                db.add(var)

                from sqlmodel import select as _sel2

                existing_clip = db.exec(_sel2(FinalClip).where(FinalClip.candidate_id == candidate_id)).first()
                if existing_clip is not None:
                    existing_clip.file_path = formal_path
                    existing_clip.duration_s = duration_s
                    existing_clip.content_hash = content_hash
                    existing_clip.status = ClipStatus.GENERATED
                    db.add(existing_clip)
                    db.flush()
                else:
                    clip = FinalClip(
                        candidate_id=candidate_id,
                        file_path=formal_path,
                        duration_s=duration_s,
                        content_hash=content_hash,
                        status=ClipStatus.GENERATED,
                    )
                    db.add(clip)
                    db.flush()
                    db.refresh(clip)

                cand = db.get(HighlightCandidate, candidate_id)
                if cand is not None:
                    cand.status = CandidateStatus.CLIPPED
                    db.add(cand)

                _update_task_and_commit(db, task, variant_id, ms)
                _remove_backup(backup_path)

        except Exception:
            _logger.warning("render_db_commit_failed: task=%s variant=%s — restoring backup", lease.task_id, variant_id)
            _restore_from_backup(formal_path, backup_path, variant_id)
            raise

    except LeaseLostError:
        _logger.warning("stale_result_discarded: render task=%s 已失去租约, 丢弃临时文件", lease.task_id)
        _safe_delete_temp(temp_path)


def _update_task_and_commit(db, task: SegmentTask, variant_id: int, ms: int) -> None:
    """推进 Task 到 RENDERED 并提交。"""
    mark_completed(task, ms)
    enqueue_next(task, TaskStatus.RENDERED, clip_id=variant_id)
    db.add(task)
    db.commit()


def _backup_and_move(temp_path: str, formal_path: str, variant_id: int, generation: int) -> str | None:
    """备份旧正式文件 (若内容不同) 并原子移动 temp → formal。

    使用 content_hash (SHA-256) 而非文件大小判断内容是否相同。
    """
    tp = Path(temp_path)
    fp = Path(formal_path)

    if not tp.exists():
        return None

    backup: str | None = None
    if fp.exists():
        existing_hash = _compute_content_hash(str(fp))
        new_hash = _compute_content_hash(str(tp))
        if existing_hash == new_hash:
            _logger.info("content_identical: formal=%s (hash same, skip)", str(fp))
            tp.unlink(missing_ok=True)
            return ""
        backup = build_backup_path(variant_id, generation)
        try:
            fp.rename(backup)
            _logger.info("backup_created: old=%s backup=%s", str(fp), backup)
        except OSError as exc:
            _logger.error("backup_failed: %s -> %s error=%s", str(fp), backup, exc)
            return None

    try:
        tp.rename(fp)
        _logger.info("atomic_move: %s -> %s", str(tp), str(fp))
        return backup
    except OSError as exc:
        _logger.error("move_failed: %s -> %s error=%s", str(tp), str(fp), exc)
        return None


def _mark_move_failed_and_restore(formal_path: str, backup_path: str | None, variant_id: int, task_id: int) -> None:
    """文件移动成功但 DB 更新失败时恢复旧文件。"""
    with get_session() as db:
        var = db.get(ClipVariant, variant_id)
        if var is not None:
            var.render_status = RenderStatus.FAILED
            var.file_path = backup_path or ""
            db.add(var)
            db.commit()

    if backup_path and Path(backup_path).exists():
        if Path(formal_path).exists():
            Path(formal_path).unlink(missing_ok=True)
        try:
            Path(backup_path).rename(formal_path)
            _logger.info("restored_backup: variant=%s %s -> %s", variant_id, backup_path, formal_path)
        except OSError as exc:
            _logger.error("restore_failed: %s -> %s error=%s", backup_path, formal_path, exc)


def _restore_from_backup(formal_path: str, backup_path: str | None, variant_id: int) -> None:
    """DB commit 失败 — 恢复 old 文件。"""
    if not backup_path or not Path(backup_path).exists():
        return
    if Path(formal_path).exists():
        Path(formal_path).unlink(missing_ok=True)
    try:
        Path(backup_path).rename(formal_path)
        _logger.info("db_failure_restored: variant=%s %s -> %s", variant_id, backup_path, formal_path)
    except OSError as exc:
        _logger.error("db_failure_restore_failed: variant=%s error=%s", variant_id, exc)


def _remove_backup(path: str | None) -> None:
    """安全删除 backup 文件。"""
    if path:
        Path(path).unlink(missing_ok=True)


def _is_render_error_permanent(exc: Exception) -> bool:
    """从渲染异常提取 FFmpeg 错误类型, 判断是否永久失败。"""
    import re

    from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error, is_retryable

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
    """渲染阶段入口 — compute → commit。"""
    t0 = time.time()
    compute_result = render_compute(lease)
    ms_val = int((time.time() - t0) * 1000)
    commit_render(lease, compute_result, ms_val)
