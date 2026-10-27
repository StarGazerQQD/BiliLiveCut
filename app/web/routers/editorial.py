# ruff: noqa: D100
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.analysis.reanalysis_targets import ReanalysisSelection, preview_reanalysis
from app.db.session import get_session
from app.web.editorial_contracts import EditorialJobAccepted, EditorialPreview, EditorialSubmission
from app.web.services.background_jobs import web_job_manager
from app.web.services.editorial_reanalysis import enqueue_editorial_reanalysis
from app.web.services.review_workflow import review_actor

router = APIRouter()


def _admin(request: Request) -> str:
    actor, role = review_actor(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以重新分析编辑判断")
    return actor


def _session_selection(session_id: int, selection: ReanalysisSelection) -> None:
    if session_id != selection.session_id:
        raise HTTPException(status_code=422, detail="选择的场次与请求路径不一致")


@router.post("/sessions/{session_id}/editorial-reanalysis/preview", response_model=EditorialPreview)
def preview_editorial_reanalysis(
    session_id: int, selection: ReanalysisSelection, request: Request
) -> dict[str, object]:
    """只读预览，查询失败或刷新都不启动模型和作业。"""
    _admin(request)
    _session_selection(session_id, selection)
    try:
        with get_session() as db:
            return preview_reanalysis(db, selection)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/editorial-reanalysis", response_model=EditorialJobAccepted, status_code=202)
async def submit_editorial_reanalysis(
    session_id: int, payload: EditorialSubmission, request: Request
) -> EditorialJobAccepted:
    """以当前预览显式排队，可在已有作业接口查询、取消和恢复。"""
    actor = _admin(request)
    _session_selection(session_id, payload.selection)
    try:
        await web_job_manager.start()
        job = await enqueue_editorial_reanalysis(web_job_manager, payload.selection, payload.fingerprint, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EditorialJobAccepted(job_id=job["id"], status=job["status"], status_url=f"/api/jobs/{job['id']}")
