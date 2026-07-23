"""高光模型 Web、房间配置与 CLI 接入测试。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_room_model_mode_merges_without_losing_existing_config(temp_db: None) -> None:
    """房间模型覆盖采用合并更新，不清除原有热词。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web.main import app

    with get_session() as db:
        room = LiveRoom(
            input_url="room",
            room_id=8,
            authorized=True,
            room_config_json='{"hotwords":["名场面"]}',
        )
        db.add(room)
        db.flush()
        room_id = room.id

    with TestClient(app) as client:
        response = client.patch(
            f"/api/rooms/{room_id}",
            json={"room_config": {"highlight_ml_mode": "shadow"}},
        )
        assert response.status_code == 200
        config = next(item for item in client.get("/api/dashboard").json()["rooms"] if item["id"] == room_id)[
            "room_config"
        ]
    assert config["hotwords"] == ["名场面"]
    assert config["highlight_ml_mode"] == "shadow"


def test_highlight_model_status_and_prediction_api(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """Web 状态与预测审计接口返回稳定结构。"""
    from app.analysis.highlight_ml import online
    from app.db.models import SystemLog
    from app.db.session import get_session
    from app.web.main import app

    monkeypatch.setattr(
        online,
        "get_online_status",
        lambda: {
            "mode": "shadow",
            "available": True,
            "champion_version": 1,
            "shadow_version": 2,
        },
    )
    with get_session() as db:
        db.add(
            SystemLog(
                module="highlight_ml",
                event="highlight_ml_prediction",
                message="ok",
                context_json='{"segment_id":3}',
            )
        )
    with TestClient(app) as client:
        status = client.get("/api/highlight-ml/status")
        predictions = client.get("/api/highlight-ml/predictions?limit=10")
    assert status.status_code == 200
    assert "mode" in status.json()
    assert predictions.status_code == 200
    assert predictions.json()["items"][0]["context"]["segment_id"] == 3


def test_highlight_model_status_cli(monkeypatch: MonkeyPatch) -> None:
    """CLI 可查看同一运行状态。"""
    from app.analysis.highlight_ml import online
    from app.cli import app

    monkeypatch.setattr(
        online,
        "get_online_status",
        lambda: {"mode": "off", "available": False, "schema_version": "1.0.0"},
    )
    result = CliRunner().invoke(app, ["highlight-model-status"])
    assert result.exit_code == 0
    assert "高光模型状态" in result.stdout
    assert "1.0.0" in result.stdout


def test_highlight_model_drift_api_requires_champion(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """尚无 Champion 时漂移接口明确返回冲突而不是伪造正常。"""
    from app.analysis.highlight_ml import online
    from app.web.main import app

    monkeypatch.setattr(online.settings, "highlight_ml_registry_root", "missing-highlight-registry")
    with TestClient(app) as client:
        response = client.get("/api/highlight-ml/drift")
    assert response.status_code == 409
    assert "Champion" in response.json()["detail"]


def test_highlight_model_train_cli_refuses_empty_dataset(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """无人工标签时训练命令非零退出且不注册半成品。"""
    from app.cli import app
    from app.core import config

    registry_root = tmp_path / "models"
    monkeypatch.setattr(config.settings, "highlight_ml_registry_root", str(registry_root))
    result = CliRunner().invoke(
        app,
        [
            "highlight-model-train",
            "--no-xgboost",
            "--min-samples",
            "1",
            "--min-positive",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "训练或注册失败" in result.stdout
    assert not (registry_root / "registry.json").exists()
