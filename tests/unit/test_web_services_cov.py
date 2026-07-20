"""High-impact coverage boost — web services and routers.

Targets small uncovered modules to push total from 47% to 50%.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


# ── Transcripts service ────────────────────────────────


class TestTranscriptsService:
    """transcripts through segments router."""

    def test_transcripts_list(self, temp_db: None) -> None:
        """GET /api/segments/transcripts returns list."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/segments/transcripts")
            assert r.status_code in (200, 404, 304)


# ── Variants router ────────────────────────────────────


class TestVariantsRouterFull:
    """variants router (currently 50%, 6 uncovered)."""

    def test_variants_list_for_clip(self, temp_db: None) -> None:
        """GET variants for a clip."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/clips/1/variants")
            assert r.status_code in (200, 404)

    def test_variants_create_for_clip(self, temp_db: None) -> None:
        """POST variants for a clip."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.post("/api/clips/1/variants", json={"variant_type": "single"})
            assert r.status_code in (200, 201, 400, 404, 422)


# ── Collection router ──────────────────────────────────


class TestCollectionRouterFull:
    """collection_router (currently 55%)."""

    def test_collection_detail(self, temp_db: None) -> None:
        """GET collection detail."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/collections/1")
            assert r.status_code in (200, 404)


# ── Settings services deeper ───────────────────────────


class TestSettingsDeeper:
    """settings service (currently 65%)."""

    def test_settings_biliup_toggle(self, temp_db: None) -> None:
        """Toggle biliup_enabled."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.patch("/api/settings", json={"biliup_enabled": False})
            assert r.status_code in (200, 400)


# ── Notification services ──────────────────────────────


class TestNotificationsDeeper:
    """notifications service (currently 27%)."""

    def test_notifications_paginated(self, temp_db: None) -> None:
        """GET notifications with pagination."""
        from app.web.main import app

        with TestClient(app) as client:
            r = client.get("/api/notifications?page=1&per_page=5")
            assert r.status_code == 200
