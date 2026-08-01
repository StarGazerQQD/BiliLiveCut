"""ASR 文本质量门禁。

该模块只判断转写结果是否可进入 LLM 整理和高光分析，不尝试修正文案。
空输出和明显的连续重复退化会被拒绝，由上层切换备用 ASR 或暂停任务。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TranscriptQuality:
    """转写质量判断结果。

    :param usable: 是否允许进入后续流水线。
    :param reason: 拒绝原因；可用时为 ``None``。
    :param repetition_ratio: 最长连续重复片段占规范化文本的比例。
    :param normalized_length: 去除空白和标点后的字符数。
    """

    usable: bool
    reason: str | None
    repetition_ratio: float
    normalized_length: int


def _normalize_text(text: str) -> str:
    """保留字母、数字和中日韩文字，去除标点与空白。"""
    return "".join(character.casefold() for character in text if character.isalnum())


def _max_consecutive_repeat(text: str) -> tuple[int, int]:
    """返回最长连续重复覆盖字符数及重复次数。"""
    text_length = len(text)
    best_coverage = 0
    best_repeats = 0
    max_unit_length = min(32, text_length // 3)

    for unit_length in range(1, max_unit_length + 1):
        last_start = text_length - unit_length * 3
        for start in range(last_start + 1):
            unit = text[start : start + unit_length]
            cursor = start + unit_length
            repeats = 1
            while text[cursor : cursor + unit_length] == unit:
                repeats += 1
                cursor += unit_length
            if repeats < 3:
                continue
            coverage = repeats * unit_length
            if coverage > best_coverage:
                best_coverage = coverage
                best_repeats = repeats

    return best_coverage, best_repeats


def assess_transcript_quality(text: str) -> TranscriptQuality:
    """拒绝空文本和模型解码循环产生的明显重复文本。

    判断刻意要求连续重复覆盖较大比例，避免把正常口语中的短暂复述误判为
    模型退化。该门禁用于决定是否回退其他 ASR，以及是否允许调用 LLM。

    :param text: 待检查的 ASR 文本。
    :returns: 结构化质量结果。
    """
    normalized = _normalize_text(text)
    length = len(normalized)
    if length == 0:
        return TranscriptQuality(False, "empty_output", 0.0, 0)
    if length < 12:
        return TranscriptQuality(True, None, 0.0, length)

    dominant_ratio = max(Counter(normalized).values()) / length
    if dominant_ratio >= 0.72:
        return TranscriptQuality(False, "degenerate_repetition", dominant_ratio, length)

    repeat_coverage, repeat_count = _max_consecutive_repeat(normalized)
    repetition_ratio = repeat_coverage / length
    clearly_repeated = repeat_count >= 4 and repeat_coverage >= 12 and repetition_ratio >= 0.45
    long_repeated_block = repeat_count >= 3 and repeat_coverage >= 18 and repetition_ratio >= 0.60
    if clearly_repeated or long_repeated_block:
        return TranscriptQuality(False, "degenerate_repetition", repetition_ratio, length)

    return TranscriptQuality(True, None, repetition_ratio, length)
