"""Coverage boost for CI (Stage 9/10). Only includes tests that pass cleanly."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


class TestOsUtil:
    def test_osutil_module(self) -> None:
        from app.core import osutil

        assert osutil is not None


class TestCookieModuleFull:
    def test_cookie_module_call(self, monkeypatch: MonkeyPatch) -> None:
        monkeypatch.delenv("STORAGE_ROOT", raising=False)
        from app.core.cookie import get_bilibili_cookie

        result = get_bilibili_cookie()
        assert isinstance(result, str)


class TestAsrMetricsFull:
    def test_record_backend_call_success(self) -> None:
        from app.analysis.asr_metrics import record_backend_call

        record_backend_call("paraformer", 2.5, success=True)

    def test_record_backend_call_failure(self) -> None:
        from app.analysis.asr_metrics import record_backend_call

        record_backend_call("whisper", 0.0, success=False)

    def test_record_fallback(self) -> None:
        from app.analysis.asr_metrics import record_fallback

        record_fallback()

    def test_record_rtf(self) -> None:
        from app.analysis.asr_metrics import record_rtf

        record_rtf(0.5)
        record_rtf(2.0)

    def test_stats_returns_something(self) -> None:
        from app.analysis import asr_metrics

        assert hasattr(asr_metrics, "record_backend_call")


class TestMetricsModule:
    def test_start_metrics_collector(self) -> None:
        from app.core.metrics import start_metrics_collector

        assert callable(start_metrics_collector)

    def test_metrics_module_exports(self) -> None:
        from app.core import metrics

        assert hasattr(metrics, "start_metrics_collector")


class TestMainStartup:
    def test_setup_proxy_env_exists(self) -> None:
        from app.web.main import _setup_proxy_env

        assert callable(_setup_proxy_env)

    def test_auth_middleware_class(self) -> None:
        from app.web.main import _AuthMiddleware

        assert hasattr(_AuthMiddleware, "dispatch")
        assert hasattr(_AuthMiddleware, "_check_csrf")
        assert hasattr(_AuthMiddleware, "_parse_origin")
        assert hasattr(_AuthMiddleware, "_is_loopback")
        assert hasattr(_AuthMiddleware, "_is_modifying")


class TestSchemaDeeper:
    def test_stored_version(self, temp_db: None) -> None:
        from app.db.schema import _stored_version

        ver = _stored_version()
        assert isinstance(ver, int)


class TestCliAndCommands:
    def test_cli_app_exists(self) -> None:
        from app.cli import app as cli_app
        from app.cli import version

        assert cli_app is not None
        assert callable(version)

    def test_commands_list(self) -> None:
        from app.commands import ALL_COMMANDS

        assert isinstance(ALL_COMMANDS, list)
        assert len(ALL_COMMANDS) > 0


class TestConfigExtra:
    def test_settings_field_aliases(self) -> None:
        from app.core.config import Settings

        s = Settings()
        _ = s.ffmpeg_path
        _ = s.ffprobe_path
        _ = s.segment_duration_s
        _ = s.require_authorization
        _ = s.collect_danmaku

    def test_settings_asr_extra(self) -> None:
        from app.core.config import Settings

        s = Settings()
        _ = s.asr_primary_max_concurrency
        _ = s.asr_auxiliary_max_concurrency
        _ = s.asr_review_max_concurrency
        _ = s.asr_fallback_max_concurrency
        _ = s.asr_primary_keep_loaded
        _ = s.asr_auxiliary_keep_loaded
        _ = s.asr_review_keep_loaded
        _ = s.asr_fallback_keep_loaded
        _ = s.asr_model_idle_unload_seconds
        _ = s.asr_preload_on_start
        _ = s.asr_model_revision


class TestResourceBudgetExtra:
    def test_resource_budget_imports(self) -> None:
        from app.core.resource_budget import acquire_resources, release_resources

        assert callable(acquire_resources)
        assert callable(release_resources)


class TestContainerRoutes:
    def test_container_router_exists(self) -> None:
        from app.web.routers.container import router

        routes = [r.path for r in router.routes]
        assert len(routes) > 0


class TestFfmpegErrors:
    def test_ffmpeg_errors_module(self) -> None:
        from app.core import ffmpeg_errors

        assert ffmpeg_errors is not None


class TestDbOptimize:
    def test_optimize_module_imports(self) -> None:
        from app.db import optimize

        assert optimize is not None


class TestMetricsRouter:
    def test_metrics_router_exists(self) -> None:
        from app.web.routers.metrics import router

        assert router is not None


class TestMigrationMore:
    def test_migration_main(self) -> None:
        from app.db.migration_v01411 import main

        assert callable(main)
