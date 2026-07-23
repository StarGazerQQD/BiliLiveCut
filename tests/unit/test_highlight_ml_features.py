"""高光模型特征 Schema 与纯计算测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from app.analysis.highlight_ml.features import extract_feature_record
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA, FeatureSchema
from app.analysis.highlight_ml.types import DanmakuSnapshot, SegmentFeatureContext, WordTiming


def _context(**overrides: object) -> SegmentFeatureContext:
    start = datetime(2026, 1, 1, 12, 1, 0)
    values: dict[str, object] = {
        "segment_id": 1,
        "session_id": 10,
        "room_id": 100,
        "start_ts": start,
        "end_ts": start + timedelta(seconds=60),
        "session_started_at": start - timedelta(seconds=60),
        "duration_s": 60.0,
        "file_path": "unused.mp4",
        "transcript_text": None,
        "words": None,
        "asr_avg_logprob": None,
        "asr_review_risk": None,
        "auxiliary": None,
        "window_danmaku": (),
        "baseline_danmaku": (),
        "audio": None,
    }
    values.update(overrides)
    return SegmentFeatureContext(**values)  # type: ignore[arg-type]


def test_schema_fingerprint_is_stable_and_sensitive_to_version() -> None:
    """相同定义指纹稳定，版本变化会改变指纹。"""
    schema = DEFAULT_FEATURE_SCHEMA
    clone = FeatureSchema(version=schema.version, specs=schema.specs)
    upgraded = FeatureSchema(version="1.0.1", specs=schema.specs)

    assert schema.fingerprint == clone.fingerprint
    assert schema.fingerprint != upgraded.fingerprint
    assert len(schema.fingerprint) == 64


def test_missing_values_are_nan_with_availability_flags() -> None:
    """缺失不是合法的零值，并有逐维可用性标志。"""
    record = extract_feature_record(_context())
    vector = record.vector(DEFAULT_FEATURE_SCHEMA)
    names = DEFAULT_FEATURE_SCHEMA.feature_names

    missing_index = names.index("audio_rms_peak")
    assert np.isnan(vector[missing_index])
    assert vector[names.index("audio_rms_peak__available")] == 0.0
    assert np.isnan(vector[names.index("danmaku_count")])
    assert vector[names.index("danmaku_count__available")] == 0.0
    assert vector[names.index("duration_s")] == 60.0
    assert vector[names.index("duration_s__available")] == 1.0
    assert vector.shape == (len(DEFAULT_FEATURE_SCHEMA.specs) * 2,)


def test_text_words_and_danmaku_features_use_only_context_data() -> None:
    """文本、词时间戳和弹幕从同一上下文计算，空值与零值语义分离。"""
    start = datetime(2026, 1, 1, 12, 1, 0)
    record = extract_feature_record(
        _context(
            transcript_text="哈哈！精彩？",
            words=(
                WordTiming("哈哈", 0.0, 0.5),
                WordTiming("精彩", 1.5, 2.0),
            ),
            window_danmaku=(
                DanmakuSnapshot(start + timedelta(seconds=2), "好！", "u1", 1.0),
                DanmakuSnapshot(start + timedelta(seconds=3), "好！", "u2", 2.0),
            ),
            baseline_danmaku=(DanmakuSnapshot(start - timedelta(seconds=20), "普通", "u3", 1.0),),
        )
    )

    assert record.values["transcript_char_count"] == 6.0
    assert record.values["word_count"] == 2.0
    assert record.values["pause_max_s"] == 1.0
    assert record.values["long_pause_ratio"] == 1.0
    assert record.values["danmaku_count"] == 2.0
    assert record.values["danmaku_unique_content_ratio"] == 0.5
    assert record.values["danmaku_value_sum"] == 3.0


def test_vectorize_rejects_undeclared_features() -> None:
    """Schema 拒绝训练或推理侧悄悄增加的列。"""
    with np.testing.assert_raises_regex(ValueError, "未声明"):
        DEFAULT_FEATURE_SCHEMA.vectorize({"unknown": 1.0})
