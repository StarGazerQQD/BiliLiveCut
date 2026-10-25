from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.core.config import settings
from app.core.configuration import ConfigurationChange, configuration_view, reload_configuration, save_configuration
from app.core.runtime_settings import effective_settings, settings_scope
from app.core.settings_store import get_setting, set_settings

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_restart_values_stay_active_until_a_new_process_loads_saved_configuration(temp_db: None) -> None:
    response = save_configuration(
        ConfigurationChange(values={"asr_preload_on_start": True, "auto_recover_max_age_hours": 12})
    )
    fields = {field["key"]: field for field in response["fields"]}
    assert fields["asr_preload_on_start"]["saved"] is True
    assert fields["asr_preload_on_start"]["effective"] is False
    assert fields["asr_preload_on_start"]["pending_restart"] is True
    reload_configuration()
    with settings_scope(fresh=True):
        assert not settings.asr_preload_on_start
        assert settings.auto_recover_max_age_hours == 24
        assert get_setting("asr_preload_on_start") == "false"
    assert get_setting("asr_preload_on_start") == "false"
    script = (
        "from app.db.session import init_db; init_db(); "
        "from app.core.configuration import configuration_view; "
        "from app.core.config import settings; import json; "
        "print('RESTART:' + json.dumps([settings.asr_preload_on_start, settings.auto_recover_max_age_hours, "
        "[f['pending_restart'] for f in configuration_view()['fields'] if f['key']=='asr_preload_on_start']]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "RESTART:[true, 12, [false]]" in result.stdout
    restored = save_configuration(ConfigurationChange(reset=["asr_preload_on_start", "auto_recover_max_age_hours"]))
    assert all(not field["pending_restart"] for field in restored["fields"])


def test_immediate_controls_and_new_resource_admissions_use_current_values_within_old_tasks(temp_db: None) -> None:
    save_configuration(ConfigurationChange(values={"notify_enabled": True}))
    with settings_scope():
        save_configuration(
            ConfigurationChange(values={"notify_enabled": False, "asr_task_max_concurrency": 2, "clip_video_crf": 30})
        )
        assert settings.notify_enabled is False
        assert get_setting("notify_enabled") == "false"
        assert effective_settings().notify_enabled is False
        assert settings.asr_task_max_concurrency == 2
        assert get_setting("asr_task_max_concurrency") == "2"
        assert settings.clip_video_crf == 20
        assert effective_settings().clip_video_crf == 20
    assert settings.clip_video_crf == 30


def test_view_counts_retained_task_and_recording_snapshots_without_exposing_credentials(temp_db: None) -> None:
    with settings_scope():
        with settings_scope(fresh=True, boundary="recording"):
            response = save_configuration(
                ConfigurationChange(values={"clip_video_crf": 29, "bilibili_cookie": "saved-private-cookie"})
            )
            fields = {field["key"]: field for field in response["fields"]}
            assert fields["clip_video_crf"]["saved"] == fields["clip_video_crf"]["effective"] == 29
            assert fields["clip_video_crf"]["retained_snapshots"] == {"task": 1, "recording": 1}
            assert fields["clip_video_crf"]["pending"] is True
            assert fields["clip_video_crf"]["pending_service"] is False
            assert fields["bilibili_cookie"]["saved"] is fields["bilibili_cookie"]["effective"] is None
            assert "saved-private-cookie" not in json.dumps(response)
            assert settings.clip_video_crf == 20
            with settings_scope(fresh=True):
                assert settings.clip_video_crf == 29
                current = {field["key"]: field for field in configuration_view()["fields"]}
                assert current["clip_video_crf"]["retained_snapshots"] == {"task": 1, "recording": 1}
    current = {field["key"]: field for field in configuration_view()["fields"]}
    assert current["clip_video_crf"]["retained_snapshots"] == {"task": 0, "recording": 0}
    assert current["clip_video_crf"]["pending"] is False


def test_room_defaults_remain_frozen_for_analysis_already_in_progress(temp_db: None) -> None:
    previous = settings.highlight_threshold
    with settings_scope():
        response = save_configuration(ConfigurationChange(values={"highlight_threshold": 0.8}))
        field = next(field for field in response["fields"] if field["key"] == "highlight_threshold")
        assert field["retained_snapshots"] == {"task": 1, "recording": 0}
        assert settings.highlight_threshold == previous
        assert effective_settings().highlight_threshold == previous
        with settings_scope(fresh=True):
            assert settings.highlight_threshold == 0.8
    assert settings.highlight_threshold == 0.8


def test_failed_commit_does_not_publish_immediate_values_or_restart_intent(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    before = configuration_view()

    def fail_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, OSError("injected storage failure"))

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail_commit)
        with pytest.raises(OperationalError):
            save_configuration(ConfigurationChange(values={"notify_enabled": True, "asr_preload_on_start": True}))
    assert configuration_view() == before
    assert settings.notify_enabled is False and settings.asr_preload_on_start is False


def test_registered_storage_writes_validate_atomically_and_refresh_running_process(temp_db: None) -> None:
    with pytest.raises(ValueError):
        set_settings({"clip_video_crf": "29", "asr_task_max_concurrency": "0"})
    assert configuration_view()["revision"] == 0
    assert settings.clip_video_crf == 20
    with pytest.raises(ValueError, match="混合写入"):
        set_settings({"clip_video_crf": "29", "plugin.example.enabled": "true"})
    assert get_setting("plugin.example.enabled", "missing") == "missing"
    set_settings({"notify_enabled": "true", "bilibili_cookie": "new-private-cookie"})
    assert settings.notify_enabled is True and settings.bilibili_cookie == "new-private-cookie"
    assert configuration_view()["revision"] == configuration_view()["runtime_revision"] == 1
    with settings_scope():
        set_settings({"bilibili_cookie": ""})
        assert settings.bilibili_cookie == "new-private-cookie"
    assert settings.bilibili_cookie == ""


def test_cli_batch_save_is_one_revision_and_next_boundary_refreshes_other_process(temp_db: None) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "set-upload", "--biliup", "--auto"],
        env=dict(os.environ, PYTHONUTF8="1"),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    response = configuration_view()
    assert response["revision"] == 1 and response["runtime_revision"] == 0
    fields = {field["key"]: field for field in response["fields"]}
    assert fields["auto_upload"]["saved"] is True and fields["auto_upload"]["effective"] is False
    assert fields["auto_upload"]["pending_service"] is True
    with settings_scope():
        assert get_setting("auto_upload") == get_setting("biliup_enabled") == "true"
    response = configuration_view()
    assert response["runtime_revision"] == 1
    assert all(not field["pending_service"] for field in response["fields"])


def test_saved_override_indicator_distinguishes_defaults_explicit_defaults_and_reset(temp_db: None) -> None:
    from app.web.services.settings import get_settings_view

    assert get_settings_view()["asr_task_max_concurrency_overridden"] is False
    save_configuration(ConfigurationChange(values={"asr_task_max_concurrency": 1}))
    assert get_settings_view()["asr_task_max_concurrency_overridden"] is True
    save_configuration(ConfigurationChange(reset=["asr_task_max_concurrency"]))
    assert get_settings_view()["asr_task_max_concurrency"] == 1
    assert get_settings_view()["asr_task_max_concurrency_overridden"] is False
