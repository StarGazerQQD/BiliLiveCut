"""基于共享上下文的纯特征计算。"""

from __future__ import annotations

import math

import numpy as np

from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA, FeatureSchema
from app.analysis.highlight_ml.types import FeatureRecord, SegmentFeatureContext


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    """分母有效时返回比例，否则返回缺失。"""
    if denominator <= 0:
        return None
    return float(numerator / denominator)


def _text_features(context: SegmentFeatureContext) -> dict[str, float | None]:
    text = context.transcript_text
    if text is None:
        return {
            "transcript_char_count": None,
            "transcript_char_rate": None,
            "transcript_unique_char_ratio": None,
            "transcript_exclamation_rate": None,
            "transcript_question_rate": None,
            "transcript_laughter_rate": None,
        }
    compact = "".join(text.split())
    count = len(compact)
    if count == 0:
        return {
            "transcript_char_count": 0.0,
            "transcript_char_rate": 0.0 if context.duration_s > 0 else None,
            "transcript_unique_char_ratio": 0.0,
            "transcript_exclamation_rate": 0.0,
            "transcript_question_rate": 0.0,
            "transcript_laughter_rate": 0.0,
        }
    laughter_chars = sum(compact.count(token) for token in ("哈", "呵", "嘿"))
    return {
        "transcript_char_count": float(count),
        "transcript_char_rate": _safe_ratio(count, context.duration_s),
        "transcript_unique_char_ratio": len(set(compact)) / count,
        "transcript_exclamation_rate": sum(compact.count(token) for token in ("!", "！")) / count,
        "transcript_question_rate": sum(compact.count(token) for token in ("?", "？")) / count,
        "transcript_laughter_rate": laughter_chars / count,
    }


def _word_features(context: SegmentFeatureContext) -> dict[str, float | None]:
    words = context.words
    names = (
        "word_count",
        "word_rate",
        "word_duration_mean",
        "word_duration_std",
        "pause_mean_s",
        "pause_max_s",
        "long_pause_ratio",
    )
    if words is None:
        return dict.fromkeys(names)
    durations = np.asarray([word.end_s - word.start_s for word in words], dtype=np.float64)
    pauses = np.asarray(
        [max(0.0, current.start_s - previous.end_s) for previous, current in zip(words, words[1:], strict=False)],
        dtype=np.float64,
    )
    return {
        "word_count": float(len(words)),
        "word_rate": _safe_ratio(len(words), context.duration_s),
        "word_duration_mean": float(np.mean(durations)) if durations.size else 0.0,
        "word_duration_std": float(np.std(durations)) if durations.size else 0.0,
        "pause_mean_s": float(np.mean(pauses)) if pauses.size else 0.0,
        "pause_max_s": float(np.max(pauses)) if pauses.size else 0.0,
        "long_pause_ratio": float(np.mean(pauses > 0.8)) if pauses.size else 0.0,
    }


def _danmaku_features(context: SegmentFeatureContext) -> dict[str, float | None]:
    rows = context.window_danmaku
    if not rows and not context.baseline_danmaku:
        return {
            "danmaku_count": None,
            "danmaku_rate": None,
            "danmaku_heat_ratio": None,
            "danmaku_unique_user_ratio": None,
            "danmaku_unique_content_ratio": None,
            "danmaku_exclamation_rate": None,
            "danmaku_question_rate": None,
            "danmaku_value_sum": None,
            "danmaku_value_max": None,
        }
    count = len(rows)
    duration = context.duration_s
    rate = _safe_ratio(count, duration)
    baseline_duration = max((context.start_ts - context.session_started_at).total_seconds(), 0.0)
    baseline_duration = min(baseline_duration, 600.0)
    baseline_rate = _safe_ratio(len(context.baseline_danmaku), baseline_duration)
    if rate is None or baseline_rate is None:
        heat_ratio = None
    elif baseline_rate <= 0:
        heat_ratio = 1.0 if count > 0 else 0.0
    else:
        heat_ratio = math.log1p(rate) / math.log1p(baseline_rate) if baseline_rate > 0 else None

    contents = [row.content for row in rows if row.content]
    users = [row.user for row in rows if row.user]
    return {
        "danmaku_count": float(count),
        "danmaku_rate": rate,
        "danmaku_heat_ratio": heat_ratio,
        "danmaku_unique_user_ratio": len(set(users)) / len(users) if users else None,
        "danmaku_unique_content_ratio": len(set(contents)) / len(contents) if contents else None,
        "danmaku_exclamation_rate": (
            sum("!" in content or "！" in content for content in contents) / len(contents) if contents else None
        ),
        "danmaku_question_rate": (
            sum("?" in content or "？" in content for content in contents) / len(contents) if contents else None
        ),
        "danmaku_value_sum": float(sum(row.value for row in rows)),
        "danmaku_value_max": float(max((row.value for row in rows), default=0.0)),
    }


def _audio_features(context: SegmentFeatureContext) -> dict[str, float | None]:
    audio = context.audio
    if audio is None:
        return {
            "audio_rms_peak": None,
            "audio_rms_median": None,
            "audio_rms_std": None,
            "audio_prominence": None,
            "audio_silence_ratio": None,
        }
    return {
        "audio_rms_peak": audio.rms_peak,
        "audio_rms_median": audio.rms_median,
        "audio_rms_std": audio.rms_std,
        "audio_prominence": audio.prominence,
        "audio_silence_ratio": audio.silence_ratio,
    }


def _auxiliary_features(context: SegmentFeatureContext) -> dict[str, float | None]:
    auxiliary = context.auxiliary
    names = ("aux_laughter_count", "aux_applause_count", "aux_surprise_count", "aux_happy_count")
    if auxiliary is None:
        return dict.fromkeys(names)
    events = auxiliary.get("emotions")
    if not isinstance(events, list):
        return dict.fromkeys(names)
    counts = {name: 0.0 for name in names}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", "")).lower()
        counts["aux_laughter_count"] += float("laughter" in event_type)
        counts["aux_applause_count"] += float("applause" in event_type)
        counts["aux_surprise_count"] += float("surprise" in event_type)
        counts["aux_happy_count"] += float("happy" in event_type)
    return counts


def extract_feature_record(
    context: SegmentFeatureContext,
    schema: FeatureSchema = DEFAULT_FEATURE_SCHEMA,
) -> FeatureRecord:
    """从一次性上下文提取与 Schema 对齐的命名特征。"""
    values: dict[str, float | None] = {
        "duration_s": context.duration_s,
        "session_elapsed_s": max((context.start_ts - context.session_started_at).total_seconds(), 0.0),
        "asr_avg_logprob": context.asr_avg_logprob,
        "asr_review_risk": context.asr_review_risk,
    }
    values.update(_text_features(context))
    values.update(_word_features(context))
    values.update(_danmaku_features(context))
    values.update(_audio_features(context))
    values.update(_auxiliary_features(context))
    expected = {spec.name for spec in schema.specs}
    missing = expected.difference(values)
    if missing:
        raise RuntimeError(f"特征提取器未生成 Schema 字段: {sorted(missing)}")
    return FeatureRecord(
        segment_id=context.segment_id,
        schema_version=schema.version,
        schema_fingerprint=schema.fingerprint,
        values=values,
    )
