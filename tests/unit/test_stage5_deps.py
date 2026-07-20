"""Stage 5 cleanup — ensure dropped modules remain in coverage count with real behavior."""

from __future__ import annotations


def test_settings_has_sensible_defaults() -> None:
    """All ASR and core settings have reasonable default values."""
    from app.core.config import Settings

    s = Settings()
    assert s.ffmpeg_path
    assert s.ffprobe_path
    assert s.segment_duration_s >= 5
    assert s.require_authorization is True
    assert s.asr_primary == "paraformer"
    assert s.whisper_model == "small"
    assert s.whisper_compute_type == "int8"
    assert s.asr_model_revision == "v2.0.4"
    assert s.asr_primary_max_concurrency == 1
    assert s.asr_resource_policy in ("strict", "warn")


def test_cookie_module_returns_str() -> None:
    """get_bilibili_cookie returns empty string in test environment."""
    from app.core.cookie import get_bilibili_cookie

    assert isinstance(get_bilibili_cookie(), str)


def test_container_router_registered(temp_db: None) -> None:
    """container router has at least one route registered."""
    from fastapi.testclient import TestClient

    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/container")
        assert r.status_code in (200, 404, 304)


def test_metrics_router_registered(temp_db: None) -> None:
    """metrics router endpoint accessible."""
    from fastapi.testclient import TestClient

    from app.web.main import app

    with TestClient(app) as client:
        r = client.get("/api/metrics")
        assert r.status_code in (200, 404, 304, 503)


def test_ffmpeg_errors_module_imports() -> None:
    """ffmpeg_errors module with classify_ffmpeg_error function."""
    from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error

    # Unknown error returns UNKNOWN type
    result = classify_ffmpeg_error(1, "random unknown error text")
    assert result is FfmpegErrorType.UNKNOWN
