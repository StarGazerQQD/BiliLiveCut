"""转写时间窗裁剪回归测试。"""

from __future__ import annotations

import json

import pytest

from app.analysis.transcript_windows import extract_transcript_window


def test_refined_text_preserves_precise_partial_window_with_uneven_speech() -> None:
    words = json.dumps([{"w": "开头密集讲话" * 20, "start": 0, "end": 10}, {"w": "结尾一句", "start": 90, "end": 95}])
    refined = "开头密集讲话，" * 20 + "结尾一句。"
    partial = extract_transcript_window("raw", words, start_s=50, end_s=100, duration_s=100, semantic_text=refined)
    assert partial.text == "结尾一句" and partial.precise
    complete = extract_transcript_window("raw", words, start_s=0, end_s=100, duration_s=100, semantic_text=refined)
    assert complete.text == refined and len(complete.words) == 2


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


@pytest.mark.parametrize("semantic_text", [None, "开场很快说完的全部整理正文"])
def test_extract_transcript_window_does_not_invent_localization_without_timestamps(semantic_text: str | None) -> None:
    result = extract_transcript_window(
        "甲乙丙丁戊己庚辛壬癸",
        None,
        start_s=20,
        end_s=40,
        duration_s=100,
        semantic_text=semantic_text,
    )

    assert result.text == ""
    assert result.precise is False
    assert result.words == []


@pytest.mark.parametrize("semantic_text", [None, "整理正文"])
def test_complete_segment_without_timestamps_keeps_all_available_text(semantic_text: str | None) -> None:
    result = extract_transcript_window(
        "完整原文", None, start_s=0, end_s=100, duration_s=100, semantic_text=semantic_text
    )
    assert result.text == (semantic_text or "完整原文")
    assert not result.precise and not result.words


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


def test_extract_transcript_window_rejects_old_word_field() -> None:
    """旧 word 字段不得被当作当前 w 字段继续读取。"""
    with pytest.raises(ValueError, match="w/start/end"):
        extract_transcript_window(
            "旧字段",
            json.dumps([{"word": "旧字段", "start": 0, "end": 1}], ensure_ascii=False),
            start_s=0,
            end_s=1,
            duration_s=1,
        )
