"""高光模型运行状态与预测审计 API。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select

from app.analysis.highlight_ml.online import get_online_status
from app.db.models import SystemLog
from app.db.session import get_session

router = APIRouter()


@router.get("/highlight-ml/status")
def highlight_ml_status() -> dict[str, object]:
    """返回全局模式、特征 Schema 和注册表角色。"""
    return get_online_status()


@router.get("/highlight-ml/predictions")
def highlight_ml_predictions(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """返回最近的 Champion/Shadow 预测与回退记录。"""
    with get_session() as db:
        rows = db.exec(
            select(SystemLog)
            .where(SystemLog.module == "highlight_ml")
            .order_by(SystemLog.created_at.desc())
            .limit(limit)
        ).all()
    items: list[dict[str, object]] = []
    for row in rows:
        try:
            context = json.loads(row.context_json) if row.context_json else {}
        except (json.JSONDecodeError, TypeError):
            context = {}
        items.append(
            {
                "id": row.id,
                "level": row.level,
                "room_id": row.room_id,
                "event": row.event,
                "message": row.message,
                "context": context,
                "created_at": row.created_at.isoformat(),
            }
        )
    return {"items": items, "count": len(items)}


@router.get("/highlight-ml/drift")
def highlight_ml_drift(
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    min_recent_samples: Annotated[int, Query(ge=1, le=1000)] = 20,
) -> dict[str, object]:
    """返回当前 Champion 相对原子训练基线的近期漂移。"""
    from app.analysis.highlight_ml.operations import check_champion_drift
    from app.core.config import settings

    try:
        with get_session() as db:
            return check_champion_drift(
                db,
                registry_root=settings.highlight_ml_registry_root,
                limit=limit,
                min_recent_samples=min_recent_samples,
            )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
