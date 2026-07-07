"""Web 控制台 API 测试(FastAPI TestClient,不发起真实网络/录制)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_dashboard_and_room_crud(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """概览、添加直播间(mock 取流)、调阈值等核心 API 正常工作。"""
    from app.sources.bilibili.client import BilibiliLiveClient, RoomInfo
    from app.web.main import app

    async def fake_room_info(self: BilibiliLiveClient, url: str) -> RoomInfo:  # noqa: ANN001
        return RoomInfo(room_id=12345, short_id=0, uid=1, live_status=0)

    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", fake_room_info)

    with TestClient(app) as client:
        # 初始概览
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        assert r.json()["counts"]["candidates"] == 0

        # 添加直播间(已授权)
        r = client.post(
            "/api/rooms",
            json={"url": "https://live.bilibili.com/12345", "authorized": True},
        )
        assert r.status_code == 200
        db_id = r.json()["id"]
        assert r.json()["room_id"] == 12345

        # 出现在概览中
        rooms = client.get("/api/dashboard").json()["rooms"]
        assert any(rm["id"] == db_id for rm in rooms)

        # 调整阈值与模式
        r = client.patch(f"/api/rooms/{db_id}", json={"mode": "auto", "highlight_threshold": 0.7})
        assert r.status_code == 200
        assert r.json()["mode"] == "auto"
        assert abs(r.json()["highlight_threshold"] - 0.7) < 1e-6


def test_add_room_requires_authorization(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """未确认授权时添加直播间返回 400。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.post("/api/rooms", json={"url": "123", "authorized": False})
        assert r.status_code == 400


def test_start_unauthorized_room_returns_400(temp_db: None) -> None:
    """对未授权房间启动录制应被拒绝(400)。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        room = LiveRoom(input_url="x", room_id=1, authorized=False)
        db.add(room)
        db.flush()
        rid = room.id

    with TestClient(app) as client:
        r = client.post(f"/api/rooms/{rid}/start", json={"pipeline": False})
        assert r.status_code == 400


def test_candidate_listing_and_reject(temp_db: None) -> None:
    """候选可被列出并拒绝。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.main import app

    now = datetime.now(UTC)
    with get_session() as db:
        cand = HighlightCandidate(
            session_id=1,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=30),
            highlight_score=0.8,
            reason="测试候选",
        )
        db.add(cand)
        db.flush()
        cid = cand.id

    with TestClient(app) as client:
        rows = client.get("/api/candidates").json()
        assert any(c["id"] == cid for c in rows)

        r = client.post(f"/api/candidates/{cid}/reject")
        assert r.status_code == 200

        rejected = client.get("/api/candidates?status=rejected").json()
        assert any(c["id"] == cid for c in rejected)


def test_trends_endpoint(temp_db: None) -> None:
    """网感资料库接口返回概览结构;未启用时 enabled=False。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/trends")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data
        assert "items" in data
        assert "keywords" in data


def test_danmaku_overview(temp_db: None) -> None:
    """弹幕接口返回热度概览结构(已接入采集模块)。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/danmaku")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is True
        assert "recent" in data
        assert "sessions" in data


def test_dashboard_page_renders(temp_db: None) -> None:
    """根路径返回仪表盘 HTML。"""
    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "BiliLiveCut" in r.text


def test_settings_toggle_and_uploads(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """上传开关默认关闭,可切换;上传队列与打开目录接口工作。"""
    from app.web import service
    from app.web.main import app

    # 避免测试真的打开文件管理器窗口。
    monkeypatch.setattr(service, "open_path", lambda p: True)

    with TestClient(app) as client:
        s = client.get("/api/settings").json()
        assert s["biliup_enabled"] is False
        assert s["upload_active"] is False

        s2 = client.patch("/api/settings", json={"biliup_enabled": True}).json()
        assert s2["biliup_enabled"] is True
        assert s2["upload_active"] is True

        assert client.get("/api/uploads").json() == []
        assert client.get("/api/notifications").json() == []

        r = client.post("/api/open-clips-dir")
        assert r.status_code == 200
        assert "clips_dir" in r.json()
