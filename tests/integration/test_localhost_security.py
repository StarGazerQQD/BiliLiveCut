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
        accepted = client.patch("/api/settings/port", headers=headers, json={"web_port": 8080})
        assert accepted.status_code == 200

        rejected = client.patch(
            "/api/settings/port",
            headers={"Host": "localhost:8000", "Origin": "http://localhost:9000"},
            json={"web_port": 9000},
        )
        assert rejected.status_code == 403
        assert accepted.json()["web_port"] == 8080


def test_no_password_rejects_unknown_client_even_with_local_host(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    with TestClient(main.app, base_url="http://127.0.0.1", client=("unknown", 1234)) as client:
        assert client.get("/api/settings").status_code == 403


def test_api_documentation_requires_admin_when_password_is_configured(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "test-admin-password")
    with TestClient(main.app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 401
            assert client.get(path, auth=("admin", "test-admin-password")).status_code == 200


def test_trusted_https_forwarding_keeps_same_origin_csrf(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.web import main

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "test-admin-password")
    app = ProxyHeadersMiddleware(main.app, trusted_hosts=["127.0.0.1"])
    with TestClient(app, base_url="http://console.example", client=("127.0.0.1", 1234)) as client:
        headers = {"X-Forwarded-For": "192.0.2.1", "X-Forwarded-Proto": "https", "Origin": "https://console.example"}
        accepted = client.patch(
            "/api/settings/port", headers=headers, auth=("admin", "test-admin-password"), json={"web_port": 8080}
        )
        assert accepted.status_code == 200, accepted.text
        rejected = client.patch(
            "/api/settings/port",
            headers=headers | {"Origin": "https://other.example"},
            auth=("admin", "test-admin-password"),
            json={"web_port": 8081},
        )
        assert rejected.status_code == 403
