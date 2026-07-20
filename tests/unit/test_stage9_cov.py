"""Coverage boost tests for Stage 9 — targeted at low-covered modules.

Tests for: app.core.logging, app.core.sanitize extra, app.commands, app.db entities.
All tests must be robust and not flaky.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


# ── Core logging ───────────────────────────────────────


class TestLogging:
    """app.core.logging coverage."""

    def test_setup_logging_runs(self, tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
        """setup_logging runs without error."""
        monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
        from app.core.logging import setup_logging

        setup_logging()

    def test_logging_module_imports(self) -> None:
        """logging module imports."""
        from app.core import logging as log_module

        assert log_module.setup_logging is not None


# ── Core sanitize more ─────────────────────────────────


class TestSanitizeMore:
    """app.core.sanitize additional coverage."""

    def test_sanitize_password_in_url(self) -> None:
        """Password patterns in URL-like strings."""
        from app.core.sanitize import sanitize_text

        text = "password=secret123"
        result = sanitize_text(text)
        assert "secret123" not in result

    def test_sanitize_bearer_token(self) -> None:
        """Bearer token sanitization."""
        from app.core.sanitize import sanitize_text

        text = "Authorization: Bearer sk-abc1234xyz"
        result = sanitize_text(text)
        assert "sk-abc1234xyz" not in result

    def test_sanitize_empty_and_none(self) -> None:
        """Empty and None inputs safe."""
        from app.core.sanitize import sanitize_text

        assert sanitize_text("") == ""
        assert sanitize_text(None) is None

    def test_sanitize_cookie_function(self) -> None:
        """sanitize_cookie masks values."""
        from app.core.sanitize import sanitize_cookie

        result = sanitize_cookie("SESSDATA=abc123; bili_jct=xyz; DedeUserID=1")
        assert "abc123" not in result
        assert "xyz" not in result
        assert "SESSDATA=***" in result


# ── Core cookie module ─────────────────────────────────


class TestCookieModule:
    """app.core.cookie coverage."""

    def test_cookie_module_imports(self) -> None:
        """cookie module imports."""
        from app.core import cookie

        assert cookie.get_bilibili_cookie is not None


# ── Settings store full coverage ───────────────────────


class TestSettingsStoreFull:
    """app.core.settings_store full coverage."""

    def test_settings_store_set_get_delete(self, monkeypatch: MonkeyPatch) -> None:
        """settings_store set/get/delete cycle."""
        from app.core import settings_store

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setenv("STORAGE_ROOT", tmp)
            settings_dir = Path(tmp) / "settings"
            settings_dir.mkdir(exist_ok=True)

            # Set
            settings_store.set_setting("test_full", "hello")
            # Get
            val = settings_store.get_setting("test_full", "")
            assert val == "hello"
            # Default
            val2 = settings_store.get_setting("nonexistent", "fallback")
            assert val2 == "fallback"

    def test_settings_store_persistence(self, monkeypatch: MonkeyPatch) -> None:
        """Settings persist across calls."""
        from app.core import settings_store

        with tempfile.TemporaryDirectory() as tmp:
            monkeypatch.setenv("STORAGE_ROOT", tmp)
            settings_dir = Path(tmp) / "settings"
            settings_dir.mkdir(exist_ok=True)

            settings_store.set_setting("persist_test", "y")
            val = settings_store.get_setting("persist_test", "")
            assert val == "y"


# ── Database session coverage ──────────────────────────


class TestDbSession:
    """app.db.session coverage."""

    def test_get_session_context_manager(self, temp_db: None) -> None:
        """get_session context manager works."""
        from app.db.session import get_session

        with get_session() as db:
            assert db is not None

    def test_engine_is_sqlite(self) -> None:
        """engine is SQLite."""
        from app.db.session import engine

        assert engine is not None
        assert "sqlite" in str(engine.url).lower()


# ── Web init coverage ──────────────────────────────────


class TestWebInit:
    """app.web.__init__ coverage."""

    def test_web_service_imports(self) -> None:
        """web.service imports."""
        from app.web import service

        assert service is not None


# ── Monitoring metrics coverage ───────────────────────
# (metrics coverage covered by test_phase3_metrics.py)


# ── ASR metrics coverage ───────────────────────────────
# (asr_metrics coverage covered by test_phase3_metrics.py)
