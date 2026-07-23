"""从当前主程序数据库构建一次性特征上下文。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from sqlmodel import Session, select

from app.analysis.audio import analyze_audio
from app.analysis.highlight_ml.types import AudioSnapshot, DanmakuSnapshot, SegmentFeatureContext, WordTiming
from app.db.models import Danmaku, DanmakuType, RawSegment, RecordingSession, Transcript

AudioLoader = Callable[[str], AudioSnapshot | None]


def _utc_naive(value: datetime) -> datetime:
    """统一 SQLite 查询使用的无时区 UTC 时间。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _parse_words(raw: str | None) -> tuple[WordTiming, ...] | None:
    """容错解析主程序的词级时间戳 JSON。"""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    words: list[WordTiming] = []
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
        text = str(item.get("w", item.get("word", "")))
        words.append(WordTiming(text=text, start_s=start_s, end_s=end_s))
    return tuple(words)


def _parse_object(raw: str | None) -> dict[str, object] | None:
    """解析 JSON 对象，格式错误时以缺失值表示。"""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_file_audio_snapshot(file_path: str) -> AudioSnapshot | None:
    """解码一个存在的媒体文件并聚合音频特征。"""
    if not Path(file_path).is_file():
        return None
    features = analyze_audio(file_path)
    if features.rms.size == 0:
        return None
    silence_duration = sum(max(0.0, end - start) for start, end in features.silences)
    duration = max(features.duration_s, 1e-9)
    return AudioSnapshot(
        rms_peak=float(np.max(features.rms)),
        rms_median=float(np.median(features.rms)),
        rms_std=float(np.std(features.rms)),
        prominence=features.volume_score(),
        silence_ratio=float(np.clip(silence_duration / duration, 0.0, 1.0)),
    )


def load_feature_context(
    db: Session,
    segment_id: int,
    *,
    audio_loader: AudioLoader | None = None,
    baseline_lookback_s: float = 600.0,
) -> SegmentFeatureContext:
    """加载片段及其所有特征依赖，每类数据只查询或解码一次。"""
    segment = db.get(RawSegment, segment_id)
    if segment is None:
        raise ValueError(f"片段不存在: id={segment_id}")
    if segment.id is None or segment.start_ts is None or segment.end_ts is None:
        raise ValueError(f"片段缺少可评分的时间边界: id={segment_id}")

    recording = db.get(RecordingSession, segment.session_id)
    if recording is None:
        raise ValueError(f"片段所属录制会话不存在: session_id={segment.session_id}")

    transcript = db.exec(
        select(Transcript)
        .where(Transcript.segment_id == segment.id)
        .order_by(Transcript.created_at.desc(), Transcript.id.desc())
    ).first()

    start_ts = _utc_naive(segment.start_ts)
    end_ts = _utc_naive(segment.end_ts)
    if end_ts <= start_ts:
        raise ValueError(f"片段时间边界无效: id={segment_id}")
    session_started_at = _utc_naive(recording.started_at)
    history_start = max(session_started_at, start_ts - timedelta(seconds=max(0.0, baseline_lookback_s)))
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

    baseline: list[DanmakuSnapshot] = []
    window: list[DanmakuSnapshot] = []
    for row in rows:
        snapshot = DanmakuSnapshot(
            ts=_utc_naive(row.ts),
            content=row.content or "",
            user=row.user,
            value=float(row.value),
        )
        if snapshot.ts < start_ts:
            baseline.append(snapshot)
        else:
            window.append(snapshot)

    duration = segment.duration_s
    if duration is None or duration <= 0:
        duration = max((end_ts - start_ts).total_seconds(), 0.0)
    audio = audio_loader(segment.file_path) if audio_loader is not None else None

    return SegmentFeatureContext(
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
        audio=audio,
    )
