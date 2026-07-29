"""显式执行的真实 BiliLiveCut Highlight 插件加载联调。"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.plugins import HighlightFeedback, HighlightScoringRequest
from app.plugins.manager import PluginManager

pytest_plugins = ("tests.conftest",)

_PLUGIN_SOURCE_ENV = "BILILIVECUT_HIGHLIGHT_SOURCE"


def _plugin_source() -> Path:
    """返回显式配置的真实插件源码目录。"""
    configured = os.environ.get(_PLUGIN_SOURCE_ENV)
    if not configured:
        raise RuntimeError(f"请设置 {_PLUGIN_SOURCE_ENV} 后再执行真实插件联调")
    source = Path(configured).resolve()
    if not source.is_dir():
        raise RuntimeError(f"真实插件源码目录不存在: {source}")
    return source


def _copy_plugin(target: Path) -> None:
    """复制用户显式指定的插件源码，排除开发环境和运行时数据。"""
    shutil.copytree(
        _plugin_source(),
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".agents",
            ".plugin-venv",
            ".venv",
            ".env",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".coverage",
            "app",
            "bili_live_cut.egg-info",
            "build",
            "dist",
            "packaging",
            "phase-test-output",
            "scripts",
            "storage",
            "tools",
        ),
    )


def _request() -> HighlightScoringRequest:
    started_at = datetime(2026, 1, 6, 12, 0, 0)
    return HighlightScoringRequest(
        segment_id=1,
        session_id=2,
        room_id=3,
        start_ts=started_at + timedelta(seconds=60),
        end_ts=started_at + timedelta(seconds=90),
        session_started_at=started_at,
        duration_s=30.0,
        file_path="segment.mp4",
        transcript_text="真实插件加载联调",
        words=None,
        asr_avg_logprob=None,
        asr_review_risk=None,
        auxiliary=None,
        window_danmaku=(),
        baseline_danmaku=(),
        audio=None,
        rule_score=0.4,
    )


@pytest.mark.asyncio
async def test_plugin_manager_loads_real_v01_plugin_and_delivers_feedback(
    temp_db: None,
    tmp_path: Path,
) -> None:
    """验证真实 v0.1 插件可加载、评分并接收人工审核反馈。"""
    plugin_root = tmp_path / "plugins"
    plugin_directory = plugin_root / "bililivecut-highlight"
    _copy_plugin(plugin_directory)
    manager = PluginManager(plugin_root)

    await manager.start()
    descriptor = await manager.set_enabled("bililivecut-highlight", True)
    assert descriptor["version"] == "0.1.0"
    assert descriptor["capabilities"] == ["highlight_scorer"]
    result = manager.score_highlight(_request())
    assert result is not None and result.prediction is not None
    assert result.prediction.effective_mode == "off"

    schema = importlib.import_module("bililivecut_highlight.schema").DEFAULT_FEATURE_SCHEMA
    feedback = HighlightFeedback(
        plugin_id="bililivecut-highlight",
        sample_id="candidate:77",
        candidate_id=77,
        segment_id=1,
        session_id=2,
        room_id=3,
        segment_start_ts=_request().start_ts,
        label=1,
        decision="approved_solo",
        label_source="human:external:approved_solo",
        reviewed_at=datetime(2026, 1, 6, 13, 0, 0),
        schema_version=schema.version,
        schema_fingerprint=schema.fingerprint,
        feature_values={spec.name: 0.0 for spec in schema.specs},
    )
    dispatch = manager.record_highlight_feedback(feedback)

    assert dispatch.delivered is True
    assert '"sample_id": "candidate:77"' in (plugin_directory / "feedback" / "reviews.jsonl").read_text(
        encoding="utf-8"
    )
    await manager.stop()
    assert "bililivecut_highlight" not in sys.modules
