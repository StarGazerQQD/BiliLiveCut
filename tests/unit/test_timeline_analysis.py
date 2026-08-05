"""录制场次高光时间轴算法回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.analysis.timeline import (
    align_danmaku_window,
    confidence_score,
    interval_iou,
    source_signals,
    suppress_clustered_drafts,
)
from app.analysis.transcript_windows import TimedTranscriptPart, extract_session_transcript_window


def test_align_danmaku_window_compensates_receive_delay() -> None:
    """弹幕查询窗口应向后平移，而高光画面时间保持不变。"""
    start = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    end = start + timedelta(seconds=30)

    assert align_danmaku_window(start, end, lag_s=7.5) == (
        start + timedelta(seconds=7.5),
        end + timedelta(seconds=7.5),
    )


def test_source_signals_and_confidence_are_explainable() -> None:
    """时间轴节点应同时给出来源标签和稳定置信度。"""
    signals = source_signals({"danmaku": 0.8, "volume": 0.7, "keywords": 0.1})

    assert signals == ["弹幕高峰", "音量突增"]
    assert confidence_score(0.7, 0.8, signals) == pytest.approx(0.815)


def test_suppress_clustered_drafts_keeps_strongest_and_spreads_timeline() -> None:
    """近邻节点只保留高分项，远处事件继续保留。"""
    base = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

    def draft(peak_s: int, score: float) -> dict[str, object]:
        peak = base + timedelta(seconds=peak_s)
        return {
            "peak_ts": peak,
            "start_ts": peak - timedelta(seconds=20),
            "end_ts": peak + timedelta(seconds=12),
            "highlight_score": score,
        }

    selected = suppress_clustered_drafts(
        [draft(30, 0.6), draft(38, 0.9), draft(120, 0.7)],
        cooldown_s=25,
        iou_threshold=0.5,
        limit=4,
    )

    assert [item["peak_ts"] for item in selected] == [
        base + timedelta(seconds=38),
        base + timedelta(seconds=120),
    ]


def test_timeline_clustering_accepts_mixed_timezone_datetimes() -> None:
    """SQLite 的无时区时间与运行时 UTC 时间混合时也应稳定排序和判重。"""
    aware = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    naive = datetime(2026, 8, 5, 12, 2)
    selected = suppress_clustered_drafts(
        [
            {
                "peak_ts": naive,
                "start_ts": naive - timedelta(seconds=20),
                "end_ts": naive + timedelta(seconds=12),
                "highlight_score": 0.7,
            },
            {
                "peak_ts": aware,
                "start_ts": aware - timedelta(seconds=20),
                "end_ts": aware + timedelta(seconds=12),
                "highlight_score": 0.8,
            },
        ],
        cooldown_s=25,
        iou_threshold=0.5,
        limit=4,
    )

    assert [item["highlight_score"] for item in selected] == [0.8, 0.7]
    assert interval_iou(
        aware,
        aware + timedelta(seconds=30),
        aware.replace(tzinfo=None),
        aware.replace(tzinfo=None) + timedelta(seconds=30),
    ) == pytest.approx(1.0)


def test_session_transcript_window_crosses_segment_boundary() -> None:
    """爆点前后文本应跨过五分钟录制分段边界连续提供给 LLM。"""
    base = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    parts = [
        TimedTranscriptPart(base, base + timedelta(seconds=300), "前段结尾", None),
        TimedTranscriptPart(
            base + timedelta(seconds=300),
            base + timedelta(seconds=600),
            "后段开头",
            None,
        ),
    ]

    window = extract_session_transcript_window(
        parts,
        start_ts=base + timedelta(seconds=290),
        end_ts=base + timedelta(seconds=310),
    )

    assert window.text == "尾\n后"
    assert window.precise is False


def test_session_transcript_window_accepts_mixed_utc_representations() -> None:
    """跨段转写应兼容 SQLite 无时区值和有时区 UTC 查询窗口。"""
    aware = datetime(2026, 8, 5, tzinfo=UTC)
    part = TimedTranscriptPart(
        start_ts=aware.replace(tzinfo=None),
        end_ts=(aware + timedelta(seconds=10)).replace(tzinfo=None),
        text="第一句 第二句",
    )

    window = extract_session_transcript_window(
        [part],
        start_ts=aware,
        end_ts=aware + timedelta(seconds=10),
    )

    assert window.text == "第一句 第二句"
