"""P3 磁盘保护与文件生命周期管理。

安全保护措施:
- 最低剩余空间阈值(默认 10GB),低于阈值时暂停高风险任务;
- 原始文件保留天数(默认 7 天);
- 被拒绝候选的自动清理策略;
- 成片成功后的原始分段延迟清理(默认 24 小时);
- 所有清理操作可配置并记录日志。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.core.config import settings
from app.core.paths import clips_dir, raw_dir

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.db.entities import FinalClip


def _safe_unlink(disk_path: str, allowed_root: Path) -> bool:
    """安全删除文件: resolve() 路径必须在 allowed_root 前缀下,防止路径遍历攻击。

    V0.1.12.8: 修复 TOCTOU — resolve() 后使用已解析路径删除,
    而非原始路径, 防止验证-删除窗口内的符号链接替换攻击。

    :param disk_path: 数据库中记录的文件路径。
    :param allowed_root: 允许的根目录 (如 clips_dir)。
    :returns: 是否成功删除。
    """
    try:
        resolved = Path(disk_path).resolve()
        resolved_root = allowed_root.resolve()
        # resolved 必须在 allowed_root 子树内
        resolved.relative_to(resolved_root)
    except (ValueError, OSError):
        logger.warning("拒绝删除非托管路径 (不在 {} 下): {}", allowed_root, disk_path)
        return False
    try:
        resolved.unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.debug("删除文件失败 {}: {}", resolved, exc)
        return False


# 可配置的默认值(可通过 settings 覆盖)。
_MIN_FREE_GB = 10
_RAW_RETENTION_DAYS = 7


def get_disk_usage(path: str | Path | None = None) -> dict:
    """获取磁盘使用情况。

    :param path: 检测路径(默认 clips_dir 所在磁盘)。
    :returns: ``{total_gb, used_gb, free_gb, free_percent}``。
    """
    p = Path(path) if path else clips_dir()
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("无法创建目录 {},回退到当前目录统计磁盘使用。", p)
            p = Path(".")
    usage = shutil.disk_usage(p)
    return {
        "total_gb": round(usage.total / (1024**3), 1),
        "used_gb": round(usage.used / (1024**3), 1),
        "free_gb": round(usage.free / (1024**3), 1),
        "free_percent": round(usage.free / usage.total * 100, 1) if usage.total > 0 else 0.0,
    }


def get_directory_size(path: str | Path) -> float:
    """递归计算目录大小(GB)。

    :param path: 目录路径。
    :returns: 大小(GB)。
    """
    p = Path(path)
    if not p.exists():
        return 0.0
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return round(total / (1024**3), 2)


def check_disk_safe(min_free_gb: float | None = None) -> tuple[bool, str]:
    """检查磁盘剩余空间是否安全。

    :param min_free_gb: 最低剩余空间(GB),默认从 settings 或 10GB。
    :returns: ``(is_safe, message)``。
    """
    threshold = min_free_gb or getattr(settings, "min_free_disk_gb", _MIN_FREE_GB)
    try:
        usage = get_disk_usage()
    except Exception as exc:
        return False, f"无法检测磁盘空间: {exc}"

    free = usage["free_gb"]
    if free < threshold:
        msg = (
            f"磁盘剩余空间不足: {free:.1f}GB < {threshold:.1f}GB "
            f"(总 {usage['total_gb']:.1f}GB, 已用 {usage['used_gb']:.1f}GB)"
        )
        logger.warning(msg)
        return False, msg
    return True, f"磁盘剩余 {free:.1f}GB,安全。"


def check_disk_level() -> tuple[str, float]:
    """两级磁盘保护检查,返回当前危险等级及剩余空间。

    等级规则:
    - ``"ok"``: 剩余空间 >= ``settings.low_disk_threshold_gb``
    - ``"low"``: ``settings.critical_disk_threshold_gb`` <= 剩余空间 < ``settings.low_disk_threshold_gb``
    - ``"critical"``: 剩余空间 < ``settings.critical_disk_threshold_gb``

    :returns: ``(level, free_gb)`` 其中 level 为 ``"ok"`` / ``"low"`` / ``"critical"``。
    """
    from app.core.runtime_settings import process_settings

    current = process_settings()
    usage = get_disk_usage()
    free_gb = usage["free_gb"]
    if free_gb < current.critical_disk_threshold_gb:
        return ("critical", free_gb)
    if free_gb < current.low_disk_threshold_gb:
        return ("low", free_gb)
    return ("ok", free_gb)


def is_safe_for_new_tasks() -> bool:
    """检查磁盘是否安全,足以启动新任务(分析/转写/渲染)。

    仅当 ``check_disk_level()`` 返回 ``"ok"`` 时返回 ``True``。

    :returns: 是否可以安全启动新任务。
    """
    level, _ = check_disk_level()
    return level == "ok"


def should_stop_recording() -> bool:
    """检查是否应立即安全停止录制(磁盘进入 critical 状态)。

    当 ``check_disk_level()`` 返回 ``"critical"`` 时返回 ``True``。

    :returns: 是否应立即停止录制并释放资源。
    """
    level, _ = check_disk_level()
    return level == "critical"


def cleanup_old_raw_files(retention_days: int | None = None) -> int:
    """按数据库引用清理已结束且无活动任务的原始分段，保留未知文件。"""
    import json
    from datetime import UTC, datetime, timedelta

    from sqlmodel import select

    from app.db.entities import (
        AppSetting,
        CandidateStatus,
        ClipVariant,
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        HotspotEvent,
        RawSegment,
        RecordingSession,
        SegmentTask,
        TaskStatus,
    )
    from app.db.session import get_session

    now = datetime.now(UTC).replace(tzinfo=None)
    days = retention_days if retention_days is not None else settings.raw_retention_days
    if days < 1:
        raise ValueError("原始录像保留天数必须 >= 1")
    age_cutoff = now - timedelta(days=days)
    clip_cutoff = now - timedelta(hours=settings.clip_cleanup_delay_hours)
    cleaned = 0
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        from app.core.media_usage import media_in_use

        if media_in_use(db):
            logger.info("原片清理暂停：媒体操作仍在使用源文件。")
            return 0
        # Web 作业可跨场次引用媒体，活动期间保守地停止自动原始文件清理。
        for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("web_job:"))).all():
            try:
                payload = json.loads(row.value)
            except json.JSONDecodeError:
                logger.warning("原片清理暂停：Web 作业记录损坏 key={}。", row.key)
                return 0
            if not isinstance(payload, dict) or payload.get("status") in {"queued", "running", "cancelling"}:
                return 0
        protected_paths = {str(Path(value).resolve()) for value in db.exec(select(FinalClip.file_path)).all() if value}
        protected_paths.update(
            str(Path(value).resolve()) for value in db.exec(select(ClipVariant.file_path)).all() if value
        )
        shared_sources: dict[str, set[int]] = {}
        for raw_path, session_id in db.exec(select(RawSegment.file_path, RawSegment.session_id)).all():
            shared_sources.setdefault(str(Path(raw_path).resolve()), set()).add(session_id)
        recordings = db.exec(select(RecordingSession).where(RecordingSession.ended_at.is_not(None))).all()
        for recording in recordings:
            if recording.status not in {"stopped", "paused", "error"}:
                continue
            if db.get(AppSetting, f"session_reanalysis:{recording.id}") is not None:
                continue
            tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == recording.id)).all()
            if any(task.stage not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED} for task in tasks):
                continue
            candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == recording.id)).all()
            if any(candidate.status in {CandidateStatus.PENDING, CandidateStatus.APPROVED} for candidate in candidates):
                continue
            if (
                db.exec(
                    select(HotspotEvent.id)
                    .where(
                        HotspotEvent.session_id == recording.id, HotspotEvent.status.in_(["provisional", "enriching"])
                    )
                    .limit(1)
                ).first()
                is not None
            ):
                continue
            from app.web.services.review_workflow import claim_state, has_review_draft

            review_events = db.exec(
                select(HighlightEvent)
                .join(HighlightCandidate, HighlightEvent.candidate_id == HighlightCandidate.id)
                .where(HighlightCandidate.session_id == recording.id)
            ).all()
            if any(has_review_draft(event.features_json) or claim_state(event)["active"] for event in review_events):
                continue
            segments = db.exec(select(RawSegment).where(RawSegment.session_id == recording.id)).all()
            completed = bool(tasks) and all(task.stage == TaskStatus.COMPLETED for task in tasks)
            expired = recording.ended_at <= age_cutoff
            usable_clips = db.exec(
                select(FinalClip, HighlightCandidate)
                .join(HighlightCandidate, FinalClip.candidate_id == HighlightCandidate.id)
                .where(HighlightCandidate.session_id == recording.id)
            ).all()
            for segment in segments:
                rendered_old = False
                if completed and segment.start_ts is not None and segment.end_ts is not None:
                    segment_task = next((task for task in tasks if task.segment_id == segment.id), None)
                    if (
                        segment_task is not None
                        and segment_task.completed_at is not None
                        and segment_task.completed_at <= clip_cutoff
                    ):
                        rendered_old = any(
                            clip.status in {"generated", "ready", "published"}
                            and clip.created_at <= clip_cutoff
                            and Path(clip.file_path).is_file()
                            and candidate.start_ts <= segment.start_ts
                            and candidate.end_ts >= segment.end_ts
                            for clip, candidate in usable_clips
                        )
                if not expired and not rendered_old:
                    continue
                file = Path(segment.file_path)
                resolved = str(file.resolve())
                if (
                    not file.is_file()
                    or resolved in protected_paths
                    or shared_sources.get(resolved, set()) - {recording.id}
                ):
                    continue
                # 仅删除确认为托管原始媒体的单个文件，目录和非登记文件保持原样。
                if _safe_unlink(segment.file_path, raw_dir()):
                    cleaned += 1
                    if cleaned >= 100:
                        logger.info("本轮原片清理达到 100 个文件，余项在下轮继续。")
                        return cleaned
    if cleaned:
        logger.info("已按保留策略清理 {} 个无活动引用的原始分段。", cleaned)
    return cleaned


def _clip_is_protected(db: Session, clip: FinalClip) -> bool:
    """保留发布、审核、任务、上传及其它成片正在引用的资源。"""
    from sqlmodel import select

    from app.db.entities import ClipStatus, ClipVariant, FinalClip, SegmentTask, TaskStatus, UploadTask

    if clip.status in {ClipStatus.PUBLISHED, ClipStatus.READY, ClipStatus.REVIEWING}:
        return True
    uploads = db.exec(select(UploadTask).where(UploadTask.clip_id == clip.id)).all()
    if any(task.status not in {"failed", "failed_permanent", "skipped", "cancelled"} for task in uploads):
        return True
    tasks = db.exec(select(SegmentTask).where(SegmentTask.clip_id == clip.id)).all()
    if any(task.stage not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED} for task in tasks):
        return True
    paths = [path for path in (clip.file_path, clip.cover_path) if path]
    if db.exec(select(ClipVariant).where(ClipVariant.file_path.in_(paths))).first() is not None:
        return True
    return (
        db.exec(
            select(FinalClip).where(
                FinalClip.id != clip.id,
                (FinalClip.file_path.in_(paths)) | (FinalClip.cover_path.in_(paths)),
                FinalClip.status != ClipStatus.REJECTED,
            )
        ).first()
        is not None
    )


def cleanup_rejected_candidates() -> int:
    """清理被拒绝候选的切片文件。

    :returns: 清理的切片数。
    """
    from sqlmodel import select

    from app.db.entities import CandidateStatus, FinalClip, HighlightCandidate
    from app.db.session import get_session

    cleaned = 0
    with get_session() as db:
        # 防止检查后另一个数据库写入者把同一素材加入发布或渲染流程。
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        rejected = db.exec(
            select(HighlightCandidate).where(
                HighlightCandidate.status == CandidateStatus.REJECTED,
            )
        ).all()

        for cand in rejected:
            clips = db.exec(
                select(FinalClip).where(
                    FinalClip.candidate_id == cand.id,
                )
            ).all()
            fully_cleaned = True
            for clip in clips:
                if _clip_is_protected(db, clip):
                    fully_cleaned = False
                    continue
                removed = not clip.file_path or _safe_unlink(clip.file_path, clips_dir())
                if clip.file_path and removed:
                    cleaned += 1
                cover_removed = not clip.cover_path or _safe_unlink(clip.cover_path, clips_dir())
                fully_cleaned = fully_cleaned and removed and cover_removed
            # 更新状态为已清理。
            if fully_cleaned:
                cand.status = CandidateStatus.CLEANED
                db.add(cand)

    if cleaned:
        logger.info("已清理 {} 个被拒绝候选的切片文件。", cleaned)
    return cleaned


def run_disk_maintenance(*, cleanup: bool = True) -> dict:
    """执行一次磁盘维护(清理 + 检查)。

    建议每 60 分钟调用一次。

    :returns: 维护报告。
    """
    result = {"disk": {}, "cleaned_raw": 0, "cleaned_rejected": 0, "safe": True}

    # 磁盘使用。
    result["disk"] = get_disk_usage()

    # 目录大小。
    result["raw_size_gb"] = get_directory_size(raw_dir())
    result["clips_size_gb"] = get_directory_size(clips_dir())

    # 清理。
    if cleanup:
        result["cleaned_raw"] = cleanup_old_raw_files()
        result["cleaned_rejected"] = cleanup_rejected_candidates()

    # 安全检查。
    safe, msg = check_disk_safe()
    result["safe"] = safe
    result["safe_message"] = msg

    # 两级磁盘保护:critical 时触发通知。
    level, free_gb = check_disk_level()
    result["disk_level"] = level
    if level == "critical":
        logger.warning("磁盘进入 critical 状态 (剩余 {:.1f}GB),触发通知。", free_gb)
        try:
            from app.notify.webhook import notify_disk_alert

            notify_disk_alert(
                free_gb=free_gb,
                threshold_gb=int(settings.critical_disk_threshold_gb),
                raw_gb=result.get("raw_size_gb", 0.0),
                clips_gb=result.get("clips_size_gb", 0.0),
            )
        except Exception:
            logger.exception("磁盘 critical 通知发送失败")

    logger.info(
        "磁盘维护完成: raw={:.2f}GB clips={:.2f}GB free={:.1f}GB cleaned_raw={} cleaned_rej={} safe={} level={}",
        result["raw_size_gb"],
        result["clips_size_gb"],
        result["disk"].get("free_gb", 0),
        result["cleaned_raw"],
        result["cleaned_rejected"],
        safe,
        level,
    )
    return result
