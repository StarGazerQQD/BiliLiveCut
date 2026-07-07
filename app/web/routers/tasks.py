"""任务队列 (V0.1.14.2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


@router.get("/tasks")
def get_tasks(limit: int = 50, stage: str | None = None) -> dict[str, Any]:
    """返回任务队列列表及各阶段统计。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    from app.pipeline.task_worker import list_tasks as _list
    from app.pipeline.task_worker import task_worker

    tasks = _list(limit=limit, stage=stage)
    return {"tasks": tasks, "stats": task_worker.stats()}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int) -> dict[str, Any]:
    """手动重试一个失败/取消的任务。"""
    from app.pipeline.task_worker import retry_task as _retry

    ok = _retry(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或状态不允许重试")
    return {"status": "retried"}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, Any]:
    """取消一个未完成的任务。"""
    from app.pipeline.task_worker import cancel_task as _cancel

    ok = _cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已完成/失败/取消")
    return {"status": "cancelled"}
