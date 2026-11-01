"""渲染阶段 Worker — compute/commit 真正分离。

render_compute 渲染到租约专属临时文件, 不写 FinalClip/ClipVariant。
commit_render 在租约保护下提交独占文件，并重新验证审核与来源。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.analysis.editorial_review_sources import media_validation_scope
from app.clipping.models import ClipOptions
from app.clipping.paths import build_final_clip_path, build_generation_clip_path, build_lease_partial_path
from app.core.cancellation import OperationCancelledError, OperationShutdownError, shutdown_event
from app.core.media_usage import media_reference_scope
from app.core.paths import resolve_storage_path, storage_path_aliases, to_storage_relative
from app.core.process_control import ProcessCancelledError
from app.core.sanitize import safe_exception_summary
from app.db.entities import (
    CandidateStatus,
    ClipStatus,
    ClipVariant,
    ClipVariantType,
    FinalClip,
    HighlightCandidate,
    RawSegment,
    RenderStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.approval import render_approval_error
from app.pipeline.lease import LeaseLostError, TaskLease, approved_task_candidate, still_owns_lease
from app.pipeline.render_checkpoint import (
    RenderCheckpoint,
    VerifiedMedia,
    read_checkpoint,
    render_input_fingerprint,
    save_checkpoint,
    verify_media,
)
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


def render_compute(lease: TaskLease) -> dict[str, Any]:
    """纯渲染计算 — 仅输出到租约临时文件, 不写 FinalClip/ClipVariant。

    渲染到场次目录下以任务 ID 和租约 SHA-256 前 16 位命名的临时文件。
    不创建 DB 记录, 不推进任务状态。

    :param lease: 任务租约 (提供 task_id + lease_token)。
    :returns: RenderArtifact dict 或 {"error": ...}。
    """
    from app.clipping.core import render_clip_to_file

    options = ClipOptions.from_settings()
    with media_validation_scope(task_id=lease.task_id), get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.candidate_id is None:
            return {"error": "task or candidate_id missing", "permanent": True}
        approval_error = render_approval_error(db, task)
        if approval_error is not None:
            return {"error": approval_error, "permanent": True}
        cid = task.candidate_id
        event_id = task.event_id or 0
        candidate = db.get(HighlightCandidate, cid)
        if candidate is None:
            return {"error": "candidate missing", "permanent": True}
        segment = db.get(RawSegment, task.segment_id)
        if segment is None or segment.session_id != task.session_id:
            return {"error": "task/segment source mismatch", "permanent": True}
        if candidate.session_id != task.session_id:
            return {
                "error": (
                    f"task/candidate source mismatch: task_session={task.session_id} "
                    f"candidate_session={candidate.session_id}"
                ),
                "permanent": True,
            }
        session_id = task.session_id
        render_config_hash = render_input_fingerprint(db, task, options)

    temp_path = build_lease_partial_path(lease.task_id, lease.lease_token, session_id=session_id)
    temporary = {"temp_path": temp_path}

    try:
        artifact = render_clip_to_file(cid, temp_path, options)
    except (OperationCancelledError, ProcessCancelledError, LeaseLostError) as exc:
        # 仍经提交边界确认可信临时路径并清理，不把中断包装为渲染业务失败。
        return {**temporary, "interruption": exc}
    except Exception as exc:
        permanent = _is_render_error_permanent(exc)
        logging.getLogger(__name__).exception("渲染计算失败 task=%s", lease.task_id)
        return {**temporary, "error": f"RenderError: {safe_exception_summary(exc)}", "permanent": permanent}

    if not artifact:
        return {**temporary, "error": "clip rendering returned no result", "permanent": False}

    fpath = artifact.get("file_path", "")
    if fpath != temp_path or not Path(fpath).is_file():
        return {**temporary, "error": "output file missing or outside lease", "permanent": False}
    cover = artifact.get("cover_path")
    if cover and (Path(cover) != Path(temp_path).with_suffix(".jpg") or not Path(cover).is_file()):
        return {**temporary, "error": "cover file missing or outside lease", "permanent": True}

    try:
        media = verify_media(temp_path)
        if cover:
            verify_media(cover, cover=True)
    except (OSError, ValueError) as exc:
        return {**temporary, "error": f"OutputReadError: {safe_exception_summary(exc)}", "permanent": False}
    variant_type = ClipVariantType.SINGLE

    return {
        "task_id": lease.task_id,
        "lease_token": lease.lease_token,
        "temp_path": temp_path,
        "formal_path": build_final_clip_path(event_id, variant_type, render_config_hash, session_id=session_id),
        "candidate_id": cid,
        "event_id": event_id,
        "variant_type": variant_type,
        "render_config_hash": render_config_hash,
        "content_hash": media.facts.sha256,
        "duration_s": media.facts.duration,
        "size_bytes": media.facts.size,
        "width": media.facts.width,
        "height": media.facts.height,
        "cover_path": artifact.get("cover_path"),
    }


def _render_context(db: Session, lease: TaskLease, result: dict[str, Any]) -> tuple[SegmentTask, HighlightCandidate]:
    """在当前写事务重新验证租约及计算结果的来源。"""
    if not still_owns_lease(db, lease):
        raise LeaseLostError()
    task = db.get(SegmentTask, lease.task_id)
    assert task is not None
    candidate = approved_task_candidate(db, task)
    approval_error = render_approval_error(db, task)
    if approval_error is not None:
        raise ValueError(approval_error)
    if result.get("candidate_id") != candidate.id or result.get("event_id") != task.event_id:
        raise ValueError("渲染结果与任务来源不一致")
    if result.get("render_config_hash") != render_input_fingerprint(db, task):
        raise ValueError("渲染输入或选项已改变，拒绝提交旧计算结果")
    return task, candidate


def _clip_for_variant(db: Session, variant: ClipVariant, candidate_id: int, media: VerifiedMedia) -> FinalClip:
    """取得实际成片记录；恢复已就位但尚未登记的产物。"""
    if not variant.file_path:
        raise ValueError("渲染变体缺少已确认的媒体路径")
    clip = db.exec(
        select(FinalClip).where(
            FinalClip.candidate_id == candidate_id,
            FinalClip.file_path.in_(storage_path_aliases(variant.file_path)),
        )
    ).first()
    if clip is None:
        clip = FinalClip(
            candidate_id=candidate_id,
            file_path=variant.file_path,
            cover_path=variant.cover_path,
            duration_s=variant.duration_s,
            width=media.facts.width,
            height=media.facts.height,
            content_hash=variant.file_hash,
            status=ClipStatus.GENERATED,
        )
        db.add(clip)
        db.flush()
    if clip.status == ClipStatus.REJECTED:
        raise ValueError("成片已被拒绝")
    if clip.cover_path is None and variant.cover_path and resolve_storage_path(variant.cover_path).is_file():
        clip.cover_path = variant.cover_path
        db.add(clip)
    return clip


def _find_reusable(
    lease: TaskLease,
    fingerprint: str,
    *,
    include_pending: bool = True,
) -> tuple[RenderCheckpoint, VerifiedMedia, VerifiedMedia | None] | None:
    with get_session() as db:
        if not still_owns_lease(db, lease):
            raise LeaseLostError()
        task = db.get(SegmentTask, lease.task_id)
        assert task is not None
        candidate = approved_task_candidate(db, task)
        variant = db.exec(
            select(ClipVariant).where(
                ClipVariant.event_id == task.event_id,
                ClipVariant.variant_type == ClipVariantType.SINGLE,
                ClipVariant.render_config_hash == fingerprint,
                ClipVariant.render_status.in_(
                    [RenderStatus.QUEUED, RenderStatus.DONE] if include_pending else [RenderStatus.DONE]
                ),
            )
        ).first()
        record = read_checkpoint(db, variant) if variant is not None else None
        if record is None or record.task_id != lease.task_id or record.candidate_id != candidate.id:
            return None
    try:
        media, cover = record.verify()
    except (OSError, ValueError) as exc:
        _logger.warning("render_checkpoint_invalid: task=%s detail=%s", lease.task_id, safe_exception_summary(exc))
        return None
    return record, media, cover


def _commit_reuse(
    lease: TaskLease,
    reusable: tuple[RenderCheckpoint, VerifiedMedia, VerifiedMedia | None],
    ms: int,
) -> None:
    record, media, cover = reusable
    with media_validation_scope(task_id=lease.task_id), get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task, candidate = _render_context(
            db,
            lease,
            {
                "candidate_id": record.candidate_id,
                "event_id": record.event_id,
                "render_config_hash": record.input_fingerprint,
            },
        )
        variant = db.get(ClipVariant, record.variant_id)
        if (
            variant is None
            or variant.render_status not in {RenderStatus.QUEUED, RenderStatus.DONE}
            or read_checkpoint(db, variant) != record
        ):
            raise LeaseLostError("恢复时渲染代次已改变")
        if shutdown_event.is_set():
            raise OperationShutdownError("服务关闭，停止恢复渲染结果")
        media.require_unchanged()
        if cover:
            cover.require_unchanged()
        variant.render_status = RenderStatus.DONE
        db.add(variant)
        clip = _clip_for_variant(db, variant, candidate.id, media)
        candidate.status = CandidateStatus.CLIPPED
        db.add(candidate)
        _update_rendered_task(db, task, clip, ms)


def resume_render(lease: TaskLease) -> bool:
    """新租约复用同输入的完整正式产物，仍经过审核、来源及代次事务。"""
    with get_session() as db:
        if not still_owns_lease(db, lease):
            raise LeaseLostError()
        task = db.get(SegmentTask, lease.task_id)
        assert task is not None
        fingerprint = render_input_fingerprint(db, task)
    reusable = _find_reusable(lease, fingerprint)
    if reusable is None:
        return False
    _commit_reuse(lease, reusable, 0)
    return True


@media_reference_scope()
def commit_render(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """以双短事务提交独占产物，两次校验租约、审核和来源。

    文件阶段不占用数据库写锁；每一 generation 使用独占路径，失败时只
    删除本次产物，保留旧成片及其它租约的文件。
    """
    temp_path = ""
    temp_cover = ""
    output_path = ""
    output_cover = ""
    variant_id: int | None = None
    generation = 0
    try:
        media: VerifiedMedia | None = None
        cover_proof: VerifiedMedia | None = None
        with get_session() as db:
            task = db.get(SegmentTask, lease.task_id)
            if task is None or not still_owns_lease(db, lease):
                raise LeaseLostError()
            temp_path = build_lease_partial_path(lease.task_id, lease.lease_token, session_id=task.session_id)
            temp_cover = str(Path(temp_path).with_suffix(".jpg"))
            if compute_result.get("temp_path") not in {None, "", temp_path}:
                raise ValueError("渲染临时路径不属于本任务租约")
        if "error" not in compute_result and "interruption" not in compute_result and not shutdown_event.is_set():
            reusable = _find_reusable(lease, compute_result["render_config_hash"], include_pending=False)
            if reusable is not None:
                _commit_reuse(lease, reusable, ms)
                return
            media = verify_media(temp_path)
            if (
                media.facts.sha256 != compute_result["content_hash"]
                or media.facts.duration != compute_result["duration_s"]
            ):
                raise ValueError("临时渲染产物与计算结果不一致")
            if compute_result.get("cover_path"):
                if compute_result["cover_path"] != temp_cover:
                    raise ValueError("渲染封面路径不属于本任务租约")
                cover_proof = verify_media(temp_cover, cover=True)
        with media_validation_scope(task_id=lease.task_id), get_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                raise LeaseLostError()
            temp_path = build_lease_partial_path(lease.task_id, lease.lease_token, session_id=task.session_id)
            temp_cover = str(Path(temp_path).with_suffix(".jpg"))
            if compute_result.get("temp_path") not in {None, "", temp_path}:
                raise ValueError("渲染临时路径不属于本任务租约")
            if not still_owns_lease(db, lease):
                raise LeaseLostError()
            interruption = compute_result.get("interruption")
            if isinstance(interruption, (OperationCancelledError, ProcessCancelledError, LeaseLostError)):
                raise interruption
            if "error" in compute_result:
                mark_failed(task, compute_result["error"], permanent=compute_result.get("permanent", False))
                db.add(task)
                return
            if shutdown_event.is_set():
                raise OperationShutdownError("服务关闭，停止提交本次渲染结果")
            task, candidate = _render_context(db, lease, compute_result)
            formal_path = build_final_clip_path(
                task.event_id,
                compute_result.get("variant_type", ClipVariantType.SINGLE),
                compute_result["render_config_hash"],
                session_id=task.session_id,
            )
            if compute_result.get("formal_path") != formal_path:
                raise ValueError("渲染正式路径不属于本任务场次")
            cover = compute_result.get("cover_path")
            if cover and (str(cover) != temp_cover or not Path(cover).is_file()):
                raise ValueError("租约封面不存在或不属于本次输出")
            variant = db.exec(
                select(ClipVariant).where(
                    ClipVariant.event_id == task.event_id,
                    ClipVariant.variant_type == compute_result.get("variant_type", ClipVariantType.SINGLE),
                    ClipVariant.render_config_hash == compute_result["render_config_hash"],
                )
            ).first()
            assert media is not None
            media.require_unchanged()
            if cover_proof:
                cover_proof.require_unchanged()
            if variant is None:
                variant = ClipVariant(
                    event_id=task.event_id,
                    variant_type=compute_result.get("variant_type", ClipVariantType.SINGLE),
                    render_config_hash=compute_result["render_config_hash"],
                    generation=0,
                )
                db.add(variant)
                db.flush()
            assert variant.id is not None
            variant_id = variant.id
            variant.generation += 1
            generation = variant.generation
            output_path = build_generation_clip_path(formal_path, variant_id, generation, lease.lease_token)
            output_cover = str(Path(output_path).with_suffix(".jpg")) if cover else ""
            variant.file_path = to_storage_relative(output_path)
            variant.cover_path = to_storage_relative(output_cover) if output_cover else None
            variant.file_hash = compute_result["content_hash"]
            variant.duration_s = compute_result["duration_s"]
            variant.render_status = RenderStatus.QUEUED
            variant.resolution = f"{media.facts.width}x{media.facts.height}"
            db.add(variant)
            record = save_checkpoint(db, lease, variant, candidate.id, media, cover_proof)

        if output_cover:
            Path(temp_cover).replace(output_cover)
        Path(temp_path).replace(output_path)

        moved_media, moved_cover = record.verify()

        with media_validation_scope(task_id=lease.task_id), get_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task, candidate = _render_context(db, lease, compute_result)
            variant = db.get(ClipVariant, variant_id)
            if (
                variant is None
                or variant.generation != generation
                or variant.file_path != to_storage_relative(output_path)
            ):
                raise LeaseLostError()
            if shutdown_event.is_set():
                raise OperationShutdownError("服务关闭，停止登记已移动的渲染结果")
            moved_media.require_unchanged()
            if moved_cover:
                moved_cover.require_unchanged()
            variant.render_status = RenderStatus.DONE
            db.add(variant)
            clip = _clip_for_variant(db, variant, candidate.id, moved_media)
            candidate.status = CandidateStatus.CLIPPED
            db.add(candidate)
            _update_rendered_task(db, task, clip, ms)
        output_path = ""  # 已提交的产物由成片生命周期管理。
        output_cover = ""
    except LeaseLostError:
        _logger.warning("stale_result_discarded: render task=%s", lease.task_id)
    except (OSError, ValueError) as exc:
        _logger.exception("渲染提交失败 task=%s", lease.task_id)
        with get_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if still_owns_lease(db, lease):
                task = db.get(SegmentTask, lease.task_id)
                assert task is not None
                mark_failed(task, f"渲染提交失败: {safe_exception_summary(exc)}", permanent=isinstance(exc, ValueError))
                db.add(task)
            variant = db.get(ClipVariant, variant_id) if variant_id is not None else None
            if (
                variant is not None
                and variant.generation == generation
                and variant.render_status == RenderStatus.QUEUED
            ):
                variant.render_status = RenderStatus.FAILED
                db.add(variant)
    finally:
        # 提交可能已成功但连接回报失败；逐文件核对正式引用，数据库不可读时保留产物。
        cleanup = {path for path in (temp_path, temp_cover, output_path, output_cover) if path}
        if cleanup:
            try:
                with get_session() as db:
                    if db.get_bind().dialect.name == "sqlite":
                        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    for path in cleanup:
                        if not _render_file_referenced(db, path):
                            _safe_delete_temp(path)
                    variant = db.get(ClipVariant, variant_id) if variant_id is not None else None
                    if (
                        output_path
                        and variant is not None
                        and variant.generation == generation
                        and variant.file_path == to_storage_relative(output_path)
                        and variant.render_status == RenderStatus.QUEUED
                    ):
                        variant.render_status = RenderStatus.FAILED
                        db.add(variant)
            except SQLAlchemyError:
                _logger.exception("render_cleanup_deferred: task=%s path=%s", lease.task_id, output_path)


def _render_file_referenced(db: Session, path: str) -> bool:
    aliases = storage_path_aliases(path)
    clip = db.exec(
        select(FinalClip.id).where(or_(FinalClip.file_path.in_(aliases), FinalClip.cover_path.in_(aliases)))
    ).first()
    if clip is not None:
        return True
    return (
        db.exec(
            select(ClipVariant.id).where(
                ClipVariant.render_status == RenderStatus.DONE,
                or_(ClipVariant.file_path.in_(aliases), ClipVariant.cover_path.in_(aliases)),
            )
        ).first()
        is not None
    )


def _update_rendered_task(db: Session, task: SegmentTask, clip: FinalClip, ms: int) -> None:
    """使用实际 FinalClip 主键推进任务，不混用 ClipVariant 主键。"""
    assert clip.id is not None
    mark_completed(task, ms)
    enqueue_next(task, TaskStatus.RENDERED, clip_id=clip.id)
    db.add(task)


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
    if not temp_path:
        return
    try:
        tp = Path(temp_path)
        if tp.exists():
            tp.unlink()
            _logger.info("safe_delete_temp: %s", temp_path)
    except OSError:
        _logger.warning("safe_delete_temp_failed: %s", temp_path, exc_info=True)


@media_reference_scope()
def run_render(lease: TaskLease) -> None:
    """渲染阶段入口 — compute → commit。"""
    t0 = time.time()
    if resume_render(lease):
        return
    compute_result = render_compute(lease)
    ms_val = int((time.time() - t0) * 1000)
    commit_render(lease, compute_result, ms_val)
