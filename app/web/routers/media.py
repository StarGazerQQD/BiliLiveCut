"""媒体预览."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.db.models import FinalClip
from app.db.session import get_session

router = APIRouter()


@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int) -> FileResponse:
    """返回成品 MP4 以便页面内预览。"""
    from app.core.paths import clips_dir as _clips_dir

    clip = _get_clip(clip_id)
    if clip is None or not clip.file_path or not Path(clip.file_path).exists():
        raise HTTPException(status_code=404, detail="视频不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip.file_path).resolve()
    clips_root = _clips_dir().resolve()
    if not file_path.is_relative_to(clips_root):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="video/mp4")


@router.get("/clips/{clip_id}/cover")
def clip_cover(clip_id: int) -> FileResponse:
    """返回成品封面图。"""
    from app.core.paths import clips_dir as _clips_dir

    clip = _get_clip(clip_id)
    if clip is None or not clip.cover_path or not Path(clip.cover_path).exists():
        raise HTTPException(status_code=404, detail="封面不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip.cover_path).resolve()
    clips_root = _clips_dir().resolve()
    if not file_path.is_relative_to(clips_root):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="image/jpeg")


def _get_clip(clip_id: int) -> FinalClip | None:
    """按主键读取成片，不让队列可见性规则影响审核媒体预览。"""
    with get_session() as db:
        return db.get(FinalClip, clip_id)
