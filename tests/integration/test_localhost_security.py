"""无密码 localhost Host 与同源写请求安全回归测试。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_no_password_accepts_only_loopback_authorities(temp_db: None, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """localhost、IPv4、IPv6 可访问，任意 Host 即使来自本机也必须拒绝。"""
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))
    with TestClient(main.app) as client:
        for authority in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
            response = client.get("/api/settings", headers={"Host": authority})
            assert response.status_code == 200, authority

        rejected = client.get("/api/settings", headers={"Host": "example.invalid:8000"})
        assert rejected.status_code == 403


def test_no_password_modifying_request_requires_same_origin(
    temp_db: None,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """本机写请求接受同源 Origin，并拒绝跨源 Origin。"""
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("BLC_WEB_PORT", "8000")
    headers = {"Host": "localhost:8000", "Origin": "http://localhost:8000"}
    with TestClient(main.app) as client:
        accepted = client.patch("/api/settings", headers=headers, json={"web_port": 8080})
        assert accepted.status_code == 200

        rejected = client.patch(
            "/api/settings",
            headers={"Host": "localhost:8000", "Origin": "http://localhost:9000"},
            json={"web_port": 9000},
        )
        assert rejected.status_code == 403
        assert accepted.json()["web_port"] == 8080
