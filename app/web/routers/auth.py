"""登录管理 (V0.1.14.1)."""
from __future__ import annotations

import time as _login_time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.web.login_handler import get_cookie_info, get_login_status, start_login

_LOGIN_FAILURES: dict[str, list[float]] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300

def _check_login_rate(ip: str) -> bool:
    now = _login_time.time()
    timestamps = _LOGIN_FAILURES.get(ip, [])
    _LOGIN_FAILURES[ip] = [t for t in timestamps if now - t <= _LOGIN_WINDOW_S]
    return len(_LOGIN_FAILURES[ip]) < _MAX_LOGIN_ATTEMPTS

def _record_login_failure(ip: str) -> None:
    now = _login_time.time()
    if ip not in _LOGIN_FAILURES:
        _LOGIN_FAILURES[ip] = []
    _LOGIN_FAILURES[ip].append(now)


router = APIRouter()

# ----------------------------- 账号登录 / Cookie 管理 ----------------------------- #
@router.get("/cookie-status")
def cookie_status() -> dict[str, Any]:
    """返回当前 Bilibili Cookie 存续状态。"""
    return get_cookie_info()


@router.post("/login")
def login_start(request: Request) -> dict[str, Any]:
    """启动一次浏览器登录流程（Playwright）,返回任务 ID 供前端轮询。"""
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(ip):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁,请稍后再试")
    try:
        return start_login()
    except Exception as exc:
        _record_login_failure(ip)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/login/status")
def login_status(task_id: int) -> dict[str, Any]:
    """查询登录任务当前状态。

    - ``starting``: 正在启动浏览器
    - ``waiting``: 等待用户在浏览器中完成登录
    - ``done``: 登录成功,Cookie 已保存
    - 含 ``error`` 时表示登录失败
    """
    return get_login_status(task_id)


@router.post("/login/clear")
def login_clear() -> dict[str, str]:
    """清除已保存的 Bilibili Cookie。"""
    from app.core import settings_store

    settings_store.set_setting("bilibili_cookie", "")
    return {"status": "cleared"}
