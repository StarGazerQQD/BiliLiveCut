"""运行指标 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()

@router.get("/metrics")
def get_metrics() -> dict[str, Any]:
    """返回实时运行指标 (V0.1.12.9)。

    轻量级监控端点, 包含:
    - 任务计数
    - Worker/录制状态
    - 平均性能耗时
    - 磁盘使用
    - 历史趋势 (最近 60 点)
    """
    from app.core.metrics import get_history, snapshot

    snap = snapshot()
    history = get_history(limit=60)

    return {
        "current": {
            "tasks": {
                "queued": snap.tasks_queued,
                "processing": snap.tasks_processing,
                "completed": snap.tasks_completed,
                "failed": snap.tasks_failed,
            },
            "workers": snap.active_workers,
            "recordings": snap.active_recordings,
            "recording_hours": snap.total_recording_hours,
            "avg_times": {
                "asr_ms": snap.asr_avg_ms,
                "render_ms": snap.render_avg_ms,
                "upload_ms": snap.upload_avg_ms,
            },
            "disk": {
                "free_gb": snap.disk_free_gb,
                "raw_gb": snap.disk_raw_gb,
                "clips_gb": snap.disk_clips_gb,
            },
            "db_lock_wait_avg_ms": snap.db_lock_wait_avg_ms,
        },
        "history": history,
    }