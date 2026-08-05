"""ASR 文本质量门禁。

该模块只判断转写结果是否可进入 LLM 整理和高光分析，不尝试修正文案。
空输出、整段退化和局部解码循环会被拒绝，由上层切换备用 ASR 或暂停任务。
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


@dataclass(frozen=True, slots=True)
class RepetitionRepair:
    """一次局部 ASR 解码循环修复结果。"""

    text: str
    changed: bool
    removed_characters: int
    original_ratio: float


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


def repair_local_decode_loop(text: str, *, max_ratio: float = 0.35) -> RepetitionRepair:
    """折叠占比较小的局部连续复读，同时保留两次自然强调。

    整段退化或覆盖比例较高的结果不会被修补，仍由上层切换备用 ASR。
    仅修复精确的连续字符重复，不对语义相近的正常复述做模糊替换。
    """
    original_quality = assess_transcript_quality(text)
    if original_quality.reason != "degenerate_repetition" or original_quality.repetition_ratio > max_ratio:
        return RepetitionRepair(text, False, 0, original_quality.repetition_ratio)

    repaired = text
    removed = 0
    for _ in range(3):
        normalized, raw_indexes = _normalize_with_indexes(repaired)
        start, unit_length, repeats = _max_consecutive_repeat_details(normalized)
        if repeats < 4 or unit_length <= 0:
            break
        keep_end_index = start + unit_length * 2 - 1
        remove_end_index = start + unit_length * repeats - 1
        if remove_end_index >= len(raw_indexes):
            break
        raw_keep_end = raw_indexes[keep_end_index] + 1
        raw_remove_end = raw_indexes[remove_end_index] + 1
        removed += raw_remove_end - raw_keep_end
        repaired = repaired[:raw_keep_end] + repaired[raw_remove_end:]
        if assess_transcript_quality(repaired).usable:
            return RepetitionRepair(repaired, True, removed, original_quality.repetition_ratio)

    return RepetitionRepair(text, False, 0, original_quality.repetition_ratio)


def _normalize_with_indexes(text: str) -> tuple[str, list[int]]:
    """规范化文本并保留规范化字符到原文下标的映射。"""
    characters: list[str] = []
    indexes: list[int] = []
    for index, character in enumerate(text):
        if character.isalnum():
            characters.append(character.casefold())
            indexes.append(index)
    return "".join(characters), indexes


def _max_consecutive_repeat_details(text: str) -> tuple[int, int, int]:
    """返回最佳连续重复的起点、单元长度和次数。"""
    text_length = len(text)
    best = (0, 0, 0)
    best_coverage = 0
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
            coverage = repeats * unit_length
            if repeats >= 3 and coverage > best_coverage:
                best = (start, unit_length, repeats)
                best_coverage = coverage
    return best


def assess_transcript_quality(text: str) -> TranscriptQuality:
    """拒绝空文本和模型解码循环产生的明显重复文本。

    局部解码循环按重复次数与绝对覆盖长度识别；整段退化再结合覆盖比例判断。
    阈值刻意保留正常口语中的短暂复述、语气强调和短口头禅。该门禁用于决定
    是否回退其他 ASR，以及是否允许调用 LLM。

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
    local_decode_loop = repeat_count >= 4 and repeat_coverage >= 16
    clearly_repeated = repeat_count >= 4 and repeat_coverage >= 12 and repetition_ratio >= 0.45
    long_repeated_block = repeat_count >= 3 and repeat_coverage >= 18 and repetition_ratio >= 0.60
    if local_decode_loop or clearly_repeated or long_repeated_block:
        return TranscriptQuality(False, "degenerate_repetition", repetition_ratio, length)

    return TranscriptQuality(True, None, repetition_ratio, length)
