"""P3 磁盘保护与文件生命周期管理(V0.1.7)。

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

from loguru import logger

from app.core.config import settings
from app.core.paths import clips_dir, raw_dir


def _safe_unlink(disk_path: str, allowed_root: Path) -> bool:
    """安全删除文件: resolve() 路径必须在 allowed_root 前缀下,防止路径遍历攻击。

    :param disk_path: 数据库中记录的文件路径。
    :param allowed_root: 允许的根目录 (如 clips_dir)。
    :returns: 是否成功删除。
    """
    try:
        resolved = Path(disk_path).resolve()
        allowed_root.resolve()
        # resolved 必须在 allowed_root 子树内
        resolved.relative_to(allowed_root.resolve())
    except (ValueError, OSError):
        logger.warning("拒绝删除非托管路径 (不在 {} 下): {}", allowed_root, disk_path)
        return False
    try:
        Path(disk_path).unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.debug("删除文件失败 {}: {}", disk_path, exc)
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


def cleanup_old_raw_files(retention_days: int | None = None) -> int:
    """清理超过保留天数的原始录像文件。

    :param retention_days: 保留天数,默认 7 天。
    :returns: 清理的目录数。
    """
    days = retention_days or getattr(settings, "raw_retention_days", _RAW_RETENTION_DAYS)
    sessions_dir_path = raw_dir()
    if not sessions_dir_path.exists():
        return 0

    import time

    cutoff = time.time() - days * 86400
    cleaned = 0

    for session_path in sessions_dir_path.iterdir():
        if not session_path.is_dir():
            continue
        try:
            mtime = session_path.stat().st_mtime
            if mtime < cutoff:
                if session_path.is_symlink():
                    logger.warning("跳过符号链接目录 (安全防护): {}", session_path)
                    continue
                shutil.rmtree(session_path)
                cleaned += 1
                logger.info("已清理过期原始文件: {}", session_path)
        except Exception as exc:
            logger.warning("清理原始文件失败 {}: {}", session_path, exc)

    if cleaned:
        logger.info("已清理 {} 个过期的原始录像目录(>{}天)。", cleaned, days)
    return cleaned


def cleanup_rejected_candidates() -> int:
    """清理被拒绝候选的切片文件。

    :returns: 清理的切片数。
    """
    from sqlmodel import select

    from app.db.models import CandidateStatus, FinalClip, HighlightCandidate
    from app.db.session import get_session

    cleaned = 0
    with get_session() as db:
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
            for clip in clips:
                if clip.file_path and _safe_unlink(clip.file_path, clips_dir()):
                    cleaned += 1
                if clip.cover_path:
                    _safe_unlink(clip.cover_path, clips_dir())
            # 更新状态为已清理。
            cand.status = CandidateStatus.CLEANED
            db.add(cand)

    if cleaned:
        logger.info("已清理 {} 个被拒绝候选的切片文件。", cleaned)
    return cleaned


def run_disk_maintenance() -> dict:
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
    result["cleaned_raw"] = cleanup_old_raw_files()
    result["cleaned_rejected"] = cleanup_rejected_candidates()

    # 安全检查。
    safe, msg = check_disk_safe()
    result["safe"] = safe
    result["safe_message"] = msg

    logger.info(
        "磁盘维护完成: raw={:.2f}GB clips={:.2f}GB free={:.1f}GB cleaned_raw={} cleaned_rej={} safe={}",
        result["raw_size_gb"],
        result["clips_size_gb"],
        result["disk"].get("free_gb", 0),
        result["cleaned_raw"],
        result["cleaned_rejected"],
        safe,
    )
    return result
