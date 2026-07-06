"""P3 运维面板路由(V0.1.7)。"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

monitor_router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# V0.1.8.2: 模块级冷却状态变量,替代模块对象动态属性挂载。
_last_disk_alert: float = 0.0


@monitor_router.get("")
def get_monitor_data() -> dict:
    """运维面板数据:磁盘/CPU/任务统计/录制状态。"""
    import time

    from app.core.config import settings
    from app.core.paths import clips_dir, raw_dir
    from app.pipeline.storage_lifecycle import (
        check_disk_safe,
        get_directory_size,
        get_disk_usage,
    )

    # 磁盘。
    disk = get_disk_usage()
    raw_size = get_directory_size(raw_dir())
    clips_size = get_directory_size(clips_dir())
    safe, safe_msg = check_disk_safe()

    # V0.1.8 P2:磁盘不足告警通知(带冷却:每30分钟最多发一次)。
    if not safe:
        from app.notify.webhook import notify_disk_alert
        global _last_disk_alert
        free_gb = disk.get("free_gb", 0) if isinstance(disk, dict) else getattr(disk, "free_gb", 0)
        if time.time() - _last_disk_alert > 1800:  # 30 分钟冷却
            notify_disk_alert(
                free_gb,
                settings.disk_alert_threshold_gb,
                raw_size,
                clips_size,
            )
            _last_disk_alert = time.time()

    # 系统资源。
    cpu = _get_cpu_percent()
    memory = _get_memory()

    # 任务队列统计。
    tasks = _get_task_stats()

    # 录制状态。
    from app.pipeline.live_monitor import live_monitor
    from app.web.service import recorder_manager

    running_rooms = recorder_manager.running_ids()
    monitor_status = live_monitor.status()

    # 最近失败任务。
    recent_failures = _get_recent_failures()

    return {
        "disk": dict(disk),
        "raw_size_gb": raw_size,
        "clips_size_gb": clips_size,
        "disk_safe": safe,
        "disk_safe_message": safe_msg,
        "cpu_percent": cpu,
        "memory": dict(memory),
        "tasks": tasks,
        "running_rooms": running_rooms,
        "running_room_count": len(running_rooms),
        "monitor": monitor_status,
        "recent_failures": recent_failures,
        "checked_at": time.time(),
    }


@monitor_router.post("/disk-maintenance")
def trigger_maintenance() -> dict:
    """手动触发磁盘维护。"""
    from app.pipeline.storage_lifecycle import run_disk_maintenance

    result = run_disk_maintenance()
    return result


def _get_cpu_percent() -> float | None:
    """获取 CPU 使用率。"""
    try:
        import psutil

        return psutil.cpu_percent(interval=0.1)
    except ImportError:
        return None


def _get_memory() -> dict:
    """获取内存使用情况。"""
    try:
        import psutil

        mem = psutil.virtual_memory()
        return {
            "total_gb": round(mem.total / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
            "percent": mem.percent,
        }
    except ImportError:
        return {"total_gb": 0, "used_gb": 0, "percent": 0}


def _get_task_stats() -> dict:
    """获取任务队列统计(各阶段数量/最老任务等待时间)。"""
    import time

    from sqlmodel import select

    from app.db.models import SegmentTask, TaskStatus
    from app.db.session import get_session

    with get_session() as db:
        all_tasks = db.exec(select(SegmentTask).order_by(SegmentTask.created_at.asc())).all()

    by_stage: dict[str, int] = {}
    oldest_wait_s = 0
    now = time.time()

    for t in all_tasks:
        stage = t.stage.value if hasattr(t.stage, "value") else str(t.stage)
        by_stage[stage] = by_stage.get(stage, 0) + 1
        if t.created_at and t.stage not in (
            TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED,
        ):
            age = now - t.created_at.timestamp()
            if age > oldest_wait_s:
                oldest_wait_s = age

    total = len(all_tasks)
    completed = by_stage.get("completed", 0)
    failed = by_stage.get("failed", 0) + by_stage.get("transient_failed", 0)
    in_progress = total - completed - failed - by_stage.get("cancelled", 0)

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "in_progress": in_progress,
        "by_stage": by_stage,
        "oldest_wait_s": round(oldest_wait_s),
    }


def _get_recent_failures() -> list[dict]:
    """获取最近 20 个失败任务。"""
    from sqlmodel import select

    from app.db.models import SegmentTask
    from app.db.session import get_session

    with get_session() as db:
        failed = db.exec(
            select(SegmentTask).where(
                SegmentTask.last_error.is_not(None),
            ).order_by(SegmentTask.updated_at.desc()).limit(20)
        ).all()

    return [
        {
            "id": t.id,
            "segment_id": t.segment_id,
            "stage": t.stage.value if hasattr(t.stage, "value") else str(t.stage),
            "error": (t.last_error or "")[:200],
            "attempts": t.attempts,
            "created": t.created_at.isoformat() if t.created_at else None,
        }
        for t in failed
    ]


# V0.1.12.2: ASR 指标
@monitor_router.get("/asr-metrics")
def get_asr_metrics() -> JSONResponse:
    """返回 ASR 调用指标 (调用次数、耗时、复核、fallback、RTF)。"""
    try:
        from app.analysis.asr_metrics import get_snapshot
        return JSONResponse(get_snapshot())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# V0.1.12.2: ASR 模型状态
@monitor_router.get("/asr-models")
def get_asr_models() -> JSONResponse:
    """返回当前已加载 ASR 模型状态。"""
    try:
        from app.analysis.asr_manager import get_asr_manager
        mgr = get_asr_manager()
        infos = []
        for info in mgr.all_infos():
            infos.append({
                "key": info.key,
                "model_id": info.model_id,
                "device": info.device,
                "is_loaded": info.is_loaded,
                "loaded_at": info.loaded_at,
                "last_used_at": info.last_used_at,
                "load_duration": info.load_duration,
                "keep_loaded": info.keep_loaded,
                "revision": info.revision,
            })
        return JSONResponse({"models": infos})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
