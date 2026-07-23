"""候选审核."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.db.models import CandidateStatus
from app.web import service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


class BatchRequest(BaseModel):
    """批量审批/发布/删除请求体。"""

    candidate_ids: list[int]
    action: Literal["approve", "reject", "publish", "delete"]

    @field_validator("candidate_ids")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("candidate_ids")
        if len(v) > 200:
            raise ValueError("too many")
        return v


router = APIRouter()


@router.get("/candidates")
def get_candidates(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """返回高光候选列表。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_candidates(limit=limit, status=status)


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: int, request: Request) -> dict[str, Any]:
    """把候选批准出片提交到后台作业。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.background_jobs import web_job_manager
    from app.web.services.review_workflow import review_actor

    with get_session() as db:
        if db.get(HighlightCandidate, candidate_id) is None:
            raise HTTPException(status_code=404, detail="候选不存在")
    actor, _ = review_actor(request)
    job = await web_job_manager.enqueue(
        "candidate_render",
        {"candidate_id": candidate_id},
        label=f"候选 #{candidate_id} 批准出片",
        owner=actor,
        dedup_key=f"candidate-render:{candidate_id}",
    )
    return {"status": "accepted", "job": job}


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int) -> dict[str, str]:
    """拒绝候选。"""
    try:
        service.set_candidate_status(candidate_id, CandidateStatus.REJECTED)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "rejected"}


@router.delete("/candidates/{candidate_id}")
def remove_candidate(candidate_id: int) -> dict[str, str]:
    """删除候选。"""
    service.delete_candidate(candidate_id)
    return {"status": "deleted"}


@router.post("/candidates/batch")
async def batch_candidates(request: BatchRequest, http_request: Request) -> dict[str, Any]:
    """批量审核/发布/删除候选(V0.1.8 P0)。

    :param request: 包含 candidate_ids 和 action。
    :returns: 各候选操作结果。
    """
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for cid in request.candidate_ids:
        try:
            if request.action == "approve":
                from app.web.services.background_jobs import web_job_manager
                from app.web.services.review_workflow import review_actor

                actor, _ = review_actor(http_request)
                job = await web_job_manager.enqueue(
                    "candidate_render",
                    {"candidate_id": cid},
                    label=f"候选 #{cid} 批准出片",
                    owner=actor,
                    dedup_key=f"candidate-render:{cid}",
                )
                results.append({"candidate_id": cid, "status": "accepted", "job_id": job["id"]})
            elif request.action == "reject":
                service.set_candidate_status(cid, CandidateStatus.REJECTED)
                results.append({"candidate_id": cid, "status": "rejected"})
            elif request.action == "publish":
                result = service.publish_clip(cid)
                results.append({"candidate_id": cid, "status": "ready", **result})
            elif request.action == "delete":
                service.delete_candidate(cid)
                results.append({"candidate_id": cid, "status": "deleted"})
            else:
                raise HTTPException(status_code=400, detail=f"未知操作: {request.action}")
        except (ValueError, HTTPException) as exc:
            failures.append({"candidate_id": cid, "error": str(exc)})
    return {"success": results, "failed": failures}
