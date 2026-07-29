"""把主程序数据转换为公开高光插件契约。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import numpy as np
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.analysis.room_config import load_room_config
from app.db.models import Danmaku, DanmakuType, LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session
from app.plugins.highlight import (
    HighlightAudio,
    HighlightDanmaku,
    HighlightScoringRequest,
    HighlightWord,
    RoomScoringMode,
)


def _utc_naive(value: datetime) -> datetime:
    """把时间统一为 SQLite 与插件契约使用的无时区 UTC。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_words(raw: str | None) -> tuple[HighlightWord, ...] | None:
    """容错解析主程序词时间戳。"""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    words: list[HighlightWord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            start_s = float(item["start"])
            end_s = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(start_s) or not np.isfinite(end_s) or end_s < start_s:
            continue
        words.append(
            HighlightWord(
                text=str(item.get("w", item.get("word", ""))),
                start_s=start_s,
                end_s=end_s,
            )
        )
    return tuple(words)


def _parse_object(raw: str | None) -> dict[str, object] | None:
    """解析可选 JSON 对象，格式无效时保留缺失语义。"""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _audio_snapshot(features: AudioFeatures) -> HighlightAudio | None:
    """复用规则评分已经解码的音频，不执行第二次 FFmpeg。"""
    if features.rms.size == 0:
        return None
    duration = max(features.duration_s, 1e-9)
    silence_duration = sum(max(0.0, end - start) for start, end in features.silences)
    return HighlightAudio(
        rms_peak=float(np.max(features.rms)),
        rms_median=float(np.median(features.rms)),
        rms_std=float(np.std(features.rms)),
        prominence=features.volume_score(),
        silence_ratio=float(np.clip(silence_duration / duration, 0.0, 1.0)),
    )


def _room_mode(room: LiveRoom | None) -> RoomScoringMode:
    """读取房间级高光评分插件模式覆盖。"""
    config = load_room_config(room)
    raw = config.get("highlight_scorer_mode", "inherit")
    return cast(RoomScoringMode, raw) if raw in {"inherit", "off", "shadow", "champion"} else "inherit"


def build_highlight_scoring_request(
    segment_id: int,
    *,
    audio_features: AudioFeatures,
    rule_score: float,
    baseline_lookback_s: float = 600.0,
) -> HighlightScoringRequest:
    """按公开 DTO 加载一次片段上下文，插件不接触 ORM 或 Session。"""
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None or segment.id is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        if segment.start_ts is None or segment.end_ts is None:
            raise ValueError(f"片段缺少可评分的时间边界: id={segment_id}")
        recording = db.get(RecordingSession, segment.session_id)
        if recording is None or recording.id is None:
            raise ValueError(f"片段所属录制会话不存在: session_id={segment.session_id}")
        room = db.get(LiveRoom, recording.room_id)
        transcript = db.exec(
            select(Transcript)
            .where(Transcript.segment_id == segment.id)
            .order_by(Transcript.created_at.desc(), Transcript.id.desc())
        ).first()

        start_ts = _utc_naive(segment.start_ts)
        end_ts = _utc_naive(segment.end_ts)
        session_started_at = _utc_naive(recording.started_at)
        history_start = max(
            session_started_at,
            start_ts - timedelta(seconds=max(0.0, baseline_lookback_s)),
        )
        rows = db.exec(
            select(Danmaku)
            .where(
                Danmaku.session_id == segment.session_id,
                Danmaku.msg_type == DanmakuType.DANMAKU,
                Danmaku.ts >= history_start,
                Danmaku.ts <= end_ts,
            )
            .order_by(Danmaku.ts)
        ).all()

        baseline: list[HighlightDanmaku] = []
        window: list[HighlightDanmaku] = []
        for row in rows:
            snapshot = HighlightDanmaku(
                ts=_utc_naive(row.ts),
                content=row.content or "",
                user=row.user,
                value=float(row.value),
            )
            (baseline if snapshot.ts < start_ts else window).append(snapshot)

        duration = segment.duration_s
        if duration is None or duration <= 0:
            duration = max((end_ts - start_ts).total_seconds(), 0.0)
        return HighlightScoringRequest(
            segment_id=segment.id,
            session_id=segment.session_id,
            room_id=recording.room_id,
            start_ts=start_ts,
            end_ts=end_ts,
            session_started_at=session_started_at,
            duration_s=float(duration),
            file_path=segment.file_path,
            transcript_text=transcript.text if transcript is not None else None,
            words=_parse_words(transcript.words_json) if transcript is not None else None,
            asr_avg_logprob=transcript.avg_logprob if transcript is not None else None,
            asr_review_risk=transcript.review_risk_score if transcript is not None else None,
            auxiliary=_parse_object(transcript.auxiliary_json) if transcript is not None else None,
            window_danmaku=tuple(window),
            baseline_danmaku=tuple(baseline),
            audio=_audio_snapshot(audio_features),
            rule_score=rule_score,
            room_mode=_room_mode(room),
        )


__all__ = ["build_highlight_scoring_request"]
