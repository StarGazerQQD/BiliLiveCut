"""Clips."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlmodel import select

from app.core import settings_store
from app.core.osutil import open_path
from app.core.paths import clips_dir
from app.db.models import (
    ClipStatus,
    FinalClip,
)
from app.db.session import get_session


def publish_clip(clip_id: int) -> dict[str, Any]:
    """人工发布:把成品置为 ready 并导出清单;上传模块开启时入队上传。

    :param clip_id: 成品切片 id。
    :returns: 结果摘要(是否进入上传)。
    :raises ValueError: 切片不存在时。
    """
    from app.publishing.copywriter import export_manifest

    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise ValueError(f"切片不存在: id={clip_id}")
        clip.status = ClipStatus.READY
        db.add(clip)
    export_manifest(clip_id)

    if settings_store.upload_active():
        from app.publishing.uploader import enqueue_and_upload

        task = enqueue_and_upload(clip_id)
        return {"uploaded": True, "task_status": task.status}
    return {"uploaded": False, "note": "上传模块未开启,已导出待上传清单。"}


# --------------------------------------------------------------------------- #
# 设置开关 / 上传队列 / 目录
# --------------------------------------------------------------------------- #


async def enqueue_clip_upload(clip_id: int) -> dict[str, Any]:
    """手动把某成品加入上传队列并执行(线程池运行)。

    :param clip_id: 成品切片 id。
    :returns: 任务状态摘要。
    """
    from app.publishing.uploader import enqueue_and_upload

    task = await asyncio.to_thread(enqueue_and_upload, clip_id)
    return {"task_id": task.id, "status": task.status, "error": task.last_error}


async def retry_upload(task_id: int) -> dict[str, Any]:
    """重试一个上传任务(线程池运行)。

    :param task_id: 上传任务 id。
    :returns: 任务状态摘要。
    """
    from app.publishing.uploader import process_upload_task

    task = await asyncio.to_thread(process_upload_task, task_id)
    return {"task_id": task.id, "status": task.status, "error": task.last_error}


def open_clips_directory() -> str:
    """在本机文件管理器打开切片目录(供"打开目录"按钮使用)。

    :returns: 切片目录路径。
    """
    path = str(clips_dir())
    open_path(path)
    return path


def reject_clip(clip_id: int) -> None:
    """拒绝成品切片。

    :param clip_id: 成品切片 id。
    """
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is not None:
            clip.status = ClipStatus.REJECTED
            db.add(clip)


# --------------------------------------------------------------------------- #
# 查询(读)
# --------------------------------------------------------------------------- #


def list_clips(limit: int = 50) -> list[dict[str, Any]]:
    """列出成品切片(按创建时间降序)。

    :param limit: 数量上限。
    :returns: 成品字典列表。
    """
    with get_session() as db:
        rows = db.exec(
            select(FinalClip).order_by(FinalClip.created_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": c.id,
            "candidate_id": c.candidate_id,
            "title": c.title,
            "description": c.description,
            "tags": json.loads(c.tags_json) if c.tags_json else [],
            "duration_s": c.duration_s,
            "status": c.status,
            "file_path": c.file_path,
            "cover_path": c.cover_path,
            "publish_suggestion": c.publish_suggestion,
        }
        for c in rows
    ]
