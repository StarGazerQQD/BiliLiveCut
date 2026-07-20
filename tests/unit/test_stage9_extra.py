"""Focus coverage boost — stage 9."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# ── CSRF parsing ───────────────────────────────────────


class TestCSRFParsing:
    """_parse_origin and _check_csrf coverage."""

    def _get_middleware(self):
        from app.web.main import _AuthMiddleware

        return _AuthMiddleware(MagicMock())

    def test_parse_http_simple(self) -> None:
        from app.web.main import _AuthMiddleware

        r = _AuthMiddleware._parse_origin("http://example.com")
        assert r == ("http", "example.com", "")

    def test_parse_https_with_port(self) -> None:
        from app.web.main import _AuthMiddleware

        r = _AuthMiddleware._parse_origin("https://example.com:8443")
        assert r == ("https", "example.com", "8443")

    def test_parse_ipv6(self) -> None:
        from app.web.main import _AuthMiddleware

        r = _AuthMiddleware._parse_origin("http://[::1]:8000")
        assert r == ("http", "::1", "8000")

    def test_parse_trailing_slash(self) -> None:
        from app.web.main import _AuthMiddleware

        r = _AuthMiddleware._parse_origin("http://localhost:8000/")
        assert r == ("http", "localhost", "8000")

    def test_invalid_no_scheme(self) -> None:
        from app.web.main import _AuthMiddleware

        assert _AuthMiddleware._parse_origin("just-a-string") is None

    def test_invalid_empty(self) -> None:
        from app.web.main import _AuthMiddleware

        assert _AuthMiddleware._parse_origin("") is None

    def test_invalid_ftp(self) -> None:
        from app.web.main import _AuthMiddleware

        assert _AuthMiddleware._parse_origin("ftp://example.com") is None

    def test_http_443_is_explicit_port(self) -> None:
        from app.web.main import _AuthMiddleware

        r = _AuthMiddleware._parse_origin("http://host:443")
        assert r == ("http", "host", "443")

    def test_loopback_check(self) -> None:
        from app.web.main import _AuthMiddleware

        mw = _AuthMiddleware(MagicMock())
        assert mw._is_loopback("127.0.0.1") is True
        assert mw._is_loopback("::1") is True
        assert mw._is_loopback("localhost") is True
        assert mw._is_loopback("127.0.0.2") is True
        assert mw._is_loopback("192.168.1.1") is False

    def test_is_modifying(self) -> None:
        from app.web.main import _AuthMiddleware

        mw = _AuthMiddleware(MagicMock())
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            req = MagicMock()
            req.method = method
            assert mw._is_modifying(req) is True
        for method in ("GET", "HEAD", "OPTIONS"):
            req = MagicMock()
            req.method = method
            assert mw._is_modifying(req) is False


# ── Proxy env ──────────────────────────────────────────


class TestProxyEnv:
    """_setup_proxy_env coverage."""

    def test_sets_no_proxy_when_unset(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        from app.web.main import _setup_proxy_env

        _setup_proxy_env()
        no_proxy = os.environ.get("NO_PROXY", "")
        assert "127.0.0.1" in no_proxy
        assert "localhost" in no_proxy
        assert "::1" in no_proxy

    def test_appends_to_existing(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setenv("NO_PROXY", "*.corp.com,.internal")

        from app.web.main import _setup_proxy_env

        _setup_proxy_env()
        no_proxy = os.environ.get("NO_PROXY", "")
        assert "*.corp.com" in no_proxy
        assert "127.0.0.1" in no_proxy


# ── Cookie tests ───────────────────────────────────────


class TestCookie:
    """Login handler cookie security."""

    def test_get_cookie_info(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.setattr(
            "app.core.settings_store.get_setting",
            lambda key, default: "SESSDATA=xx; DedeUserID=12345",
        )
        from app.web.login_handler import get_cookie_info

        result = get_cookie_info()
        assert result["has_cookie"] is True
        assert result["uid"] == "12345"


# ── Database FK tests ──────────────────────────────────


class TestDbFK:
    """UploadTask/UploadAttempt FK coverage."""

    def test_upload_task_fk_exists(self, temp_db: None) -> None:
        from app.db.models import UploadTask

        field = UploadTask.__table__.columns["clip_id"]
        fks = list(field.foreign_keys)
        assert len(fks) >= 1
        assert any("final_clips" in str(fk) for fk in fks)

    def test_upload_attempt_fks_exist(self, temp_db: None) -> None:
        from app.db.models import UploadAttempt

        for col_name in ("upload_task_id", "clip_id"):
            field = UploadAttempt.__table__.columns[col_name]
            fks = list(field.foreign_keys)
            assert len(fks) >= 1

    def test_orphan_task_fails(self, temp_db: None) -> None:
        from app.db.session import get_session

        with pytest.raises((Exception,)):
            with get_session() as db:
                from app.db.models import UploadTask

                task = UploadTask(clip_id=99999, uploader="test")
                db.add(task)
                db.flush()
                db.rollback()

    def test_orphan_attempt_fails(self, temp_db: None) -> None:
        from app.db.session import get_session

        with pytest.raises((Exception,)):
            with get_session() as db:
                from app.db.models import UploadAttempt

                attempt = UploadAttempt(upload_task_id=99999, clip_id=99999)
                db.add(attempt)
                db.flush()
                db.rollback()

    def test_migration_module(self, temp_db: None) -> None:
        from app.db.migration_v01411 import run_migration, scan_orphan_records

        assert callable(run_migration)
        assert callable(scan_orphan_records)
        result = run_migration()
        assert result is True


# ── Schema validation tests ────────────────────────────


class TestSchemaChecks:
    """Schema validation coverage."""

    def test_fingerprint_is_stable(self) -> None:
        from app.db.schema import compute_schema_fingerprint

        fp1 = compute_schema_fingerprint()
        fp2 = compute_schema_fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_version_is_2(self) -> None:
        from app.db.schema import CURRENT_SCHEMA_VERSION

        assert CURRENT_SCHEMA_VERSION == 2

    def test_verify_fk_runs(self, temp_db: None) -> None:
        from app.db.schema import _verify_foreign_keys

        assert _verify_foreign_keys() is True

    def test_verify_structure_ok(self, temp_db: None) -> None:
        from app.db.schema import _verify_actual_structure

        ok, msg = _verify_actual_structure()
        assert ok, f"Structure check failed: {msg}"


# ── Web route coverage ─────────────────────────────────


class TestWebRoutes:
    """Lightweight web route coverage."""

    def _client(self):
        from app.web.main import app

        return TestClient(app)

    def test_notifications_list(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/notifications")
            assert r.status_code in (200, 404)

    def test_settings_get(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/settings")
            assert r.status_code == 200

    def test_settings_patch(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.patch("/api/settings", json={"biliup_enabled": False})
            assert r.status_code == 200

    def test_candidates_by_bad_status(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/candidates?status=approved")
            assert r.status_code == 200

    def test_schedules_list(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/schedules")
            assert r.status_code in (200, 404)

    def test_media_list(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/media")
            assert r.status_code in (200, 404)

    def test_collections_list(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/collections")
            assert r.status_code in (200, 404)

    def test_templates_list(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/intro-templates")
            assert r.status_code in (200, 404)

    def test_subtitle_templates(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.get("/api/subtitle-templates")
            assert r.status_code in (200, 404)

    def test_room_patch(self, temp_db: None) -> None:
        with self._client() as client:
            r = client.patch("/api/rooms/1", json={"mode": "auto", "highlight_threshold": 0.7})
            assert r.status_code in (200, 404)
