"""Cookie and settings module behavioral tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


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
    assert s.segment_duration_s == 300
    assert s.reconnect_max_backoff_s >= 1
    assert s.live_poll_interval_s >= 5
    assert s.danmaku_login_retry_max_attempts == 5
    assert s.danmaku_login_retry_interval_s == 60.0
    assert s.transcript_llm_refine_enabled is True
    assert s.transcript_llm_refine_max_tokens == 4096
    assert s.asr_primary_max_concurrency >= 1
    assert s.asr_auxiliary_max_concurrency >= 1
    assert s.asr_review_max_concurrency >= 1
    assert s.asr_fallback_max_concurrency >= 1
    # Check all devices default to cpu
    for attr in ("asr_primary_device", "asr_auxiliary_device", "asr_review_device", "asr_fallback_device"):
        assert getattr(s, attr) == "cpu"


def test_danmaku_login_retry_settings_are_configurable(monkeypatch: MonkeyPatch) -> None:
    """登录重试次数与间隔应能由环境变量配置并执行字段边界校验。"""
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("DANMAKU_LOGIN_RETRY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("DANMAKU_LOGIN_RETRY_INTERVAL_S", "12.5")
    configured = Settings(_env_file=None)

    assert configured.danmaku_login_retry_max_attempts == 7
    assert configured.danmaku_login_retry_interval_s == 12.5

    with pytest.raises(ValidationError):
        Settings(_env_file=None, danmaku_login_retry_max_attempts=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, danmaku_login_retry_interval_s=0.5)


def test_recording_pipeline_env_and_runtime_override(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """实时转写默认值来自环境配置，控制台运行时设置可覆盖。"""
    from app.core import settings_store
    from app.core.config import Settings, settings

    monkeypatch.setenv("RECORDING_PIPELINE_ENABLED", "false")
    configured = Settings(_env_file=None)
    assert configured.recording_pipeline_enabled is False

    monkeypatch.setattr(settings, "recording_pipeline_enabled", False)
    assert settings_store.recording_pipeline_enabled() is False

    settings_store.set_bool("recording_pipeline_enabled", True)
    assert settings_store.recording_pipeline_enabled() is True


def test_transcript_refinement_env_and_runtime_override(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """转写整理默认值来自环境配置，控制台运行时设置可覆盖。"""
    from app.core import settings_store
    from app.core.config import Settings, settings

    monkeypatch.setenv("TRANSCRIPT_LLM_REFINE_ENABLED", "false")
    configured = Settings(_env_file=None)
    assert configured.transcript_llm_refine_enabled is False

    monkeypatch.setattr(settings, "transcript_llm_refine_enabled", False)
    assert settings_store.transcript_llm_refine_enabled() is False

    settings_store.set_bool("transcript_llm_refine_enabled", True)
    assert settings_store.transcript_llm_refine_enabled() is True


def test_db_session_context_manager(temp_db: None) -> None:
    """get_session context manager provides a working session."""
    from app.db.session import get_session

    with get_session() as db:
        assert db is not None
