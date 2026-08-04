"""转写时间窗裁剪回归测试。"""

from __future__ import annotations

import json

from app.analysis.transcript_windows import extract_transcript_window


def test_extract_transcript_window_uses_word_timestamps() -> None:
    """有词级时间戳时只保留与目标窗重叠的词。"""
    result = extract_transcript_window(
        "前文目标后文",
        json.dumps(
            [
                {"w": "前文", "start": 5, "end": 8},
                {"w": "目标", "start": 61, "end": 64},
                {"w": "边界外", "start": 90, "end": 91},
                {"w": "后文", "start": 180, "end": 185},
            ],
            ensure_ascii=False,
        ),
        start_s=60,
        end_s=90,
        duration_s=300,
    )

    assert result.text == "目标"
    assert result.precise is True
    assert [word["w"] for word in result.words] == ["目标"]


def test_extract_transcript_window_falls_back_to_proportional_slice() -> None:
    """旧转写没有词级时间戳时仍不得返回整段后续正文。"""
    result = extract_transcript_window(
        "甲乙丙丁戊己庚辛壬癸",
        None,
        start_s=20,
        end_s=40,
        duration_s=100,
    )

    assert result.text == "丙丁"
    assert result.precise is False
    assert result.words == []


def test_extract_transcript_window_keeps_precise_empty_window_empty() -> None:
    """已有有效词时间戳时，窗口内无词不得回退并误取后文。"""
    result = extract_transcript_window(
        "候选结束后发生的另一件事",
        json.dumps(
            [{"w": "候选结束后发生的另一件事", "start": 120, "end": 125}],
            ensure_ascii=False,
        ),
        start_s=10,
        end_s=20,
        duration_s=300,
    )

    assert result.text == ""
    assert result.precise is True
    assert result.words == []
