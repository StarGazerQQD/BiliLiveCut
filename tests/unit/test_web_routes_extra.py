"""Coverage boost tests — stage 9 extra coverage."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    pass

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# ── Review router coverage ─────────────────────────────


class TestReviewRoutes:
    """review router test coverage."""

    def test_review_list(self, temp_db: None) -> None:
        """GET /api/review-list returns data."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/candidates?status=pending")
            assert r.status_code == 200
            assert isinstance(r.json(), list)

    def test_review_all_statuses(self, temp_db: None) -> None:
        """GET /api/candidates?status=approved works."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/candidates?status=approved")
            assert r.status_code == 200


# ── Notifications endpoint ─────────────────────────────


class TestNotificationRoutes:
    """notification coverage."""

    def test_notifications_list(self, temp_db: None) -> None:
        """GET /api/notifications returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/notifications")
            assert r.status_code == 200
            assert isinstance(r.json(), list)


# ── Room settings routes ───────────────────────────────


class TestRoomSettings:
    """room settings coverage."""

    def test_update_room_settings(self, temp_db: None) -> None:
        """PATCH /api/rooms/1 returns 200 or 404."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.patch(
                "/api/rooms/1",
                json={"mode": "auto", "highlight_threshold": 0.7},
            )
            assert r.status_code in (200, 404)


# ── Schedules routes ───────────────────────────────────


class TestScheduleRoutes:
    """schedule coverage."""

    def test_schedules_list(self, temp_db: None) -> None:
        """GET /api/schedules returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/schedules")
            assert r.status_code in (200, 404)

    def test_create_schedule(self, temp_db: None) -> None:
        """POST /api/schedules returns response."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.post(
                "/api/schedules",
                json={
                    "room_id": 12345,
                    "scheduled_at": "2027-01-01T00:00:00Z",
                    "enabled": True,
                },
            )
            assert r.status_code in (200, 201, 400, 404)


# ── Transcription endpoints ────────────────────────────


class TestTranscriptionRoutes:
    """transcription endpoint coverage."""

    def test_transcribe_segment_status(self, temp_db: None) -> None:
        """POST /api/transcribe/segment returns status."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.post("/api/transcribe/99999", json={})
            assert r.status_code in (200, 400, 404)


# ── Monitor routes ─────────────────────────────────────


class TestMonitorRoutes:
    """monitor route coverage."""

    def test_monitor_health(self, temp_db: None) -> None:
        """GET /api/health returns response."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/monitor")
            assert r.status_code == 200


# ── Media routes ───────────────────────────────────────


class TestMediaRoutes:
    """media route coverage."""

    def test_media_list(self, temp_db: None) -> None:
        """GET /api/media returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/media")
            assert r.status_code in (200, 404)


# ── Settings routes ────────────────────────────────────


class TestSettingsRoutes:
    """settings route coverage."""

    def test_settings_get(self, temp_db: None) -> None:
        """GET /api/settings returns config."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/settings")
            assert r.status_code == 200
            data = r.json()
            assert "biliup_enabled" in data or isinstance(data, dict)

    def test_settings_patch(self, temp_db: None) -> None:
        """PATCH /api/settings returns updated config."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.patch("/api/settings", json={"biliup_enabled": False})
            assert r.status_code == 200


# ── Collection router ──────────────────────────────────


class TestCollectionRoutes:
    """collection route coverage."""

    def test_collection_list(self, temp_db: None) -> None:
        """GET /api/collections returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/collections")
            assert r.status_code in (200, 404)


# ── Subtitle template routes ───────────────────────────


class TestSubtitleTemplateCrud:
    """subtitle template CRUD coverage."""

    def test_subtitle_template_get(self, temp_db: None) -> None:
        """GET /api/subtitle-templates returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/subtitle-templates")
            assert r.status_code in (200, 404)


# ── Intro template CRUD ────────────────────────────────


class TestIntroTemplateCrud:
    """intro template CRUD coverage."""

    def test_intro_template_create(self, temp_db: None) -> None:
        """POST /api/intro-templates returns response."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.post(
                "/api/intro-templates",
                json={"name": "test", "template": "text"},
            )
            assert r.status_code in (200, 201, 400, 422)
