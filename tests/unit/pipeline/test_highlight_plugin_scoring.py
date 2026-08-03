from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.core.config import settings
from app.db.models import LiveRoom, RawSegment, RecordingSession, SystemLog, Transcript
from app.db.session import get_session
from app.pipeline.workers.analyze import (
    HighlightDecision,
    _record_plugin_dispatch,
    _score_segment_draft,
)
from app.plugins import HighlightDispatch, HighlightScoringResult
from app.plugins.manager import plugin_manager

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_segment() -> tuple[int, int]:
    started_at = datetime(2026, 1, 3, 12, 0, 0)
    with get_session() as db:
        room = LiveRoom(
            input_url="https://live.bilibili.com/123",
            room_id=123,
            highlight_threshold=0.5,
            review_threshold=0.5,
        )
        db.add(room)
        db.flush()
        assert room.id is not None
        recording = RecordingSession(room_id=room.id, started_at=started_at)
        db.add(recording)
        db.flush()
        assert recording.id is not None
        segment = RawSegment(
            session_id=recording.id,
            seq=0,
            file_path="segment.mp4",
            start_ts=started_at + timedelta(seconds=60),
            end_ts=started_at + timedelta(seconds=90),
            duration_s=30.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        db.add(Transcript(segment_id=segment.id, text="测试高光", words_json="[]"))
        db.commit()
        return segment.id, room.id


def _patch_rule_scoring(monkeypatch: MonkeyPatch, rule_score: float) -> None:
    from app.analysis import highlight, llm
    from app.pipeline.workers import analyze

    audio = AudioFeatures(
        sample_rate=16000,
        hop_s=0.1,
        times=np.asarray([1.0, 2.0]),
        rms=np.asarray([0.2, 1.0]),
        duration_s=30.0,
        silences=[],
    )
    monkeypatch.setattr(analyze.audio_mod, "analyze_audio", lambda _path: audio)
    monkeypatch.setattr(highlight, "_danmaku_score", lambda *_args: 0.0)
    monkeypatch.setattr(highlight, "_is_duplicate", lambda *_args: False)
    monkeypatch.setattr(highlight, "danmaku_score_explain", lambda *_args: {})
    monkeypatch.setattr(highlight, "weighted_rule_score", lambda *_args: rule_score)
    monkeypatch.setattr(highlight, "fuse_scores", lambda primary, *_args: primary)
    monkeypatch.setattr(llm, "judge_highlight", lambda *_args: None)
    monkeypatch.setattr(settings, "highlight_init_threshold", 0.5)
    monkeypatch.setattr(plugin_manager, "has_capability", lambda _capability: True)


def test_champion_probability_replaces_primary_score(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    segment_id, _room_id = _seed_segment()
    _patch_rule_scoring(monkeypatch, rule_score=0.1)
    monkeypatch.setattr(
        plugin_manager,
        "score_highlight",
        lambda _request: HighlightDispatch(
            plugin_id="highlight-model",
            prediction=HighlightScoringResult(
                requested_mode="champion",
                effective_mode="champion",
                champion_version=3,
                champion_probability=0.9,
                champion_threshold=0.6,
            ),
        ),
    )

    result = _score_segment_draft(segment_id)

    assert result is not None
    assert result["decision"] == HighlightDecision.CANDIDATE
    assert result["rule_score"] == 0.1
    assert result["highlight_score"] == 0.9
    metadata = json.loads(result["features_json"])["highlight_plugin"]
    assert metadata["plugin_id"] == "highlight-model"
    assert metadata["prediction"]["champion_version"] == 3


def test_shadow_probability_does_not_change_rule_decision(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    segment_id, _room_id = _seed_segment()
    _patch_rule_scoring(monkeypatch, rule_score=0.1)
    monkeypatch.setattr(
        plugin_manager,
        "score_highlight",
        lambda _request: HighlightDispatch(
            plugin_id="highlight-model",
            prediction=HighlightScoringResult(
                requested_mode="shadow",
                effective_mode="shadow",
                shadow_version=4,
                shadow_probability=0.95,
            ),
        ),
    )

    result = _score_segment_draft(segment_id)

    assert result is not None
    assert result["decision"] == HighlightDecision.BELOW_THRESHOLD
    assert result["highlight_score"] == 0.1
    metadata = json.loads(result["features_json"])["highlight_plugin"]
    assert metadata["prediction"]["shadow_probability"] == 0.95


def test_llm_reason_is_limited_to_candidate_time_window(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """高光理由不得读取候选结束后的同一原始分段内容。"""
    from app.analysis import highlight, llm
    from app.analysis.llm import HighlightJudgement
    from app.analysis.scoring_config import ScoringConfig
    from app.pipeline.workers import analyze

    segment_id, _room_id = _seed_segment()
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        transcript.text = "当前爆点。候选结束后发生的另一件事。"
        transcript.words_json = json.dumps(
            [
                {"w": "当前爆点", "start": 1.5, "end": 2.4},
                {"w": "候选结束后发生的另一件事", "start": 20, "end": 24},
            ],
            ensure_ascii=False,
        )
        db.add(transcript)

    _patch_rule_scoring(monkeypatch, rule_score=0.9)
    monkeypatch.setattr(plugin_manager, "has_capability", lambda _capability: False)
    monkeypatch.setattr(
        highlight,
        "get_scoring_config",
        lambda: ScoringConfig(pre_roll_s=1.0, post_roll_s=1.0),
    )
    captured: dict[str, object] = {}

    def fake_match_keywords(text: str) -> tuple[float, list[str]]:
        captured["keyword_text"] = text
        return 0.0, []

    def fake_danmaku_score(_session_id: int, start_ts: datetime, end_ts: datetime) -> float:
        captured["danmaku_start"] = start_ts
        captured["danmaku_end"] = end_ts
        return 0.0

    def fake_judge(text: str, _features: dict[str, float], _danmaku: str, window_start: float) -> HighlightJudgement:
        captured["text"] = text
        captured["window_start"] = window_start
        return HighlightJudgement(
            is_highlight=True,
            score=0.9,
            reason="当前爆点",
            suggested_start_offset=1.0,
            suggested_end_offset=3.0,
        )

    monkeypatch.setattr(llm, "judge_highlight", fake_judge)
    monkeypatch.setattr(analyze, "match_keywords", fake_match_keywords)
    monkeypatch.setattr(highlight, "_danmaku_score", fake_danmaku_score)

    result = _score_segment_draft(segment_id)

    assert result is not None
    assert result["decision"] == HighlightDecision.CANDIDATE
    assert captured["text"] == "当前爆点"
    assert captured["keyword_text"] == "当前爆点"
    assert captured["window_start"] == 1.0
    assert captured["danmaku_start"] == datetime(2026, 1, 3, 12, 1, 1)
    assert captured["danmaku_end"] == datetime(2026, 1, 3, 12, 1, 3)
    assert result["reason"] == "当前爆点"
    assert result["asr_text"] == "当前爆点"
    metadata = json.loads(result["features_json"])["analysis_window"]
    assert metadata == {
        "segment_id": segment_id,
        "segment_seq": 0,
        "start_offset_s": 1.0,
        "end_offset_s": 3.0,
        "precise_transcript": True,
    }


def test_degenerate_transcript_is_rejected_before_audio_or_llm(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """历史污染转写不得继续进入音频特征、插件或 LLM 分析。"""
    from app.pipeline.workers import analyze

    segment_id, _room_id = _seed_segment()
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        transcript.text = "等一下我们先看看" * 20
        db.add(transcript)

    def fail_audio(_path: str) -> None:
        raise AssertionError("退化文本不得进入高光特征计算")

    monkeypatch.setattr(analyze.audio_mod, "analyze_audio", fail_audio)

    with pytest.raises(ValueError, match="已阻止高光与 LLM 分析"):
        _score_segment_draft(segment_id)


def test_commit_records_plugin_fallback_as_structured_log(temp_db: None) -> None:
    _segment_id, room_id = _seed_segment()
    compute_result = {
        "segment_id": 7,
        "session_id": 8,
        "room_id": room_id,
        "rule_score": 0.4,
        "highlight_score": 0.4,
        "highlight_plugin": {
            "plugin_id": "highlight-model",
            "error": "RuntimeError: registry damaged",
        },
    }
    with get_session() as db:
        _record_plugin_dispatch(db, compute_result)
        db.commit()

    with get_session() as db:
        row = db.exec(select(SystemLog).where(SystemLog.event == "highlight_scoring_fallback")).one()
        assert row.level == "WARNING"
        assert row.room_id == room_id
        context = json.loads(row.context_json)
        assert context["plugin_id"] == "highlight-model"
        assert context["segment_id"] == 7
