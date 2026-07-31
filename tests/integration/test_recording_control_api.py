"""录制控制 API 契约测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_start_uses_global_pipeline_default_when_request_omits_override(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """Web 开始录制未传 pipeline 时应把 ``None`` 交给统一默认值解析。"""
    from app.web import service
    from app.web.main import app

    calls: list[dict[str, Any]] = []

    async def fake_start(db_id: int, pipeline: bool | None = None, produce: bool = False) -> None:
        calls.append({"db_id": db_id, "pipeline": pipeline, "produce": produce})

    monkeypatch.setattr(service.recorder_manager, "start", fake_start)

    with TestClient(app) as client:
        response = client.post("/api/rooms/3/start", json={"produce": False})

    assert response.status_code == 200
    assert calls == [{"db_id": 3, "pipeline": None, "produce": False}]


def test_stop_and_marker_api_contract(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """停止模式、取消任务和人工打点参数通过 JSON 正确传给服务层。"""
    from app.web import service
    from app.web.main import app

    stop_calls: list[dict[str, Any]] = []
    marker_calls: list[dict[str, Any]] = []

    async def fake_stop(db_id: int, **kwargs: Any) -> dict[str, Any]:
        stop_calls.append({"db_id": db_id, **kwargs})
        return {"state": "paused", "session_id": 8, "forced": False, "cancelled_tasks": 2}

    def fake_marker(db_id: int, **kwargs: Any) -> dict[str, Any]:
        marker_calls.append({"db_id": db_id, **kwargs})
        return {"candidate_id": 12, "event_id": 13, "session_id": 8}

    monkeypatch.setattr(service.recorder_manager, "stop", fake_stop)
    monkeypatch.setattr(service.recorder_manager, "mark_highlight", fake_marker)

    with TestClient(app) as client:
        stop_response = client.post(
            "/api/rooms/3/stop",
            json={"mode": "graceful", "cancel_pending": True},
        )
        marker_response = client.post(
            "/api/rooms/3/markers",
            json={"pre_roll_s": 15, "post_roll_s": 30, "note": "团战"},
        )
        invalid_marker = client.post(
            "/api/rooms/3/markers",
            json={"pre_roll_s": -1, "post_roll_s": 1},
        )

    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "paused"
    assert stop_calls == [
        {
            "db_id": 3,
            "mode": "graceful",
            "pause_auto_restart": True,
            "cancel_pending": True,
        }
    ]
    assert marker_response.status_code == 200
    assert marker_response.json()["candidate_id"] == 12
    assert marker_calls == [{"db_id": 3, "pre_roll_s": 15.0, "post_roll_s": 30.0, "note": "团战"}]
    assert invalid_marker.status_code == 422
