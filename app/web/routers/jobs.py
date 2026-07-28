"""Web 长耗时作业查询、取消和重试 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.web.services.background_jobs import get_job, list_jobs, web_job_manager
from app.web.services.review_workflow import review_actor

router = APIRouter()


@router.get("/jobs")
def get_jobs(request: Request, limit: int = 100, mine: bool = False) -> dict[str, Any]:
    """列出后台作业；审核员只能看到自己的作业。"""
    actor, role = review_actor(request)
    owner = actor if mine or role != "admin" else None
    return {"jobs": list_jobs(limit=limit, owner=owner), "actor": actor, "role": role}


@router.get("/jobs/{job_id}")
def get_job_detail(job_id: str, request: Request) -> dict[str, Any]:
    """查询单个后台作业。"""
    actor, role = review_actor(request)
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    if role != "admin" and job.get("owner") != actor:
        raise HTTPException(status_code=403, detail="无权查看其他用户的作业")
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    """请求取消排队或运行中的后台作业。"""
    actor, role = review_actor(request)
    try:
        return web_job_manager.cancel(job_id, actor, is_admin=role == "admin")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    """重试失败或已取消的后台作业。"""
    actor, role = review_actor(request)
    try:
        return web_job_manager.retry(job_id, actor, is_admin=role == "admin")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
