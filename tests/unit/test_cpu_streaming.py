from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.accelerators import dispatcher
from app.accelerators.python_fallback import speedups_round2 as reference
from app.analysis.audio import AudioFeatures
from app.analysis.hotspot_detector import (
    HotspotDetector,
    HotspotDetectorConfig,
    SignalBucket,
    _BucketBuilder,
    _fill_audio,
)
from app.analysis.timeline import _HUMOR_WORDS, _REACTION_WORDS, select_representative_danmaku
from app.analysis.transcript_windows import TranscriptWindowCache, extract_transcript_window


@pytest.mark.parametrize("text", ["", "A!ß İ𐐀", "𠀀\ud800\x00🧑！²Ⅷ", "ＡＢＣﬃ１２３\n", "重复文本" * 1024])
def test_asr_normalization_preserves_unicode_expansion_and_original_indexes(text: str) -> None:
    expected = reference.normalize_asr_with_indexes(text)
    assert dispatcher.normalize_asr_with_indexes(text) == expected
    assert dispatcher.normalize_asr_text(text) == expected[0]
    assert len(expected[0]) == len(expected[1])
    for index in set(expected[1]):
        assert expected[1].count(index) == len(text[index].casefold())


def test_asr_normalization_covers_unicode_planes() -> None:
    rng = random.Random(51025)
    text = "".join(chr(rng.randrange(0x110000)) for _ in range(10000))
    assert dispatcher.normalize_asr_with_indexes(text) == reference.normalize_asr_with_indexes(text)
    assert dispatcher.normalize_asr_text(text) == reference.normalize_asr_text(text)


@pytest.mark.parametrize("bucket", [0.1, 0.3, 1.0, 10.0])
@pytest.mark.parametrize("count", [0, 1, 8])
def test_audio_indices_keep_float_floor_boundaries_and_original_order(bucket: float, count: int) -> None:
    times = np.array(
        [1.0, 0.0, 1.3, -1.0, math.inf, -math.inf, 0.3, math.nextafter(1.0, 0.0), math.nextafter(1.0, math.inf), 10.0]
    )
    times.flags.writeable = False
    assert dispatcher.audio_bucket_indices(times, 10.0, bucket, count) == reference.audio_bucket_indices(
        times, 10.0, bucket, count
    )


def test_audio_indices_reject_nan_without_index_conversion() -> None:
    with pytest.raises(ValueError):
        dispatcher.audio_bucket_indices([math.nan], 10.0, 1.0, 10)


def test_audio_buckets_keep_exact_numpy_statistics_and_first_peak() -> None:
    times = np.array([0.9, 0.2, 1.0, 0.1, 1.1, 2.0], dtype=np.float32)
    rms = np.array([0.8, 0.8, 0.1, 0.01, 0.9, 1.0], dtype=np.float32)
    features = AudioFeatures(16000, 0.1, times, rms, 2.0, [])
    builders = [_BucketBuilder(), _BucketBuilder()]
    _fill_audio(builders, features, 2.0, 1.0)
    start = datetime(2026, 10, 25)
    previous = None
    for index, builder in enumerate(builders):
        entries = [(float(t), float(v)) for t, v in zip(times, rms, strict=True) if index <= float(t) < index + 1]
        offsets = np.array([item[0] for item in entries], dtype=np.float64)
        values = np.array([item[1] for item in entries], dtype=np.float64)
        result = builder.freeze(
            start_ts=start + timedelta(seconds=index),
            end_ts=start + timedelta(seconds=index + 1),
            previous_audio_mean=previous,
        )
        assert result.audio_rms_mean == float(np.mean(values))
        assert result.audio_rms_peak == float(values[np.argmax(values)])
        assert result.audio_peak_offset_s == float(offsets[np.argmax(values)])
        assert result.audio_silence_ratio == float(np.mean(values < 0.15))
        assert result.audio_local_peak == max(0.0, float(np.max(values)) - float(np.median(values)))
        previous = result.audio_rms_mean
    assert np.array_equal(features.times, times) and np.array_equal(features.rms, rms)


@pytest.mark.parametrize("semantic", [None, "整理正文"])
def test_transcript_index_preserves_unsorted_nested_and_zero_duration_words(semantic: str | None) -> None:
    rng = random.Random(123)
    words = [
        {"w": f"词{index}", "start": float(index % 50), "end": float(index % 50 + rng.randrange(4))}
        for index in range(300)
    ]
    words.append({"w": "覆盖整个区间", "start": 0.0, "end": 90.0})
    rng.shuffle(words)
    raw = json.dumps(words)
    cache = TranscriptWindowCache()
    for start, end in [(0, 0), (0, 1), (4, 4), (4, 5), (30, 33), (49, 50), (80, 99), (100, 100), (0, 100)]:
        expected = extract_transcript_window(
            "正文", raw, start_s=start, end_s=end, duration_s=100, semantic_text=semantic
        )
        result = extract_transcript_window(
            "正文", raw, start_s=start, end_s=end, duration_s=100, semantic_text=semantic, word_cache=cache
        )
        assert result == expected
        if result.words:
            result.words[0]["w"] = "调用方修改"
            assert (
                extract_transcript_window(
                    "正文", raw, start_s=start, end_s=end, duration_s=100, semantic_text=semantic, word_cache=cache
                )
                == expected
            )


def test_transcript_cache_distinguishes_changes_and_keeps_validation() -> None:
    cache = TranscriptWindowCache()
    for text in ["原文", "修正文"]:
        raw = json.dumps([{"w": text, "start": 0, "end": 1}])
        assert extract_transcript_window("", raw, start_s=0, end_s=2, duration_s=2, word_cache=cache).text == text
    for malformed in ["{}", '[{"w":"x","start":0,"end":-1}]', '[{"w":"x","start":0,"end":1,"extra":0}]']:
        with pytest.raises(ValueError):
            extract_transcript_window("", malformed, start_s=0, end_s=2, duration_s=2, word_cache=cache)


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [None, "", "\n"],
        ["666", "666", "！", "LOL", "lol"],
        ["开播之后玩原神吧", "这节目效果笑死我了", "\ud800A！", "a" * 121],
        ["\t讲 解 这段剧情\n", "ß", "ss", "ᾈ", "😀"] * 12,
    ],
)
def test_representative_statistics_preserve_scores_order_and_frequency(messages: list[str | None]) -> None:
    assert dispatcher.representative_danmaku_statistics(
        messages, _REACTION_WORDS, _HUMOR_WORDS
    ) == reference.representative_danmaku_statistics(messages, _REACTION_WORDS, _HUMOR_WORDS)


def test_representative_choice_keeps_original_spelling_and_roles() -> None:
    result = select_representative_danmaku(["666", "666", "一起讲解这个游戏的剧情", "笑死节目效果太好了"], limit=3)
    assert result[0] == {"text": "666", "count": 2, "role": "reaction"}
    assert [row["role"] for row in result] == ["reaction", "information", "humorous"]


@pytest.mark.parametrize(
    "texts,expected",
    [
        ([], (0, 0, 0)),
        (["", ""], (2, 0, 0)),
        (["高能!", "高能!", "高能！", "???", "普通"], (2, 3, 4)),
        (["a\0b", "😀", "？？", "!！"], (1, 1, 1)),
    ],
)
def test_sentiment_statistics_keep_exact_duplicates_and_message_hit_counts(
    texts: list[str], expected: tuple[int, int, int]
) -> None:
    assert dispatcher.danmaku_sentiment_statistics(texts, ("高能", "??", "a\0b")) == expected


@pytest.mark.parametrize(
    "left,right",
    [
        (["中文", "直播ABC_1", "İΣ中", ""], ["中文直播", "ABC_1"]),
        (["中", "文", "A", "B"], ["中文", "AB"]),
        (["\ud800😀", "", "　"], []),
        (["实体123", "新话题"], ["旧话题", "实体123"]),
    ],
)
def test_per_bucket_tokens_equal_space_joined_window(left: list[str], right: list[str]) -> None:
    current = [dispatcher.novelty_features(text.strip()) for text in left]
    baseline = [dispatcher.novelty_features(text.strip()) for text in right]
    result = dispatcher.novelty_from_features(
        set().union(*(x[0] for x in current)),
        set().union(*(x[0] for x in baseline)),
        set().union(*(x[1] for x in current)),
        set().union(*(x[1] for x in baseline)),
    )
    assert result == reference.text_novelty(
        " ".join(text.strip() for text in left if text.strip()),
        " ".join(text.strip() for text in right if text.strip()),
    )


def test_rolling_detector_preserves_each_window_evidence_after_token_reuse() -> None:
    start = datetime(2026, 10, 25)
    buckets = [
        SignalBucket(
            start_ts=start + timedelta(seconds=index * 10),
            end_ts=start + timedelta(seconds=(index + 1) * 10),
            asr_text=(f"中文直播 ABC{index % 4} 新话题 " * 30) if index % 5 else "",
            asr_keyword_score=0.5,
            asr_speech_rate=3.0,
        )
        for index in range(24)
    ]
    drafts = HotspotDetector(HotspotDetectorConfig(detection_threshold=0.0)).detect(10, buckets)
    evidence = [item for draft in drafts for item in draft.evidence if item.type == "asr"]
    assert len(evidence) == 11
    for item in evidence:
        position = int((item.start_ts - start).total_seconds() / 10)
        current_text = " ".join(
            bucket.asr_text.strip()
            for bucket in buckets[position : position + 2]
            if bucket.asr_text and bucket.asr_text.strip()
        )
        baseline_text = " ".join(
            bucket.asr_text.strip()
            for bucket in buckets[max(0, position - 9) : position]
            if bucket.asr_text and bucket.asr_text.strip()
        )
        novelty, change, _ = reference.text_novelty(current_text, baseline_text)
        assert item.metrics["semantic_novelty"] == novelty
        assert item.metrics["topic_change"] == change
        assert item.excerpts == ((current_text,) if current_text else ())


@pytest.mark.parametrize(
    "peak,start,end,cooldown,threshold",
    [(10, 8, 12, 10, 0.5), (9.999, 8, 12, 10, 0.5), (100, 0, 2, 0, 0.5), (100, 2, 2, 0, 0), (5, 8, 3, 0, 0.5)],
)
def test_interval_kernel_keeps_strict_distance_and_iou_threshold(
    peak: float, start: float, end: float, cooldown: float, threshold: float
) -> None:
    selected = [(0.0, 0.0, 1.0), (30.0, 29.0, 32.0)]
    assert dispatcher.interval_conflicts(
        peak, start, end, selected, cooldown, threshold
    ) == reference.interval_conflicts(peak, start, end, selected, cooldown, threshold)


def test_bounded_srt_trims_overlap_and_clip_end_without_parsing_generated_text() -> None:
    words = [(0.0, 0.1, "甲"), (0.2, 0.25, "乙"), (0.5, 0.51, "丙"), (1.5, 1.6, "删除")]
    expected = "1\n00:00:00,000 --> 00:00:00,200\n甲\n\n2\n00:00:00,200 --> 00:00:00,500\n乙\n\n3\n00:00:00,500 --> 00:00:01,000\n丙\n"
    assert dispatcher.group_srt_blocks(words, max_chars=1, duration_s=1.0) == expected
    assert reference.group_srt_blocks(words, max_chars=1, duration_s=1.0) == expected


@pytest.mark.parametrize("duration", [0.0, 1.001, 60.031, 3601.01])
def test_bounded_srt_preserves_millisecond_rounding_and_renumbering(duration: float) -> None:
    words = [(1.001, 1.002, "甲"), (59.999999, 60.01, "乙"), (60.02, 60.025, "丙"), (3600.2, 3600.21, "丁")]
    assert dispatcher.group_srt_blocks(words, max_chars=1, duration_s=duration) == reference.group_srt_blocks(
        words, max_chars=1, duration_s=duration
    )
