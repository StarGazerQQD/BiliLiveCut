from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
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
