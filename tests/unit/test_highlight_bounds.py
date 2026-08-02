"""候选时间边界与默认出片敏感度回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analysis.highlight import candidate_time_bounds, contiguous_recording_start
from app.analysis.scoring_config import ScoringConfig
from app.core.config import Settings
from app.db.models import LiveRoom


def test_candidate_time_bounds_keeps_sixty_seconds_of_available_context() -> None:
    """LLM 建议过晚时仍必须保留可用的 60 秒前文。"""
    base = datetime(2026, 8, 2, tzinfo=UTC)
    segment_start = base + timedelta(seconds=300)

    start, end, peak = candidate_time_bounds(
        segment_start=segment_start,
        available_start=base,
        available_end=base + timedelta(seconds=600),
        peak_offset_s=20,
        pre_roll_s=60,
        post_roll_s=30,
        suggested_start_offset_s=10,
        suggested_end_offset_s=80,
        silences=[],
    )

    assert peak == base + timedelta(seconds=320)
    assert start == base + timedelta(seconds=260)
    assert (peak - start).total_seconds() == 60
    assert end == base + timedelta(seconds=380)


def test_candidate_time_bounds_clamps_to_recorded_range() -> None:
    """录像开头不足前文或本段尚无后文时，候选不得越界。"""
    base = datetime(2026, 8, 2, tzinfo=UTC)
    start, end, _ = candidate_time_bounds(
        segment_start=base + timedelta(seconds=300),
        available_start=base + timedelta(seconds=280),
        available_end=base + timedelta(seconds=330),
        peak_offset_s=20,
        pre_roll_s=60,
        post_roll_s=30,
        suggested_start_offset_s=None,
        suggested_end_offset_s=None,
        silences=[],
    )

    assert start == base + timedelta(seconds=280)
    assert end == base + timedelta(seconds=330)


def test_contiguous_recording_start_stops_at_stream_gap() -> None:
    """前文扩展不得跨越断流缺口。"""
    from app.db.models import RawSegment

    base = datetime(2026, 8, 2, tzinfo=UTC)
    before_gap = RawSegment(
        id=1,
        session_id=1,
        seq=1,
        file_path="before-gap.ts",
        start_ts=base,
        end_ts=base + timedelta(seconds=300),
    )
    after_gap = RawSegment(
        id=2,
        session_id=1,
        seq=2,
        file_path="after-gap.ts",
        start_ts=base + timedelta(seconds=310),
        end_ts=base + timedelta(seconds=610),
    )
    current = RawSegment(
        id=3,
        session_id=1,
        seq=3,
        file_path="current.ts",
        start_ts=base + timedelta(seconds=610),
        end_ts=base + timedelta(seconds=910),
    )

    assert contiguous_recording_start([before_gap, after_gap, current], current) == after_gap.start_ts


def test_default_thresholds_are_candidate_friendly_without_schema_drift() -> None:
    """推荐阈值来自应用配置，表模型默认值保持兼容已有 Schema 指纹。"""
    settings = Settings(_env_file=None)
    room = LiveRoom(input_url="test")

    assert settings.highlight_init_threshold == pytest.approx(0.35)
    assert settings.highlight_threshold == pytest.approx(0.45)
    assert settings.highlight_review_threshold == pytest.approx(0.40)
    assert settings.highlight_auto_approve_threshold == pytest.approx(0.72)
    assert settings.auto_publish_threshold == pytest.approx(0.80)
    assert room.highlight_threshold == pytest.approx(0.65)
    assert room.review_threshold == pytest.approx(0.50)
    assert room.auto_approve_threshold == pytest.approx(0.82)
    assert ScoringConfig().pre_roll_s == pytest.approx(60)
