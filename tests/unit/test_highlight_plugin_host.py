from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.analysis.audio import AudioFeatures
from app.analysis.room_config import merge_room_config
from app.db.models import Danmaku, LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session
from app.pipeline.highlight_plugins import build_highlight_scoring_request


def test_host_builds_complete_highlight_request_without_exposing_orm(temp_db: None) -> None:
    started_at = datetime(2026, 1, 2, 12, 0, 0)
    with get_session() as db:
        room = LiveRoom(
            input_url="https://live.bilibili.com/123",
            room_id=123,
            room_config_json=json.dumps({"highlight_scorer_mode": "shadow"}),
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
        db.add(
            Transcript(
                segment_id=segment.id,
                text="高光测试",
                words_json=json.dumps(
                    [
                        {"w": "高光", "start": 0.0, "end": 0.8},
                        {"w": "无效", "start": 2.0, "end": 1.0},
                    ]
                ),
                avg_logprob=-0.2,
                review_risk_score=0.1,
                auxiliary_json=json.dumps({"emotions": [{"type": "laughter"}]}),
            )
        )
        db.add(
            Danmaku(
                session_id=recording.id,
                room_id=123,
                ts=started_at + timedelta(seconds=40),
                content="基线",
                user="a",
            )
        )
        db.add(
            Danmaku(
                session_id=recording.id,
                room_id=123,
                ts=started_at + timedelta(seconds=70),
                content="爆了！",
                user="b",
            )
        )
        db.commit()
        segment_id = segment.id

    audio = AudioFeatures(
        sample_rate=16000,
        hop_s=0.1,
        times=np.asarray([0.0, 0.1]),
        rms=np.asarray([0.2, 1.0]),
        duration_s=30.0,
        silences=[(0.0, 3.0)],
    )
    request = build_highlight_scoring_request(
        segment_id,
        audio_features=audio,
        rule_score=0.42,
    )

    assert request.segment_id == segment_id
    assert request.room_mode == "shadow"
    assert request.rule_score == 0.42
    assert request.transcript_text == "高光测试"
    assert request.words is not None
    assert [word.text for word in request.words] == ["高光"]
    assert [item.content for item in request.baseline_danmaku] == ["基线"]
    assert [item.content for item in request.window_danmaku] == ["爆了！"]
    assert request.audio is not None
    assert request.audio.rms_peak == 1.0
    assert request.audio.silence_ratio == 0.1


def test_room_config_rejects_invalid_highlight_scorer_mode(temp_db: None) -> None:
    room = LiveRoom(input_url="https://live.bilibili.com/123")

    with pytest.raises(ValueError, match="inherit/off/shadow/champion"):
        merge_room_config(room, {"highlight_scorer_mode": "invalid"})
