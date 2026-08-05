"""按媒体时间窗提取转写正文与词级条目。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class TranscriptWindow:
    """一段已经按媒体时间裁剪的转写。

    :param text: 与时间窗对应的正文。
    :param words: 落入时间窗的原始词级条目。
    :param precise: 是否使用词级时间戳完成精确裁剪。
    """

    text: str
    words: list[dict[str, object]]
    precise: bool


@dataclass(frozen=True, slots=True)
class TimedTranscriptPart:
    """带绝对媒体时间的单个录制分段转写。"""

    start_ts: datetime
    end_ts: datetime
    text: str
    words_json: str | None = None


def extract_transcript_window(
    text: str,
    words_json: str | None,
    *,
    start_s: float,
    end_s: float,
    duration_s: float,
) -> TranscriptWindow:
    """只返回与指定片内时间窗重叠的转写内容。

    优先使用 ASR 词级时间戳。旧数据没有可用词级时间戳时，按片段时长
    对正文做比例裁剪，避免把候选之后的整段内容交给文案模型。

    :param text: 原始片段的完整转写正文。
    :param words_json: 词级时间戳 JSON。
    :param start_s: 时间窗起点，相对原始片段起点的秒数。
    :param end_s: 时间窗终点，相对原始片段起点的秒数。
    :param duration_s: 原始片段时长。
    :returns: 裁剪后的正文、词级条目及精确性标记。
    """
    duration = max(0.0, _finite_float(duration_s, 0.0))
    start = max(0.0, _finite_float(start_s, 0.0))
    end = max(start, _finite_float(end_s, start))
    if duration > 0:
        start = min(start, duration)
        end = min(end, duration)

    words = _decode_words(words_json)
    selected = [word for word in words if _word_overlaps(word, start, end)]
    if words:
        return TranscriptWindow(
            text=_join_word_tokens(selected),
            words=selected,
            precise=True,
        )

    return TranscriptWindow(
        text=_proportional_text_slice(text, start_s=start, end_s=end, duration_s=duration),
        words=[],
        precise=False,
    )


def extract_session_transcript_window(
    parts: Sequence[TimedTranscriptPart],
    *,
    start_ts: datetime,
    end_ts: datetime,
) -> TranscriptWindow:
    """跨连续录制分段提取一个绝对时间窗内的转写。

    词级时间戳会被换算为相对整个目标窗口的秒数，便于语速计算和后续
    时间轴展示。缺少词级时间戳的旧数据仍按各分段时长比例裁剪。
    """
    normalized_start = _coerce_datetime_like(start_ts, start_ts)
    normalized_end = _coerce_datetime_like(end_ts, start_ts)
    if normalized_end <= normalized_start:
        return TranscriptWindow(text="", words=[], precise=False)

    texts: list[str] = []
    words: list[dict[str, object]] = []
    precise = True
    matched = False
    for part in sorted(parts, key=lambda item: _datetime_epoch(item.start_ts)):
        part_start = _coerce_datetime_like(part.start_ts, normalized_start)
        part_end = _coerce_datetime_like(part.end_ts, normalized_start)
        overlap_start = max(normalized_start, part_start)
        overlap_end = min(normalized_end, part_end)
        if overlap_end <= overlap_start:
            continue
        matched = True
        duration = max(0.0, (part_end - part_start).total_seconds())
        local_start = (overlap_start - part_start).total_seconds()
        local_end = (overlap_end - part_start).total_seconds()
        window = extract_transcript_window(
            part.text,
            part.words_json,
            start_s=local_start,
            end_s=local_end,
            duration_s=duration,
        )
        if window.text:
            texts.append(window.text)
        if not window.precise:
            precise = False
        shift = (part_start - normalized_start).total_seconds()
        for word in window.words:
            shifted = dict(word)
            shifted["start"] = float(word["start"]) + shift
            shifted["end"] = float(word["end"]) + shift
            words.append(shifted)

    return TranscriptWindow(
        text="\n".join(texts).strip(),
        words=words,
        precise=matched and precise,
    )


def _coerce_datetime_like(value: datetime, reference: datetime) -> datetime:
    """把 UTC 时间统一成与参考值相同的时区表示。"""
    if reference.tzinfo is None:
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).astimezone(reference.tzinfo)
    return value.astimezone(reference.tzinfo)


def _datetime_epoch(value: datetime) -> float:
    """返回兼容有/无时区 UTC 时间的排序键。"""
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def _decode_words(words_json: str | None) -> list[dict[str, object]]:
    """解析并保留包含有效起止时间的词级条目。"""
    if not words_json:
        return []
    try:
        raw = json.loads(words_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []

    decoded: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        normalized = dict(item)
        normalized["start"] = start
        normalized["end"] = max(start, end)
        decoded.append(normalized)
    return decoded


def _word_overlaps(word: dict[str, object], start_s: float, end_s: float) -> bool:
    """判断一个词级条目是否与目标时间窗重叠。"""
    word_start = float(word["start"])
    word_end = float(word["end"])
    if word_end == word_start:
        return start_s <= word_start < end_s
    return word_end > start_s and word_start < end_s


def _join_word_tokens(words: list[dict[str, object]]) -> str:
    """兼容 FunASR/Whisper 字段名并恢复中英文词间距。"""
    result = ""
    for word in words:
        raw = word.get("w", word.get("word", word.get("text", "")))
        token = str(raw or "")
        if not token:
            continue
        if result and _needs_ascii_space(result[-1], token[0]):
            result += " "
        result += token
    return re.sub(r"[\t\r\f\v]+", " ", result).strip()


def _needs_ascii_space(left: str, right: str) -> bool:
    """仅在两个 ASCII 单词字符之间补空格，避免拆散中文。"""
    return left.isascii() and right.isascii() and left.isalnum() and right.isalnum()


def _proportional_text_slice(text: str, *, start_s: float, end_s: float, duration_s: float) -> str:
    """在缺少词级时间戳时按时长比例近似裁剪正文。"""
    source = text.strip()
    if not source or duration_s <= 0 or end_s <= start_s:
        return source if start_s <= 0 and end_s > start_s else ""
    if start_s <= 0 and end_s >= duration_s:
        return source
    start_index = min(len(source), max(0, math.floor(len(source) * start_s / duration_s)))
    end_index = min(len(source), max(start_index, math.ceil(len(source) * end_s / duration_s)))
    return source[start_index:end_index].strip()


def _finite_float(value: float, fallback: float) -> float:
    """把非有限浮点值替换为安全回退值。"""
    number = float(value)
    return number if math.isfinite(number) else fallback
