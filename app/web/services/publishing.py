"""Publishing."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from app.db.models import (
    UploadTask,
)
from app.db.session import get_session


def list_uploads(limit: int = 50) -> list[dict[str, Any]]:
    """列出上传任务(按更新时间降序)。

    :param limit: 数量上限。
    :returns: 上传任务字典列表。
    """
    with get_session() as db:
        rows = db.exec(
            select(UploadTask).order_by(UploadTask.updated_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": t.id,
            "clip_id": t.clip_id,
            "uploader": t.uploader,
            "status": t.status,
            "attempts": t.attempts,
            "remote_id": t.remote_id,
            "last_error": t.last_error,
            "precheck": json.loads(t.precheck_json) if t.precheck_json else None,
        }
        for t in rows
    ]
