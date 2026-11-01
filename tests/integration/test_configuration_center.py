"""统一配置的持久化、原子性、生效边界和凭据回归。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.core.config import Settings, get_settings, settings
from app.core.configuration import (
    BOOTSTRAP,
    ConfigurationChange,
    ConfigurationConflict,
    RuntimeOptions,
    configuration_view,
    save_configuration,
)
from app.core.runtime_settings import settings_scope
from app.core.settings_store import get_setting

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_registry_covers_every_core_runtime_field_without_secrets(temp_db: None) -> None:
    response = save_configuration(
        ConfigurationChange(
            values={"smtp_password": "mail-private-value", "dingtalk_webhook": "https://example.invalid/private-token"}
        )
    )
    fields = {item["key"]: item for item in response["fields"]}
    assert set(fields) == set(Settings.model_fields) | set(RuntimeOptions.model_fields)
    assert len(fields) == len(response["fields"])
    assert all(item["label"] != item["key"] for item in fields.values())
    assert all(not fields[key]["editable"] and fields[key]["reason"] for key in BOOTSTRAP)
    encoded = json.dumps(response, ensure_ascii=False)
    assert "mail-private-value" not in encoded
    assert "private-token" not in encoded
    assert fields["smtp_password"]["configured"] is True


def test_web_encoder_settings_reach_next_render_and_preserve_existing_options(temp_db: None) -> None:
    from app.clipping.models import ClipOptions
    from app.web.main import app

    existing = ClipOptions.from_settings()
    client = TestClient(app)
    response = client.patch(
        "/api/settings/configuration",
        json={
            "values": {
                "clip_video_encoder": "intel",
                "clip_hardware_quality": 18,
                "clip_hardware_preset": "quality",
                "clip_loudnorm": False,
                "clip_vertical": True,
                "clip_preset": "slow",
            }
        },
    )
    assert response.status_code == 200, response.text
    current = ClipOptions.from_settings()
    assert current.video_encoder == "intel" and current.hardware_quality == 18
    assert current.hardware_preset == "quality" and current.preset == "slow"
    assert current.vertical and not current.loudnorm
    command = current.video_encoding_args()
    assert command[command.index("-c:v") + 1] == "h264_qsv"
    assert command[command.index("-global_quality") + 1] == "18"
    assert existing.video_encoder == "cpu" and existing.vertical is False
    assert (
        client.patch("/api/settings/configuration", json={"values": {"clip_video_encoder": "cpu"}}).status_code == 200
    )
    assert ClipOptions.from_settings().video_encoder == "cpu"


@pytest.mark.parametrize(
    "invalid",
    [
        {"asr_task_max_concurrency": 0},
        {"asr_primary_max_concurrency": 0},
        {"asr_primary": "unavailable-engine"},
        {"clip_preset": "not-a-preset"},
        {"clip_video_encoder": "unavailable-device"},
        {"clip_hardware_quality": 0},
        {"clip_hardware_quality": 52},
        {"clip_hardware_preset": "ultrafast"},
        {"llm_daily_budget": -1},
        {"smtp_port": 65536},
        {"trend_schedule_end": "99:45"},
        {"hotspot_asr_priority": 100},
        {"critical_disk_threshold_gb": 30},
        {"smtp_password": 123},
        {"database_url": "sqlite:///different.db"},
        {"unknown_configuration": True},
    ],
)
def test_invalid_batch_preserves_all_values(temp_db: None, invalid: dict[str, object]) -> None:
    before = configuration_view()
    with pytest.raises(ValueError):
        save_configuration(ConfigurationChange(values={"clip_video_crf": 28, **invalid}))
    assert configuration_view() == before
    assert get_setting("clip_video_crf", "missing") == "missing"


def test_cross_field_batch_and_reset_are_atomic(temp_db: None) -> None:
    saved = save_configuration(
        ConfigurationChange(
            values={"hotspot_asr_priority": 60, "near_live_asr_priority": 70, "background_asr_priority": 80}
        )
    )
    assert settings.hotspot_asr_priority == 60
    with pytest.raises(ValueError):
        save_configuration(ConfigurationChange(reset=["near_live_asr_priority"]))
    assert settings.near_live_asr_priority == 70
    restored = save_configuration(
        ConfigurationChange(
            reset=["hotspot_asr_priority", "near_live_asr_priority", "background_asr_priority"],
            revision=saved["revision"],
        )
    )
    assert restored["revision"] == saved["revision"] + 1
    assert settings.hotspot_asr_priority == get_settings().hotspot_asr_priority


def test_running_task_keeps_first_override_and_nested_snapshot(temp_db: None) -> None:
    from app.core.cookie import get_bilibili_cookie
    from app.core.settings_store import transcript_llm_refine_enabled

    with settings_scope():
        assert settings.clip_video_crf == 20
        assert transcript_llm_refine_enabled() is True
        save_configuration(
            ConfigurationChange(
                values={"clip_video_crf": 30, "transcript_llm_refine_enabled": False, "bilibili_cookie": "new-cookie"}
            )
        )
        with settings_scope():
            assert settings.clip_video_crf == 20
            assert transcript_llm_refine_enabled() is True
            assert get_bilibili_cookie() == ""
        with settings_scope(fresh=True):
            assert settings.clip_video_crf == 30
            assert transcript_llm_refine_enabled() is False
            assert get_bilibili_cookie() == "new-cookie"
        assert settings.clip_video_crf == 20
    assert settings.clip_video_crf == 30


def test_cookie_clear_is_distinct_from_reset_to_environment(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.core.cookie import get_bilibili_cookie
    from app.web.login_handler import get_cookie_info

    monkeypatch.setattr(settings, "bilibili_cookie", "SESSDATA=env-secret; DedeUserID=321")
    assert get_bilibili_cookie() == "SESSDATA=env-secret; DedeUserID=321"
    assert get_cookie_info()["has_cookie"] is True
    save_configuration(ConfigurationChange(clear=["bilibili_cookie"]))
    assert get_bilibili_cookie() == ""
    assert get_cookie_info()["has_cookie"] is False
    save_configuration(ConfigurationChange(values={"bilibili_cookie": "   "}))
    assert get_bilibili_cookie() == ""
    save_configuration(ConfigurationChange(reset=["bilibili_cookie"]))
    assert get_bilibili_cookie() == "SESSDATA=env-secret; DedeUserID=321"


def test_concurrent_stale_forms_have_one_winner(temp_db: None) -> None:
    barrier = Barrier(2)

    def attempt(value: int) -> str:
        barrier.wait(timeout=5)
        try:
            save_configuration(ConfigurationChange(values={"clip_video_crf": value}, revision=0))
        except ConfigurationConflict:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, [25, 30])) == ["conflict", "saved"]
    assert configuration_view()["revision"] == 1


def test_configuration_survives_fresh_python_process(temp_db: None) -> None:
    save_configuration(ConfigurationChange(values={"clip_video_crf": 27, "max_analyzing": 4}))
    script = "from app.db.session import init_db; init_db(); from app.core.config import settings; print(f'CONFIG:{settings.clip_video_crf}:{settings.max_analyzing}')"
    result = subprocess.run(
        [sys.executable, "-c", script], env=os.environ.copy(), capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "CONFIG:27:4" in result.stdout


def test_scoring_values_drive_actual_scoring_reader(temp_db: None) -> None:
    from app.analysis.scoring_config import get_scoring_config, scoring_defaults

    baseline = scoring_defaults().model_dump()
    changed = {**baseline, "pre_roll_s": 75, "weights": {**baseline["weights"], "audio_events": 0.9}}
    with settings_scope():
        save_configuration(ConfigurationChange(values={"scoring_configuration": changed}))
        assert get_scoring_config().pre_roll_s == baseline["pre_roll_s"]
    assert get_scoring_config().pre_roll_s == 75
    assert get_scoring_config().weights["audio_events"] == 0.9
    with pytest.raises(ValueError):
        save_configuration(
            ConfigurationChange(values={"scoring_configuration": {**changed, "weights": {"invented": 1}}})
        )
    save_configuration(ConfigurationChange(reset=["scoring_configuration"]))
    assert get_scoring_config().pre_roll_s == baseline["pre_roll_s"]


def test_port_endpoint_rejects_mixed_business_settings(temp_db: None, monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    from config.launcher_settings import load_launcher_config

    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))
    from app.web.main import app

    with TestClient(app) as client:
        response = client.patch("/api/settings/port", json={"web_port": 8088, "biliup_enabled": True})
    assert response.status_code == 422
    assert load_launcher_config(tmp_path).web_port == 8000
    assert get_setting("biliup_enabled") == "false"


def test_database_commit_failure_preserves_configuration_and_port(
    temp_db: None, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    from sqlmodel import Session

    from config.launcher_settings import load_launcher_config

    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))

    def fail_commit(self: Session) -> None:
        raise OperationalError("COMMIT", {}, RuntimeError("injected disk error"))

    with monkeypatch.context() as scoped:
        scoped.setattr(Session, "commit", fail_commit)
        with pytest.raises(OperationalError):
            save_configuration(ConfigurationChange(values={"clip_video_crf": 33}))
    assert load_launcher_config(tmp_path).web_port == 8000
    assert get_setting("clip_video_crf", "missing") == "missing"
    assert settings.clip_video_crf == 20


def test_configuration_api_masks_errors_and_reports_conflicts(temp_db: None) -> None:
    from app.web.main import app

    with TestClient(app) as client:
        saved = client.patch(
            "/api/settings/configuration", json={"values": {"smtp_password": "top-secret-password"}, "revision": 0}
        )
        assert saved.status_code == 200
        assert "top-secret-password" not in saved.text
        invalid = client.patch(
            "/api/settings/configuration", json={"values": {"biliup_upload_cmd": "private-credential-without-file"}}
        )
        assert invalid.status_code == 400
        assert "private-credential-without-file" not in invalid.text
        conflict = client.patch("/api/settings/configuration", json={"values": {"clip_video_crf": 26}, "revision": 0})
        assert conflict.status_code == 409


@pytest.mark.parametrize(
    "route,payload",
    [
        ("/api/settings/configuration", {"values": [{"smtp_password": "must-not-echo-secret"}]}),
        ("/api/llm-providers", {"providers": [{"api_key": "must-not-echo-secret"}]}),
    ],
)
def test_request_validation_never_echoes_nested_credentials(
    temp_db: None, route: str, payload: dict[str, object]
) -> None:
    from app.web.main import app

    with TestClient(app) as client:
        response = client.request("PATCH" if route.endswith("configuration") else "PUT", route, json=payload)
        assert response.status_code == 422
        assert "must-not-echo-secret" not in response.text
        assert all("input" not in error for error in response.json()["detail"])


def test_actual_cli_bootstrap_loads_web_overrides_in_new_process(temp_db: None) -> None:
    save_configuration(ConfigurationChange(values={"clip_video_crf": 29}))
    script = "from app.cli import app; import click; from app.core.config import settings; app.command('config-probe')(lambda: print(f'CLI:{settings.clip_video_crf}')); app()"
    result = subprocess.run(
        [sys.executable, "-c", script, "config-probe"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "CLI:29" in result.stdout


def test_llm_explicit_clear_duplicate_ids_and_nonfinite_prices(temp_db: None) -> None:
    from app.analysis import llm_providers as providers

    item = providers.LLMProvider("provider", "服务商", "https://example.invalid", "private", "model")
    providers.save_providers([item])
    with pytest.raises(ValueError, match="ID"):
        providers.merge_and_save([item.to_dict(), item.to_dict()])
    for price in (float("inf"), float("nan"), -1):
        with pytest.raises(ValueError, match="非负"):
            providers.merge_and_save([{**item.to_dict(), "price_input_per_m": price}])
    assert providers.load_providers()[0].api_key == "private"
    providers.merge_and_save([{**item.to_dict(), "clear_api_key": True}])
    assert providers.load_providers()[0].api_key == ""
    assert providers.public_view()[0]["api_key_set"] is False


@pytest.mark.parametrize("score", [None, 0.79, 0.8, 0.95])
@pytest.mark.parametrize(
    "global_enabled,biliup,room_enabled",
    [(False, True, True), (True, False, True), (True, True, False), (True, True, True)],
)
def test_auto_upload_requires_all_three_switches(
    temp_db: None,
    global_enabled: bool,
    biliup: bool,
    room_enabled: bool,
    score: float | None,
    pcm_wav: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.entities import HighlightCandidate, LiveRoom, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline.scheduler import advance_rendered
    from tests.approval_fixtures import seed_automatic_task

    save_configuration(ConfigurationChange(values={"auto_upload": global_enabled, "biliup_enabled": biliup}))
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.RENDERED)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        room = db.exec(select(LiveRoom)).one()
        room.auto_upload, room.auto_publish_threshold = room_enabled, 0.8
        db.add(room)
        candidate = db.get(HighlightCandidate, task.candidate_id)
        if score is None:
            task.candidate_id = None
        else:
            candidate.highlight_score = score
            db.add(candidate)
        db.add(task)
    advance_rendered()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        assert task.stage == (
            "queued_for_publish"
            if global_enabled and biliup and room_enabled and score is not None and score >= 0.8
            else "awaiting_publish_confirmation"
        )


def test_web_danmaku_settings_reach_next_task_and_invalid_change_is_atomic(temp_db: None) -> None:
    from app.clipping.models import ClipOptions
    from app.web.main import app

    before = ClipOptions.from_settings()
    client = TestClient(app)
    response = client.patch(
        "/api/settings/configuration",
        json={
            "values": {
                "clip_danmaku": True,
                "clip_danmaku_mode": "bottom",
                "clip_danmaku_font_size": 48,
                "clip_danmaku_color": "FF8000",
                "clip_danmaku_opacity": 0.5,
                "clip_danmaku_duration_s": 4.5,
                "clip_danmaku_max_lines": 3,
            }
        },
    )
    assert response.status_code == 200, response.text
    current = ClipOptions.from_settings()
    assert current.danmaku is True and current.subtitle is False
    assert current.danmaku_mode == "bottom" and current.danmaku_font_size == 48
    assert current.danmaku_color == "FF8000" and current.danmaku_opacity == 0.5
    assert current.danmaku_duration_s == 4.5 and current.danmaku_max_lines == 3
    assert before.danmaku is False
    rejected = client.patch(
        "/api/settings/configuration",
        json={
            "values": {
                "clip_danmaku": False,
                "clip_danmaku_color": "FFFFFF\nStyle: injected",
            }
        },
    )
    assert rejected.status_code == 400
    assert ClipOptions.from_settings() == current


def test_remote_asr_settings_are_atomic_and_credentials_never_echo(temp_db: None) -> None:
    from app.web.main import app

    client = TestClient(app)
    invalid = client.patch("/api/settings/configuration", json={"values": {"asr_primary": "openai_compatible"}})
    assert invalid.status_code == 400 and settings.asr_primary == "funasr_nano"
    saved = client.patch(
        "/api/settings/configuration",
        json={
            "values": {
                "asr_primary": "openai_compatible",
                "asr_remote_base_url": "https://asr.example.invalid/v1",
                "asr_remote_model": "operator-selected-model",
                "asr_remote_api_key": "unique-fixture-asr-credential",
                "asr_remote_response_format": "verbose_json",
                "asr_remote_language": "zh",
                "asr_remote_chunk_s": 120,
            }
        },
    )
    assert saved.status_code == 200, saved.text
    assert "unique-fixture-asr-credential" not in saved.text
    fields = {item["key"]: item for item in saved.json()["fields"]}
    assert fields["asr_remote_api_key"]["secret"] and fields["asr_remote_api_key"]["configured"]
    assert fields["asr_remote_base_url"]["effect"] == "next_task"
    assert settings.asr_primary == "openai_compatible" and settings.asr_remote_chunk_s == 120
    preserved = client.patch("/api/settings/configuration", json={"values": {"asr_remote_api_key": ""}})
    assert preserved.status_code == 200 and settings.asr_remote_api_key == "unique-fixture-asr-credential"
    rejected = client.patch(
        "/api/settings/configuration", json={"values": {"asr_remote_base_url": "https://user:password@host/v1"}}
    )
    assert rejected.status_code == 400
    assert settings.asr_remote_base_url == "https://asr.example.invalid/v1"
    cleared = client.patch("/api/settings/configuration", json={"clear": ["asr_remote_api_key"]})
    assert cleared.status_code == 200 and settings.asr_remote_api_key == ""
    back = client.patch("/api/settings/configuration", json={"values": {"asr_primary": "funasr_nano"}})
    assert back.status_code == 200
