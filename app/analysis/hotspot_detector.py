"""Event-first 热点信号分桶、滚动基线与无 LLM 检测器。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger
from sqlmodel import Session, select

from app.accelerators.dispatcher import danmaku_text_features, robust_relative_uplift
from app.analysis.keywords import match_keywords
from app.analysis.timeline import datetime_epoch
from app.analysis.transcription.quality import assess_transcript_quality
from app.core.config import settings
from app.db.entities import Danmaku, DanmakuType, RawSegment, Transcript
from app.db.session import get_session

if TYPE_CHECKING:
    from app.analysis.audio import AudioFeatures


_DETECTOR_VERSION = "hotspot-detector-v1"
_HIGH_EMOTION_TOKENS = (
    "卧槽",
    "绝了",
    "离谱",
    "破防",
    "高能",
    "泪目",
    "笑死",
    "无敌",
    "666",
    "??",
    "牛逼",
    "天秀",
)
_HEAT_WEIGHTS: dict[str, float] = {
    "danmaku": 0.38,
    "audio": 0.32,
    "sensevoice": 0.15,
    "asr": 0.10,
    "trend": 0.05,
}
_CLIP_WEIGHTS: dict[str, float] = {
    "danmaku": 0.25,
    "audio": 0.20,
    "sensevoice": 0.15,
    "asr": 0.30,
    "trend": 0.10,
}
_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


@dataclass(frozen=True, slots=True)
class HotspotDetectorConfig:
    """热点检测时间尺度与触发阈值的集中配置。"""

    bucket_s: float = 10.0
    baseline_window_s: float = 90.0
    detector_tick_s: float = 20.0
    min_baseline_buckets: int = 3
    detection_threshold: float = 0.55

    def __post_init__(self) -> None:
        """拒绝无法形成稳定整数窗口的配置。"""
        if not 5.0 <= self.bucket_s <= 10.0:
            raise ValueError("bucket_s 必须在 5~10 秒之间")
        if not 60.0 <= self.baseline_window_s <= 120.0:
            raise ValueError("baseline_window_s 必须在 60~120 秒之间")
        if not 15.0 <= self.detector_tick_s <= 30.0:
            raise ValueError("detector_tick_s 必须在 15~30 秒之间")
        baseline_ratio = self.baseline_window_s / self.bucket_s
        tick_ratio = self.detector_tick_s / self.bucket_s
        if not baseline_ratio.is_integer() or not tick_ratio.is_integer():
            raise ValueError("baseline_window_s 和 detector_tick_s 必须是 bucket_s 的整数倍")
        if not 2 <= self.min_baseline_buckets < int(baseline_ratio):
            raise ValueError("min_baseline_buckets 必须小于滚动基线 bucket 数")
        if not 0.0 <= self.detection_threshold <= 1.0:
            raise ValueError("detection_threshold 必须在 0~1 之间")

    @classmethod
    def from_settings(cls) -> HotspotDetectorConfig:
        """从全局设置构造检测配置。"""
        return cls(
            bucket_s=settings.hotspot_bucket_s,
            baseline_window_s=settings.hotspot_baseline_window_s,
            detector_tick_s=settings.hotspot_detector_tick_s,
            min_baseline_buckets=settings.hotspot_min_baseline_buckets,
            detection_threshold=settings.hotspot_detection_threshold,
        )


@dataclass(frozen=True, slots=True)
class SignalBucket:
    """一个短时间桶内的原始可用信号；``None`` 表示该模态不可用。"""

    start_ts: datetime
    end_ts: datetime
    danmaku_count: int | None = None
    danmaku_unique_users: int | None = None
    danmaku_repetition: float | None = None
    danmaku_intensity: float | None = None
    danmaku_high_emotion: float | None = None
    representative_messages: tuple[str, ...] = ()
    audio_rms_mean: float | None = None
    audio_rms_peak: float | None = None
    audio_energy_change: float | None = None
    audio_silence_ratio: float | None = None
    audio_local_peak: float | None = None
    audio_peak_offset_s: float | None = None
    sensevoice_intensity: float | None = None
    sensevoice_events: tuple[str, ...] = ()
    asr_text: str | None = None
    asr_keyword_score: float | None = None
    asr_speech_rate: float | None = None
    trend_score: float | None = None
    trend_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """可扩展的热点证据；``type`` 不设枚举以允许未来 ``visual``。"""

    type: str
    start_ts: datetime
    end_ts: datetime
    metrics: Mapping[str, float | int | str]
    excerpts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """转换为可稳定 JSON 序列化的证据对象。"""
        return {
            "type": self.type,
            "start_ts": self.start_ts.isoformat(),
            "end_ts": self.end_ts.isoformat(),
            "metrics": dict(self.metrics),
            "excerpts": list(self.excerpts),
        }


@dataclass(frozen=True, slots=True)
class HotspotDraft:
    """HotspotDetector 的纯计算产物。"""

    event_key: str
    session_id: int
    start_ts: datetime
    peak_ts: datetime
    end_ts: datetime
    heat_score: float
    clip_score: float
    semantic_confidence: float
    evidence_coverage: float
    features: Mapping[str, object]
    evidence: tuple[EvidenceItem, ...]
    transcript_text: str | None

    def to_payload(self) -> dict[str, object]:
        """转换为跨 compute/commit 边界使用的无 ORM 载荷。"""
        return {
            "event_key": self.event_key,
            "session_id": self.session_id,
            "start_ts": self.start_ts,
            "peak_ts": self.peak_ts,
            "end_ts": self.end_ts,
            "heat_score": self.heat_score,
            "clip_score": self.clip_score,
            "semantic_confidence": self.semantic_confidence,
            "evidence_coverage": self.evidence_coverage,
            "features_json": json.dumps(self.features, ensure_ascii=False, allow_nan=False),
            "evidence_json": json.dumps(
                {"version": 1, "items": [item.to_dict() for item in self.evidence]},
                ensure_ascii=False,
                allow_nan=False,
            ),
            "transcript_text": self.transcript_text,
        }


@dataclass(frozen=True, slots=True)
class _TickDetection:
    """一个 detector tick 的内部检测结果。"""

    start_ts: datetime
    peak_ts: datetime
    end_ts: datetime
    heat_score: float
    clip_score: float
    semantic_confidence: float
    evidence_coverage: float
    features: Mapping[str, object]
    evidence: tuple[EvidenceItem, ...]
    transcript_text: str | None


def dynamic_weighted_score(
    scores: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> tuple[float, float]:
    """只按可用模态重归一化，并返回分数与证据覆盖率。"""
    total_weight = sum(max(0.0, value) for value in weights.values())
    available = {
        name: (_clamp(score), max(0.0, weights.get(name, 0.0)))
        for name, score in scores.items()
        if score is not None and math.isfinite(score) and weights.get(name, 0.0) > 0.0
    }
    available_weight = sum(weight for _score, weight in available.values())
    if total_weight <= 0.0 or available_weight <= 0.0:
        return 0.0, 0.0
    value = sum(score * weight for score, weight in available.values()) / available_weight
    return _clamp(value), _clamp(available_weight / total_weight)


class HotspotDetector:
    """使用短桶和滚动基线生成高召回 provisional 热点。"""

    def __init__(self, config: HotspotDetectorConfig | None = None) -> None:
        self.config = config or HotspotDetectorConfig.from_settings()

    def detect(self, session_id: int, buckets: Sequence[SignalBucket]) -> list[HotspotDraft]:
        """检测按时间升序排列的信号桶并合并连续触发 tick。"""
        _validate_buckets(buckets)
        if len(buckets) <= self.config.min_baseline_buckets:
            return []
        baseline_count = int(self.config.baseline_window_s / self.config.bucket_s)
        tick_count = int(self.config.detector_tick_s / self.config.bucket_s)
        detections: list[_TickDetection] = []
        start_index = self.config.min_baseline_buckets
        for index in range(start_index, len(buckets), tick_count):
            current = buckets[index : index + tick_count]
            if not current:
                break
            baseline = buckets[max(0, index - baseline_count) : index]
            if len(baseline) < self.config.min_baseline_buckets:
                continue
            detection = self._detect_tick(current, baseline)
            if detection is not None:
                detections.append(detection)
        return self._merge_detections(session_id, detections)

    def _detect_tick(
        self,
        current: Sequence[SignalBucket],
        baseline: Sequence[SignalBucket],
    ) -> _TickDetection | None:
        modality_scores: dict[str, float | None] = {
            "danmaku": _danmaku_modality_score(current, baseline),
            "audio": _audio_modality_score(current, baseline),
            "sensevoice": _sensevoice_modality_score(current),
            "asr": _asr_modality_score(current, baseline),
            "trend": _trend_modality_score(current),
        }
        heat_score, evidence_coverage = dynamic_weighted_score(modality_scores, _HEAT_WEIGHTS)
        clip_score, _ = dynamic_weighted_score(modality_scores, _CLIP_WEIGHTS)
        semantic_scores: dict[str, float | None] = {
            "asr": modality_scores["asr"],
            "trend": modality_scores["trend"],
            "danmaku": _danmaku_semantic_score(current),
        }
        semantic_confidence, _semantic_coverage = dynamic_weighted_score(
            semantic_scores,
            {"asr": 0.70, "trend": 0.15, "danmaku": 0.15},
        )
        strongest_modality = max((score for score in modality_scores.values() if score is not None), default=0.0)
        trigger_score = max(heat_score, clip_score, strongest_modality * 0.70)
        if trigger_score < self.config.detection_threshold:
            return None

        start_ts = current[0].start_ts
        end_ts = current[-1].end_ts
        peak_bucket = max(current, key=_bucket_peak_strength)
        evidence = _build_tick_evidence(current, baseline, modality_scores)
        transcript_text = _join_bucket_text(current)
        features: dict[str, object] = {
            "detector_version": _DETECTOR_VERSION,
            "bucket_s": self.config.bucket_s,
            "baseline_window_s": self.config.baseline_window_s,
            "detector_tick_s": self.config.detector_tick_s,
            "baseline_bucket_count": len(baseline),
            "modality_scores": modality_scores,
            "trigger_score": trigger_score,
        }
        return _TickDetection(
            start_ts=start_ts,
            peak_ts=_bucket_midpoint(peak_bucket),
            end_ts=end_ts,
            heat_score=heat_score,
            clip_score=clip_score,
            semantic_confidence=semantic_confidence,
            evidence_coverage=evidence_coverage,
            features=features,
            evidence=evidence,
            transcript_text=transcript_text or None,
        )

    def _merge_detections(
        self,
        session_id: int,
        detections: Sequence[_TickDetection],
    ) -> list[HotspotDraft]:
        if not detections:
            return []
        groups: list[list[_TickDetection]] = []
        for detection in detections:
            if groups and datetime_epoch(detection.start_ts) <= datetime_epoch(groups[-1][-1].end_ts) + 1e-6:
                groups[-1].append(detection)
            else:
                groups.append([detection])

        drafts: list[HotspotDraft] = []
        for group in groups:
            peak = max(group, key=lambda item: max(item.heat_score, item.clip_score))
            peak_slot = int(datetime_epoch(peak.peak_ts) // self.config.detector_tick_s)
            key_source = f"{_DETECTOR_VERSION}:{session_id}:{peak_slot}"
            event_key = f"hotspot:{hashlib.sha256(key_source.encode()).hexdigest()}"
            feature_ticks = [dict(item.features) for item in group]
            evidence = tuple(item for detection in group for item in detection.evidence)
            transcript = " ".join(text for text in (item.transcript_text for item in group) if text).strip()
            drafts.append(
                HotspotDraft(
                    event_key=event_key,
                    session_id=session_id,
                    start_ts=group[0].start_ts,
                    peak_ts=peak.peak_ts,
                    end_ts=group[-1].end_ts,
                    heat_score=max(item.heat_score for item in group),
                    clip_score=max(item.clip_score for item in group),
                    semantic_confidence=peak.semantic_confidence,
                    evidence_coverage=peak.evidence_coverage,
                    features={"detector_version": _DETECTOR_VERSION, "ticks": feature_ticks},
                    evidence=evidence,
                    transcript_text=transcript or None,
                )
            )
        return drafts


def detect_segment_hotspots(
    segment_id: int,
    *,
    audio_features: AudioFeatures | None = None,
    config: HotspotDetectorConfig | None = None,
) -> list[HotspotDraft]:
    """从一个录制分段构建真实信号桶并生成 provisional 草稿。"""
    buckets, session_id = build_segment_signal_buckets(
        segment_id,
        audio_features=audio_features,
        config=config,
    )
    return HotspotDetector(config).detect(session_id, buckets)


def build_segment_signal_buckets(
    segment_id: int,
    *,
    audio_features: AudioFeatures | None = None,
    config: HotspotDetectorConfig | None = None,
) -> tuple[list[SignalBucket], int]:
    """把弹幕、音频、SenseVoice、ASR 与缓存趋势对齐为短时间桶。"""
    cfg = config or HotspotDetectorConfig.from_settings()
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        if segment.start_ts is None or segment.end_ts is None:
            return [], segment.session_id
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).first()
        from app.analysis.source_policy import session_danmaku_lag_s, session_has_danmaku

        lag_s = session_danmaku_lag_s(segment.session_id)
        lag = timedelta(seconds=lag_s)
        query_start = _database_datetime(segment.start_ts + lag)
        query_end = _database_datetime(segment.end_ts + lag)
        danmaku_rows = db.exec(
            select(Danmaku).where(
                Danmaku.session_id == segment.session_id,
                Danmaku.msg_type == DanmakuType.DANMAKU,
                Danmaku.ts >= query_start,
                Danmaku.ts < query_end,
            )
        ).all()
        start_ts = segment.start_ts
        end_ts = segment.end_ts
        session_id = segment.session_id

    duration_s = max(0.0, datetime_epoch(end_ts) - datetime_epoch(start_ts))
    bucket_count = max(1, math.ceil(duration_s / cfg.bucket_s))
    builders = [_BucketBuilder() for _ in range(bucket_count)]
    _fill_danmaku(builders, danmaku_rows, start_ts, duration_s, cfg.bucket_s, lag_s=lag_s)
    _fill_audio(builders, audio_features, duration_s, cfg.bucket_s)
    _fill_transcript(builders, transcript, duration_s, cfg.bucket_s)
    _fill_sensevoice(builders, transcript, duration_s, cfg.bucket_s)

    buckets: list[SignalBucket] = []
    previous_audio_mean: float | None = None
    for index, builder in enumerate(builders):
        bucket_start = start_ts + timedelta(seconds=index * cfg.bucket_s)
        bucket_end = min(end_ts, bucket_start + timedelta(seconds=cfg.bucket_s))
        builder.danmaku_available = session_has_danmaku(session_id, bucket_start + lag, bucket_end + lag)
        bucket = builder.freeze(
            start_ts=bucket_start,
            end_ts=bucket_end,
            previous_audio_mean=previous_audio_mean,
        )
        previous_audio_mean = bucket.audio_rms_mean
        buckets.append(bucket)
    return buckets, session_id


def persist_provisional_hotspots(
    db: Session,
    drafts: Sequence[HotspotDraft | Mapping[str, object]],
    *,
    expected_session_id: int,
    observed_through: datetime | None = None,
) -> list[int]:
    """在调用方事务内协调 provisional 热点并返回最终主事件 ID。"""
    from app.analysis.hotspot_lifecycle import reconcile_hotspot_events

    payloads = [raw.to_payload() if isinstance(raw, HotspotDraft) else dict(raw) for raw in drafts]
    return reconcile_hotspot_events(
        db,
        payloads,
        expected_session_id=expected_session_id,
        observed_through=observed_through,
    )


class _BucketBuilder:
    """构建 ``SignalBucket`` 的可变内部缓冲。"""

    def __init__(self) -> None:
        self.danmaku_available = False
        self.danmaku_total = 0
        self.danmaku_users: set[str] = set()
        self.danmaku_texts: list[str] = []
        self.audio_values: list[tuple[float, float]] | None = None
        self.asr_available = False
        self.asr_words: list[str] = []
        self.sensevoice_available = False
        self.sensevoice: list[tuple[str, float]] = []

    def freeze(
        self,
        *,
        start_ts: datetime,
        end_ts: datetime,
        previous_audio_mean: float | None,
    ) -> SignalBucket:
        """冻结为只读信号桶并计算桶内派生特征。"""
        count = self.danmaku_total
        repetition, intensity, high_emotion, messages = danmaku_text_features(
            self.danmaku_texts,
            _HIGH_EMOTION_TOKENS,
        )
        audio_mean: float | None = None
        audio_peak: float | None = None
        silence_ratio: float | None = None
        local_peak: float | None = None
        peak_offset_s: float | None = None
        if self.audio_values:
            offsets = np.asarray([offset for offset, _value in self.audio_values], dtype=np.float64)
            values = np.asarray([value for _offset, value in self.audio_values], dtype=np.float64)
            audio_mean = float(np.mean(values))
            peak_index = int(np.argmax(values))
            audio_peak = float(values[peak_index])
            peak_offset_s = float(offsets[peak_index])
            silence_ratio = float(np.mean(values < 0.15))
            local_peak = max(0.0, audio_peak - float(np.median(values)))
        energy_change = (
            abs(audio_mean - previous_audio_mean)
            if audio_mean is not None and previous_audio_mean is not None
            else (0.0 if audio_mean is not None else None)
        )
        asr_text = "".join(self.asr_words) if self.asr_available else None
        keyword_score = match_keywords(asr_text)[0] if asr_text else (0.0 if self.asr_available else None)
        duration_s = max(1e-6, datetime_epoch(end_ts) - datetime_epoch(start_ts))
        speech_rate = len(self.asr_words) / duration_s if self.asr_available else None
        sensevoice_intensity = (
            max((score for _event, score in self.sensevoice), default=0.0) if self.sensevoice_available else None
        )
        trend_score: float | None = None
        trend_terms: tuple[str, ...] = ()
        if settings.trend_enabled and asr_text:
            from app.analysis.highlight import _trend_score

            trend_score, matched = _trend_score(asr_text)
            trend_terms = tuple(matched)
        return SignalBucket(
            start_ts=start_ts,
            end_ts=end_ts,
            danmaku_count=count if self.danmaku_available else None,
            danmaku_unique_users=len(self.danmaku_users) if self.danmaku_available else None,
            danmaku_repetition=repetition if self.danmaku_available else None,
            danmaku_intensity=intensity if self.danmaku_available else None,
            danmaku_high_emotion=high_emotion if self.danmaku_available else None,
            representative_messages=messages,
            audio_rms_mean=audio_mean,
            audio_rms_peak=audio_peak,
            audio_energy_change=energy_change,
            audio_silence_ratio=silence_ratio,
            audio_local_peak=local_peak,
            audio_peak_offset_s=peak_offset_s,
            sensevoice_intensity=sensevoice_intensity,
            sensevoice_events=tuple(event for event, _score in self.sensevoice),
            asr_text=asr_text,
            asr_keyword_score=keyword_score,
            asr_speech_rate=speech_rate,
            trend_score=trend_score,
            trend_terms=trend_terms,
        )


def _fill_danmaku(
    builders: Sequence[_BucketBuilder],
    rows: Sequence[Danmaku],
    start_ts: datetime,
    duration_s: float,
    bucket_s: float,
    *,
    lag_s: float | None = None,
) -> None:
    lag_s = settings.danmaku_event_lag_s if lag_s is None else lag_s
    start_epoch = datetime_epoch(start_ts)
    for row in rows:
        offset_s = datetime_epoch(row.ts) - lag_s - start_epoch
        index = _bucket_index(offset_s, duration_s, bucket_s, len(builders))
        if index is None:
            continue
        builders[index].danmaku_total += 1
        content = re.sub(r"\s+", " ", row.content or "").strip()
        if content:
            builders[index].danmaku_texts.append(content)
        if row.user:
            builders[index].danmaku_users.add(row.user)


def _fill_audio(
    builders: Sequence[_BucketBuilder],
    features: AudioFeatures | None,
    duration_s: float,
    bucket_s: float,
) -> None:
    if features is None or features.rms.size == 0 or features.times.size == 0:
        return
    size = min(features.rms.size, features.times.size)
    for time_s, value in zip(features.times[:size], features.rms[:size], strict=True):
        index = _bucket_index(float(time_s), duration_s, bucket_s, len(builders))
        if index is None:
            continue
        if builders[index].audio_values is None:
            builders[index].audio_values = []
        builders[index].audio_values.append((float(time_s), float(value)))


def _fill_transcript(
    builders: Sequence[_BucketBuilder],
    transcript: Transcript | None,
    duration_s: float,
    bucket_s: float,
) -> None:
    if transcript is None or transcript.words_json is None or not _transcript_semantic_usable(transcript):
        return
    try:
        words = json.loads(transcript.words_json)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(words, list):
        return
    for builder in builders:
        builder.asr_available = True
    for raw in words:
        if not isinstance(raw, dict):
            continue
        word = str(raw.get("w", "")).strip()
        try:
            start_s = float(raw.get("start", 0.0))
            end_s = float(raw.get("end", start_s))
        except (TypeError, ValueError):
            continue
        index = _bucket_index((start_s + end_s) / 2.0, duration_s, bucket_s, len(builders))
        if index is not None and word:
            builders[index].asr_words.append(word)


def _transcript_semantic_usable(transcript: Transcript) -> bool:
    """只把通过质量分类的正文作为热点语义证据。"""
    if transcript.auxiliary_json:
        try:
            auxiliary = json.loads(transcript.auxiliary_json)
        except (json.JSONDecodeError, TypeError):
            auxiliary = None
        if isinstance(auxiliary, dict):
            quality = auxiliary.get("asr_quality")
            if isinstance(quality, dict):
                state = quality.get("state")
                if state == "available":
                    return True
                if state in {"degraded", "unavailable"}:
                    return False
    return assess_transcript_quality(transcript.final_text or "").usable


def _fill_sensevoice(
    builders: Sequence[_BucketBuilder],
    transcript: Transcript | None,
    duration_s: float,
    bucket_s: float,
) -> None:
    if transcript is None or transcript.auxiliary_json is None:
        return
    try:
        payload = json.loads(transcript.auxiliary_json)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("emotions"), list):
        return
    for builder in builders:
        builder.sensevoice_available = True
    for raw in payload["emotions"]:
        if not isinstance(raw, dict):
            continue
        event_type = str(raw.get("type", "")).strip()
        try:
            start_s = float(raw.get("start", 0.0))
            end_s = float(raw.get("end", start_s))
            confidence = _clamp(float(raw.get("confidence", 1.0)))
        except (TypeError, ValueError):
            continue
        index = _bucket_index((start_s + end_s) / 2.0, duration_s, bucket_s, len(builders))
        if index is not None and event_type:
            builders[index].sensevoice.append((event_type, _sensevoice_event_weight(event_type) * confidence))


def _danmaku_modality_score(
    current: Sequence[SignalBucket],
    baseline: Sequence[SignalBucket],
) -> float | None:
    if not any(bucket.danmaku_count is not None for bucket in current):
        return None
    scale = len(current)
    current_count = float(sum(bucket.danmaku_count or 0 for bucket in current))
    current_users = float(sum(bucket.danmaku_unique_users or 0 for bucket in current))
    count_history = [float((bucket.danmaku_count or 0) * scale) for bucket in baseline]
    user_history = [float((bucket.danmaku_unique_users or 0) * scale) for bucket in baseline]
    previous_count = float(sum(bucket.danmaku_count or 0 for bucket in baseline[-scale:]))
    relative_velocity = max(0.0, current_count - previous_count) / max(previous_count, 1.0)
    burst = max(
        robust_relative_uplift(current_count, count_history),
        robust_relative_uplift(current_users, user_history),
        _clamp(relative_velocity / 2.0),
    )
    repetition = _weighted_bucket_average(current, "danmaku_repetition", "danmaku_count")
    intensity = _weighted_bucket_average(current, "danmaku_intensity", "danmaku_count")
    high_emotion = _weighted_bucket_average(current, "danmaku_high_emotion", "danmaku_count")
    intrinsic = (repetition + intensity + high_emotion) / 3.0
    return _clamp(burst * 0.70 + intrinsic * 0.30)


def _audio_modality_score(
    current: Sequence[SignalBucket],
    baseline: Sequence[SignalBucket],
) -> float | None:
    means = [bucket.audio_rms_mean for bucket in current if bucket.audio_rms_mean is not None]
    if not means:
        return None
    current_mean = float(np.mean(means))
    current_peak = max(bucket.audio_rms_peak or 0.0 for bucket in current)
    current_change = max(bucket.audio_energy_change or 0.0 for bucket in current)
    current_local = max(bucket.audio_local_peak or 0.0 for bucket in current)
    baseline_means = [bucket.audio_rms_mean for bucket in baseline if bucket.audio_rms_mean is not None]
    baseline_peaks = [bucket.audio_rms_peak for bucket in baseline if bucket.audio_rms_peak is not None]
    baseline_changes = [bucket.audio_energy_change for bucket in baseline if bucket.audio_energy_change is not None]
    baseline_local = [bucket.audio_local_peak for bucket in baseline if bucket.audio_local_peak is not None]
    if not baseline_means:
        return 0.0
    silence_values = [bucket.audio_silence_ratio for bucket in current if bucket.audio_silence_ratio is not None]
    baseline_silence = [bucket.audio_silence_ratio for bucket in baseline if bucket.audio_silence_ratio is not None]
    silence_change = 0.0
    if silence_values and baseline_silence:
        silence_change = _clamp(abs(float(np.mean(silence_values)) - float(np.median(baseline_silence))) * 2.0)
    return _clamp(
        robust_relative_uplift(current_mean, baseline_means) * 0.25
        + robust_relative_uplift(current_peak, baseline_peaks) * 0.30
        + robust_relative_uplift(current_change, baseline_changes) * 0.20
        + robust_relative_uplift(current_local, baseline_local) * 0.15
        + silence_change * 0.10
    )


def _sensevoice_modality_score(current: Sequence[SignalBucket]) -> float | None:
    values = [bucket.sensevoice_intensity for bucket in current if bucket.sensevoice_intensity is not None]
    return max(values) if values else None


def _asr_modality_score(
    current: Sequence[SignalBucket],
    baseline: Sequence[SignalBucket],
) -> float | None:
    if not any(bucket.asr_text is not None for bucket in current):
        return None
    current_text = _join_bucket_text(current)
    if not current_text:
        return 0.0
    baseline_text = _join_bucket_text(baseline)
    current_tokens = _lexical_tokens(current_text)
    baseline_tokens = _lexical_tokens(baseline_text)
    semantic_novelty = _set_novelty(current_tokens, baseline_tokens)
    topic_change = 1.0 - _jaccard(current_tokens, baseline_tokens)
    current_entities = _entity_tokens(current_text)
    baseline_entities = _entity_tokens(baseline_text)
    entity_change = _set_novelty(current_entities, baseline_entities)
    keyword_score = max(bucket.asr_keyword_score or 0.0 for bucket in current)
    speech_rate = float(np.mean([bucket.asr_speech_rate or 0.0 for bucket in current]))
    baseline_rates = [bucket.asr_speech_rate for bucket in baseline if bucket.asr_speech_rate is not None]
    speech_rate_change = robust_relative_uplift(speech_rate, baseline_rates)
    return _clamp(
        semantic_novelty * 0.25
        + keyword_score * 0.25
        + entity_change * 0.15
        + topic_change * 0.20
        + speech_rate_change * 0.15
    )


def _trend_modality_score(current: Sequence[SignalBucket]) -> float | None:
    values = [bucket.trend_score for bucket in current if bucket.trend_score is not None]
    return max(values) if values else None


def _danmaku_semantic_score(current: Sequence[SignalBucket]) -> float | None:
    messages = [message for bucket in current for message in bucket.representative_messages]
    if not messages:
        return None
    unique_ratio = len(set(messages)) / len(messages)
    intensity = _weighted_bucket_average(current, "danmaku_intensity", "danmaku_count")
    return _clamp(unique_ratio * 0.40 + intensity * 0.60)


def _build_tick_evidence(
    current: Sequence[SignalBucket],
    baseline: Sequence[SignalBucket],
    scores: Mapping[str, float | None],
) -> tuple[EvidenceItem, ...]:
    start_ts = current[0].start_ts
    end_ts = current[-1].end_ts
    evidence: list[EvidenceItem] = []
    danmaku_score = scores.get("danmaku")
    if danmaku_score is not None:
        current_count = sum(bucket.danmaku_count or 0 for bucket in current)
        previous_count = sum(bucket.danmaku_count or 0 for bucket in baseline[-len(current) :])
        evidence.append(
            EvidenceItem(
                type="danmaku",
                start_ts=start_ts,
                end_ts=end_ts,
                metrics={
                    "score": danmaku_score,
                    "count": current_count,
                    "unique_users": sum(bucket.danmaku_unique_users or 0 for bucket in current),
                    "count_velocity": current_count - previous_count,
                    "relative_velocity": max(0.0, current_count - previous_count) / max(previous_count, 1),
                    "repetition": _weighted_bucket_average(
                        current,
                        "danmaku_repetition",
                        "danmaku_count",
                    ),
                    "intensity": _weighted_bucket_average(
                        current,
                        "danmaku_intensity",
                        "danmaku_count",
                    ),
                    "high_emotion": _weighted_bucket_average(
                        current,
                        "danmaku_high_emotion",
                        "danmaku_count",
                    ),
                },
                excerpts=tuple(
                    dict.fromkeys(message for bucket in current for message in bucket.representative_messages)
                )[:3],
            )
        )
    audio_score = scores.get("audio")
    if audio_score is not None:
        peak_bucket = max(current, key=lambda bucket: bucket.audio_rms_peak or 0.0)
        evidence.append(
            EvidenceItem(
                type="audio",
                start_ts=start_ts,
                end_ts=end_ts,
                metrics={
                    "score": audio_score,
                    "rms_mean": float(
                        np.mean([bucket.audio_rms_mean for bucket in current if bucket.audio_rms_mean is not None])
                    ),
                    "rms_peak": max(bucket.audio_rms_peak or 0.0 for bucket in current),
                    "energy_change": max(bucket.audio_energy_change or 0.0 for bucket in current),
                    "silence_ratio": float(
                        np.mean(
                            [bucket.audio_silence_ratio for bucket in current if bucket.audio_silence_ratio is not None]
                        )
                    ),
                    "local_peak": max(bucket.audio_local_peak or 0.0 for bucket in current),
                    "local_peak_offset_s": peak_bucket.audio_peak_offset_s or 0.0,
                },
            )
        )
    sensevoice_score = scores.get("sensevoice")
    if sensevoice_score is not None:
        evidence.append(
            EvidenceItem(
                type="sensevoice",
                start_ts=start_ts,
                end_ts=end_ts,
                metrics={"score": sensevoice_score},
                excerpts=tuple(dict.fromkeys(event for bucket in current for event in bucket.sensevoice_events)),
            )
        )
    asr_score = scores.get("asr")
    if asr_score is not None:
        current_text = _join_bucket_text(current)
        baseline_text = _join_bucket_text(baseline)
        current_tokens = _lexical_tokens(current_text)
        baseline_tokens = _lexical_tokens(baseline_text)
        evidence.append(
            EvidenceItem(
                type="asr",
                start_ts=start_ts,
                end_ts=end_ts,
                metrics={
                    "score": asr_score,
                    "semantic_novelty": _set_novelty(current_tokens, baseline_tokens),
                    "topic_change": 1.0 - _jaccard(current_tokens, baseline_tokens),
                    "keyword_score": max(bucket.asr_keyword_score or 0.0 for bucket in current),
                    "speech_rate": float(np.mean([bucket.asr_speech_rate or 0.0 for bucket in current])),
                },
                excerpts=(current_text,) if current_text else (),
            )
        )
    trend_score = scores.get("trend")
    if trend_score is not None:
        evidence.append(
            EvidenceItem(
                type="trend",
                start_ts=start_ts,
                end_ts=end_ts,
                metrics={"score": trend_score},
                excerpts=tuple(dict.fromkeys(term for bucket in current for term in bucket.trend_terms)),
            )
        )
    return tuple(evidence)


def _validate_buckets(buckets: Sequence[SignalBucket]) -> None:
    previous_end: datetime | None = None
    for bucket in buckets:
        if datetime_epoch(bucket.start_ts) >= datetime_epoch(bucket.end_ts):
            raise ValueError("SignalBucket 必须满足 start_ts < end_ts")
        if previous_end is not None:
            gap = datetime_epoch(bucket.start_ts) - datetime_epoch(previous_end)
            if abs(gap) > 1e-6:
                raise ValueError("SignalBucket 必须按时间升序且连续")
        previous_end = bucket.end_ts


def _sensevoice_event_weight(event_type: str) -> float:
    lowered = event_type.lower()
    if "surprise" in lowered:
        return 1.0
    if "applause" in lowered:
        return 0.95
    if "laughter" in lowered or "laugh" in lowered:
        return 0.90
    if "angry" in lowered or "happy" in lowered or "emotion" in lowered:
        return 0.75
    if "music" in lowered:
        return 0.35
    return 0.60


def _weighted_bucket_average(
    buckets: Sequence[SignalBucket],
    value_name: str,
    weight_name: str,
) -> float:
    weighted = 0.0
    total = 0.0
    for bucket in buckets:
        value = getattr(bucket, value_name)
        weight = getattr(bucket, weight_name)
        if value is None:
            continue
        numeric_weight = max(1.0, float(weight or 0.0))
        weighted += float(value) * numeric_weight
        total += numeric_weight
    return weighted / total if total else 0.0


def _bucket_peak_strength(bucket: SignalBucket) -> float:
    return max(
        bucket.audio_rms_peak or 0.0,
        bucket.sensevoice_intensity or 0.0,
        bucket.danmaku_intensity or 0.0,
        bucket.danmaku_high_emotion or 0.0,
    )


def _bucket_midpoint(bucket: SignalBucket) -> datetime:
    duration_s = datetime_epoch(bucket.end_ts) - datetime_epoch(bucket.start_ts)
    return bucket.start_ts + timedelta(seconds=duration_s / 2.0)


def _join_bucket_text(buckets: Sequence[SignalBucket]) -> str:
    return " ".join(
        bucket.asr_text.strip() for bucket in buckets if bucket.asr_text is not None and bucket.asr_text.strip()
    )


def _lexical_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _WORD_PATTERN.findall(text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            tokens.update(raw[index : index + 2] for index in range(max(1, len(raw) - 1)))
        else:
            tokens.add(raw)
    return tokens


def _entity_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _WORD_PATTERN.findall(text)
        if any(char.isdigit() or char.isascii() and char.isalpha() for char in token) or 2 <= len(token) <= 8
    }


def _set_novelty(current: set[str], baseline: set[str]) -> float:
    if not current:
        return 0.0
    return _clamp(len(current - baseline) / len(current))


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _bucket_index(offset_s: float, duration_s: float, bucket_s: float, count: int) -> int | None:
    if offset_s < 0.0 or offset_s >= duration_s or count <= 0:
        return None
    return min(int(offset_s // bucket_s), count - 1)


def _database_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def log_hotspot_detection(segment_id: int, drafts: Sequence[HotspotDraft]) -> None:
    """记录一次检测摘要，不输出弹幕或转写正文。"""
    logger.info(
        "hotspot_detection_complete: segment_id={} provisional_count={}",
        segment_id,
        len(drafts),
    )
