"""仪表盘 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.web import service

router = APIRouter()

@router.get("/dashboard")
def get_dashboard() -> dict[str, Any]:
    """返回仪表盘概览数据。"""
    return service.dashboard_state()

