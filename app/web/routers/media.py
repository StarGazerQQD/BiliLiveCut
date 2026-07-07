"""媒体预览 (V0.1.14.2)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.web import service

router = APIRouter()


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int) -> FileResponse:
    """返回成品 MP4 以便页面内预览。"""
    from app.core.paths import clips_dir as _clips_dir

    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["file_path"] or not Path(clip["file_path"]).exists():
        raise HTTPException(status_code=404, detail="视频不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip["file_path"]).resolve()
    clips_root = _clips_dir().resolve()
    if not str(file_path).startswith(str(clips_root)):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="video/mp4")


@router.get("/clips/{clip_id}/cover")
def clip_cover(clip_id: int) -> FileResponse:
    """返回成品封面图。"""
    from app.core.paths import clips_dir as _clips_dir

    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["cover_path"] or not Path(clip["cover_path"]).exists():
        raise HTTPException(status_code=404, detail="封面不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip["cover_path"]).resolve()
    clips_root = _clips_dir().resolve()
    if not str(file_path).startswith(str(clips_root)):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="image/jpeg")
