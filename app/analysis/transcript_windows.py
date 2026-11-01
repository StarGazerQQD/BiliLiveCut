"""按媒体时间窗提取转写正文与词级条目。"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_left
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
    semantic_text: str | None = None


@dataclass(frozen=True, slots=True)
class _WordIndex:
    """保留输入顺序的只读词快照及起点、前缀最远终点索引。"""

    words: tuple[tuple[str, float, float], ...]
    order: tuple[int, ...]
    starts: tuple[float, ...]
    furthest_ends: tuple[float, ...]

    @classmethod
    def build(cls, words_json: str | None) -> _WordIndex:
        """完整验证一次输入，允许乱序、嵌套区间和零时长词。"""
        words = tuple((str(word["w"]), float(word["start"]), float(word["end"])) for word in _decode_words(words_json))
        order = tuple(sorted(range(len(words)), key=lambda index: words[index][1]))
        ends: list[float] = []
        furthest = -math.inf
        for index in order:
            furthest = max(furthest, words[index][2])
            ends.append(furthest)
        return cls(words, order, tuple(words[index][1] for index in order), tuple(ends))

    def select(self, start: float, end: float) -> list[dict[str, object]]:
        """缩小候选范围后按原输入顺序返回独立词对象。"""
        lower = bisect_left(self.furthest_ends, start)
        upper = bisect_left(self.starts, end)
        selected: list[int] = []
        for index in self.order[lower:upper]:
            _text, word_start, word_end = self.words[index]
            if (word_end > start) or (word_end == word_start and word_start >= start):
                selected.append(index)
        return [
            {"w": self.words[index][0], "start": self.words[index][1], "end": self.words[index][2]}
            for index in sorted(selected)
        ]


class TranscriptWindowCache:
    """仅在一次多峰分析中复用索引，以完整 JSON 内容区分更新后的转写。"""

    def __init__(self) -> None:
        self._indexes: dict[str | None, _WordIndex] = {}

    def get(self, words_json: str | None) -> _WordIndex:
        """相同词快照只解析一次；返回值不会暴露可变的缓存数据。"""
        index = self._indexes.get(words_json)
        if index is None:
            index = _WordIndex.build(words_json)
            self._indexes[words_json] = index
        return index


def extract_transcript_window(
    text: str,
    words_json: str | None,
    *,
    start_s: float,
    end_s: float,
    duration_s: float,
    semantic_text: str | None = None,
    word_cache: TranscriptWindowCache | None = None,
) -> TranscriptWindow:
    """只返回与指定片内时间窗重叠的转写内容。

    优先使用 ASR 词级时间戳。缺少词级时间时，仅在目标覆盖完整分段时提供
    正文；局部窗口返回空正文，避免把字符位置当作真实媒体时间。

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

    if word_cache is None:
        words = _decode_words(words_json)
        has_words = bool(words)
        selected = [word for word in words if _word_overlaps(word, start, end)]
    else:
        index = word_cache.get(words_json)
        has_words = bool(index.words)
        selected = index.select(start, end)
    complete = duration > 0 and start <= 0 and end >= duration
    if semantic_text is not None and complete:
        # 整理正文没有词对齐信息：有词时间戳的局部窗口保留精确原文，
        # 只有完整分段使用整理正文。原始词仍用于语速与字幕。
        return TranscriptWindow(
            text=semantic_text.strip(),
            words=selected,
            precise=has_words,
        )
    if has_words:
        return TranscriptWindow(
            text=_join_word_tokens(selected),
            words=selected,
            precise=True,
        )

    return TranscriptWindow(
        text=text.strip() if complete else "",
        words=[],
        precise=False,
    )


def extract_session_transcript_window(
    parts: Sequence[TimedTranscriptPart],
    *,
    start_ts: datetime,
    end_ts: datetime,
    word_cache: TranscriptWindowCache | None = None,
) -> TranscriptWindow:
    """跨连续录制分段提取一个绝对时间窗内的转写。

    词级时间戳会被换算为相对整个目标窗口的秒数，便于语速计算和后续
    时间轴展示。缺少词级时间戳时，只纳入被完整覆盖的分段正文。
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
            semantic_text=part.semantic_text,
            word_cache=word_cache,
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
    """返回统一为 UTC 的排序键。"""
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def _decode_words(words_json: str | None) -> list[dict[str, object]]:
    """解析当前唯一的 ``[{w,start,end}]`` 词级时间戳格式。"""
    if not words_json:
        return []
    try:
        raw = json.loads(words_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("词时间戳不是有效 JSON") from exc
    if not isinstance(raw, list):
        raise ValueError("词时间戳必须是 JSON 数组")

    decoded: list[dict[str, object]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"w", "start", "end"}:
            raise ValueError(f"词时间戳第 {index} 项必须且只能包含 w/start/end")
        if not isinstance(item["w"], str):
            raise ValueError(f"词时间戳第 {index} 项的 w 必须是字符串")
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"词时间戳第 {index} 项的 start/end 必须是数值") from exc
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            raise ValueError(f"词时间戳第 {index} 项的时间范围无效")
        decoded.append({"w": item["w"], "start": start, "end": end})
    return decoded


def _word_overlaps(word: dict[str, object], start_s: float, end_s: float) -> bool:
    """判断一个词级条目是否与目标时间窗重叠。"""
    word_start = float(word["start"])
    word_end = float(word["end"])
    if word_end == word_start:
        return start_s <= word_start < end_s
    return word_end > start_s and word_start < end_s


def _join_word_tokens(words: list[dict[str, object]]) -> str:
    """从当前 ``w`` 字段恢复中英文词间距。"""
    result = ""
    for word in words:
        token = str(word["w"])
        if not token:
            continue
        if result and _needs_ascii_space(result[-1], token[0]):
            result += " "
        result += token
    return re.sub(r"[\t\r\f\v]+", " ", result).strip()


def _needs_ascii_space(left: str, right: str) -> bool:
    """仅在两个 ASCII 单词字符之间补空格，避免拆散中文。"""
    return left.isascii() and right.isascii() and left.isalnum() and right.isalnum()


def _finite_float(value: float, fallback: float) -> float:
    """把非有限浮点值替换为安全回退值。"""
    number = float(value)
    return number if math.isfinite(number) else fallback
