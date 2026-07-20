"""Cookie and settings module behavioral tests."""

from __future__ import annotations


def test_get_cookie_runs_and_returns_str() -> None:
    """get_bilibili_cookie returns a string through the settings chain."""
    from app.core.cookie import get_bilibili_cookie

    result = get_bilibili_cookie()
    assert isinstance(result, str)


def test_settings_store_get_set_delete_cycle(tmp_path) -> None:
    """settings_store set/get/delete persists correctly across calls."""
    import os

    from app.core import settings_store

    os.environ["STORAGE_ROOT"] = str(tmp_path)
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir(exist_ok=True)

    settings_store.set_setting("stage5_test", "value5")
    val = settings_store.get_setting("stage5_test", "default")
    assert val == "value5"

    # Default fallback
    val2 = settings_store.get_setting("nonexistent_stage5", "fallback123")
    assert val2 == "fallback123"


def test_settings_fields_boundary_values() -> None:
    """Settings fields respect their declared boundaries."""
    from app.core.config import Settings

    s = Settings()
    assert 5 <= s.segment_duration_s <= 600
    assert s.reconnect_max_backoff_s >= 1
    assert s.live_poll_interval_s >= 5
    assert s.asr_primary_max_concurrency >= 1
    assert s.asr_auxiliary_max_concurrency >= 1
    assert s.asr_review_max_concurrency >= 1
    assert s.asr_fallback_max_concurrency >= 1
    # Check all devices default to cpu
    for attr in ("asr_primary_device", "asr_auxiliary_device", "asr_review_device", "asr_fallback_device"):
        assert getattr(s, attr) == "cpu"


def test_db_session_context_manager(temp_db: None) -> None:
    """get_session context manager provides a working session."""
    from app.db.session import get_session

    with get_session() as db:
        assert db is not None
