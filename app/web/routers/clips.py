"""成品剪辑 (V0.1.14.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.web import service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


@router.get("/clips")
def get_clips(limit: int = 50) -> list[dict[str, Any]]:
    """返回成品切片列表。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_clips(limit=limit)


@router.post("/clips/{clip_id}/publish")
def publish_clip(clip_id: int) -> dict[str, Any]:
    """人工发布:置 ready 并导出待上传清单;上传模块开启时入队上传。"""
    try:
        result = service.publish_clip(clip_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ready", **result}


@router.post("/clips/{clip_id}/confirm-manual-upload")
def confirm_manual_upload(
    clip_id: int,
    platform: str | None = None,
    submission_id: str | None = None,
    published_url: str | None = None,
) -> dict[str, Any]:
    """V0.1.12.7: 确认手动上传完成, 将 FinalClip 标记为 PUBLISHED。

    ManualUploader 导出清单后, FinalClip 不会自动标记为 PUBLISHED。
    用户需在前端确认已完成手动投稿, 然后调用此接口完成状态更新。

    :param clip_id: FinalClip.id。
    :param platform: 投稿平台 (如 bilibili)。
    :param submission_id: 稿件号 (如 BV 号)。
    :param published_url: 已发布链接。
    :returns: 操作结果。
    """
    import json as _json

    from loguru import logger as _log

    from app.db.models import ClipStatus, FinalClip, SegmentTask, SystemLog
    from app.db.models import TaskStatus as _Ts
    from app.db.session import get_session

    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="切片不存在")
        clip.status = ClipStatus.PUBLISHED
        db.add(clip)

        # 如果有关联的 SegmentTask, 推进到 COMPLETED
        from sqlmodel import select as _sel

        task = db.exec(
            _sel(SegmentTask)
            .where(
                SegmentTask.clip_id == clip_id,
            )
            .order_by(SegmentTask.created_at.desc())
        ).first()
        if task and task.stage in (
            _Ts.AWAITING_PUBLISH_CONFIRMATION,
            _Ts.PUBLISHING,
            _Ts.QUEUED_FOR_PUBLISH,
            _Ts.RENDERED,
        ):
            task.stage = _Ts.COMPLETED
            from datetime import UTC
            from datetime import datetime as _dt_now

            task.completed_at = _dt_now(UTC)
            db.add(task)

        # 日志记录
        db.add(
            SystemLog(
                level="INFO",
                module="web",
                event="manual_upload_confirmed",
                message=f"clip={clip_id} 手动上传已确认",
                context_json=_json.dumps(
                    {
                        "clip_id": clip_id,
                        "platform": platform,
                        "submission_id": submission_id,
                        "published_url": published_url,
                    }
                )
                if (platform or submission_id or published_url)
                else None,
            )
        )

    _log.info("manual_upload_confirmed: clip={} platform={} submission_id={}", clip_id, platform, submission_id)
    return {"status": "published", "clip_id": clip_id}


@router.post("/clips/{clip_id}/reject")
def reject_clip(clip_id: int) -> dict[str, str]:
    """拒绝成品切片。"""
    service.reject_clip(clip_id)
    return {"status": "rejected"}
