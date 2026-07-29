from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.plugins import HighlightFeedback, HighlightScoringRequest
from app.plugins.manager import PluginManager, PluginStateError, PluginValidationError


def _request(segment_id: int = 1) -> HighlightScoringRequest:
    started_at = datetime(2026, 1, 1, 12, 0, 0)
    return HighlightScoringRequest(
        segment_id=segment_id,
        session_id=2,
        room_id=3,
        start_ts=started_at + timedelta(seconds=60),
        end_ts=started_at + timedelta(seconds=90),
        session_started_at=started_at,
        duration_s=30.0,
        file_path="segment.mp4",
        transcript_text="测试高光",
        words=None,
        asr_avg_logprob=None,
        asr_review_risk=None,
        auxiliary=None,
        window_danmaku=(),
        baseline_danmaku=(),
        audio=None,
        rule_score=0.4,
    )


def _feedback(candidate_id: int = 10) -> HighlightFeedback:
    return HighlightFeedback(
        plugin_id="scorer",
        sample_id=f"candidate:{candidate_id}",
        candidate_id=candidate_id,
        segment_id=1,
        session_id=2,
        room_id=3,
        segment_start_ts=datetime(2026, 1, 1, 12, 1, 0),
        label=1,
        decision="approved_solo",
        label_source="human:tester",
        reviewed_at=datetime(2026, 1, 1, 13, 0, 0),
        schema_version="1.0.0",
        schema_fingerprint="abc",
        feature_values={"duration_s": 30.0},
    )


def _write_scorer(root: Path, plugin_id: str, *, implementation: str) -> Path:
    directory = root / plugin_id
    directory.mkdir()
    manifest = {
        "id": plugin_id,
        "name": f"评分插件 {plugin_id}",
        "version": "0.1.0",
        "api_version": "1",
        "entrypoint": "main.py:Plugin",
        "settings_page": False,
        "capabilities": ["highlight_scorer"],
    }
    (directory / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (directory / "main.py").write_text(implementation, encoding="utf-8")
    return directory


@pytest.mark.asyncio
async def test_highlight_scorer_loads_sibling_package_and_is_cleaned_up(
    temp_db: None,
    tmp_path: Path,
) -> None:
    directory = _write_scorer(
        tmp_path,
        "scorer",
        implementation="""from pathlib import Path
from app.plugins import BasePlugin, HighlightScoringResult
from scorer_impl import probability

class Plugin(BasePlugin):
    def score_highlight(self, request):
        if request.segment_id == 99:
            raise RuntimeError("推理故障")
        return HighlightScoringResult(
            requested_mode="champion",
            effective_mode="champion",
            champion_version=7,
            champion_probability=probability(),
            champion_threshold=0.6,
        )

    def record_highlight_feedback(self, feedback):
        if feedback.candidate_id == 404:
            raise RuntimeError("反馈故障")
        Path(__file__).with_name("feedback.txt").write_text(
            f"{feedback.sample_id}:{feedback.label}",
            encoding="utf-8",
        )
""",
    )
    (directory / "scorer_impl.py").write_text(
        "def probability() -> float:\n    return 0.91\n",
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)

    await manager.start()
    descriptor = await manager.set_enabled("scorer", True)
    assert descriptor["capabilities"] == ["highlight_scorer"]
    assert manager.has_capability("highlight_scorer") is True

    dispatch = manager.score_highlight(_request())
    assert dispatch is not None
    assert dispatch.error is None
    assert dispatch.prediction is not None
    assert dispatch.prediction.uses_champion is True
    assert dispatch.prediction.champion_probability == 0.91

    fallback = manager.score_highlight(_request(99))
    assert fallback is not None
    assert fallback.prediction is None
    assert fallback.error == "RuntimeError: 推理故障"

    feedback_dispatch = manager.record_highlight_feedback(_feedback())
    assert feedback_dispatch.delivered is True
    assert feedback_dispatch.error is None
    assert (directory / "feedback.txt").read_text(encoding="utf-8") == "candidate:10:1"

    feedback_fallback = manager.record_highlight_feedback(_feedback(404))
    assert feedback_fallback.delivered is False
    assert feedback_fallback.error == "RuntimeError: 反馈故障"

    assert "scorer_impl" in sys.modules
    await manager.stop()
    assert "scorer_impl" not in sys.modules


@pytest.mark.asyncio
async def test_declared_highlight_capability_requires_scoring_method(
    temp_db: None,
    tmp_path: Path,
) -> None:
    _write_scorer(
        tmp_path,
        "missing",
        implementation="""from app.plugins import BasePlugin

class Plugin(BasePlugin):
    pass
""",
    )
    manager = PluginManager(tmp_path)
    await manager.start()

    with pytest.raises(PluginValidationError, match="必须实现 score_highlight"):
        await manager.set_enabled("missing", True)
    assert manager.descriptor("missing")["loaded"] is False
    await manager.stop()


@pytest.mark.asyncio
async def test_declared_highlight_capability_requires_feedback_method(
    temp_db: None,
    tmp_path: Path,
) -> None:
    _write_scorer(
        tmp_path,
        "scorer-only",
        implementation="""from app.plugins import BasePlugin, HighlightScoringResult

class Plugin(BasePlugin):
    def score_highlight(self, request):
        return HighlightScoringResult(requested_mode="off", effective_mode="off")
""",
    )
    manager = PluginManager(tmp_path)
    await manager.start()

    with pytest.raises(PluginValidationError, match="record_highlight_feedback"):
        await manager.set_enabled("scorer-only", True)
    await manager.stop()


@pytest.mark.asyncio
async def test_only_one_highlight_scorer_can_be_enabled(
    temp_db: None,
    tmp_path: Path,
) -> None:
    implementation = """from app.plugins import BasePlugin, HighlightScoringResult

class Plugin(BasePlugin):
    def score_highlight(self, request):
        return HighlightScoringResult(requested_mode="shadow", effective_mode="shadow")

    def record_highlight_feedback(self, feedback):
        return None
"""
    _write_scorer(tmp_path, "first", implementation=implementation)
    _write_scorer(tmp_path, "second", implementation=implementation)
    manager = PluginManager(tmp_path)
    await manager.start()
    await manager.set_enabled("first", True)

    with pytest.raises(PluginStateError, match="高光评分提供者已启用: first"):
        await manager.set_enabled("second", True)
    assert manager.descriptor("second")["enabled"] is False
    await manager.stop()
