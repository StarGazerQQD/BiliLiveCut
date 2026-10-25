"""BiliLiveCut 原生加速统一分派层。

所有编译扩展只存在于 ``app.accelerators`` 命名空间。业务代码只调用本
模块；当前解释器缺少某个编译扩展时，按函数粒度回退到同语义 Python
参考实现，不保留旧 ``app.analysis`` 扩展接口。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime as _dt
from functools import lru_cache
from math import isfinite
from typing import Any, TypedDict

from rapidfuzz.distance import Levenshtein

from app.accelerators.python_fallback import speedups_round2 as _fallback_round2


class ClusterItem(TypedDict, total=False):
    """矩阵 CPU 边界所需的候选字段，业务对象及数据库保持在调用方。"""

    asr_text: str | None
    keywords: list[str] | None
    start_ts: _dt | str | None


_logger = logging.getLogger(__name__)


# C: Aho-Corasick、字符 bigram、余弦相似度和梗词计数。
try:
    from app.accelerators._c_speedups import (
        danmaku_sentiment_statistics as _native_sentiment_statistics,
    )
    from app.accelerators._c_speedups import (  # type: ignore[import]
        fast_aho_has_match,
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_meme_count,
    )
    from app.accelerators._c_speedups import (
        fast_match_keywords as _uncached_match_keywords,
    )

    _BACKEND = "C"
    _logger.info("加速模块(C): 已加载 app.accelerators._c_speedups")
except ImportError:
    from app.accelerators.python_fallback.speedups import (  # type: ignore[no-redef]
        fast_aho_has_match,
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_meme_count,
    )
    from app.accelerators.python_fallback.speedups import (
        fast_match_keywords as _uncached_match_keywords,
    )

    _BACKEND = "python"
    _logger.info("加速模块(C): 使用 Python 参考实现")


# Cython: PCM、文本新颖度、音频峰值/静音、稳健滚动基线、弹幕基线和 SRT 组装。
try:
    from app.accelerators import _cython_speedups as _cython_backend  # type: ignore[import]

    _CYTHON_BACKEND = "Cython"
    _logger.info("加速模块(Cython): 已加载 app.accelerators._cython_speedups")
except ImportError:
    _cython_backend = None
    _CYTHON_BACKEND = "python"
    _logger.info("加速模块(Cython): 使用 Python 参考实现")


# C++17: 独立的精确重复检测内核；只接收 Unicode 文本，不接触设备。
try:
    from app.accelerators import _repeat_speedups as _repeat_backend  # type: ignore[import]

    _logger.info("加速模块(C++): 已加载 app.accelerators._repeat_speedups")
except ImportError:
    _repeat_backend = None
    _logger.info("加速模块(C++): 使用 Python 重复检测参考实现")


# Rust: rayon 聚类矩阵、并查集/簇内统计与弹幕文本特征。
try:
    from app.accelerators import _rust_speedups as _rust_backend  # type: ignore[import]

    _RUST_BACKEND = "Rust+rayon"
    _logger.info("加速模块(Rust): 已加载 app.accelerators._rust_speedups")
except ImportError:
    _rust_backend = None
    _RUST_BACKEND = "python"
    _logger.info("加速模块(Rust): 使用 Python 参考实现")


@lru_cache(maxsize=8)
def _keyword_automaton(patterns: tuple[str, ...]) -> object:
    return fast_ahocorasick_build(patterns)


def fast_match_keywords(text: str, patterns: tuple[str, ...]) -> list[str]:
    """复用有界关键词自动机；按完整词表隔离房间和配置变更。"""
    if not text or not patterns:
        return []
    # 限制保留的词表与节点规模；大词表仍走完整匹配，不截断命中结果。
    if len(patterns) <= 64 and all(isinstance(word, str) for word in patterns):
        words = tuple(patterns)
        if sum(map(len, words)) <= 512:
            return fast_ahocorasick_search(_keyword_automaton(words), text)
    return _uncached_match_keywords(text, patterns)


def danmaku_sentiment_statistics(texts: list[str], memes: tuple[str, ...]) -> tuple[int, int, int]:
    """复用梗词自动机并合并重复率与感叹号统计，不修改情绪公式。"""
    if not texts:
        return 0, 0, 0
    if _BACKEND == "C":
        automaton = (
            _keyword_automaton(memes)
            if len(memes) <= 64 and sum(map(len, memes)) <= 512
            else fast_ahocorasick_build(memes)
        )
        return _native_sentiment_statistics(texts, automaton)
    counts: dict[str, int] = {}
    exclamations = 0
    for text in texts:
        counts[text] = counts.get(text, 0) + 1
        exclamations += "!" in text or "！" in text
    return max(counts.values()), exclamations, fast_meme_count(texts, memes)


def levenshtein_distance(a: str, b: str) -> int:
    """计算逐 Unicode 字符的精确编辑距离，保留复核阈值和原因中的比率。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    return Levenshtein.distance(a, b)


def _float64_contiguous(values: Sequence[float]) -> Any:
    """把数值序列转换为 Cython ``double[::1]`` 所需的连续缓冲区。"""
    import numpy as np

    return np.ascontiguousarray(values, dtype=np.float64)


def representative_danmaku_statistics(
    messages: Sequence[str | None], reaction_words: set[str], humor_words: tuple[str, ...]
) -> list[tuple[str, str, int, int, float, float, float]]:
    """一次生成代表弹幕排序所需的全部稳定统计。"""
    if _cython_backend is None:
        return _fallback_round2.representative_danmaku_statistics(messages, reaction_words, humor_words)
    return _cython_backend.representative_danmaku_statistics(messages, reaction_words, humor_words)


def normalize_asr_text(text: str) -> str:
    """执行保留 Unicode 语义的 ASR 规范化。"""
    if _cython_backend is None:
        return _fallback_round2.normalize_asr_text(text)
    return _cython_backend.normalize_asr_text(text)


def normalize_asr_with_indexes(text: str) -> tuple[str, list[int]]:
    """规范化 ASR 文本并返回精确原文位置。"""
    if _cython_backend is None:
        return _fallback_round2.normalize_asr_with_indexes(text)
    return _cython_backend.normalize_asr_with_indexes(text)


def interval_conflicts(
    peak: float,
    start: float,
    end: float,
    selected: list[tuple[float, float, float]],
    cooldown_s: float,
    iou_threshold: float,
) -> bool:
    """检查候选与已选区间，时间对象和业务排序由调用方处理。"""
    if _cython_backend is None:
        return _fallback_round2.interval_conflicts(peak, start, end, selected, cooldown_s, iou_threshold)
    return _cython_backend.interval_conflicts(peak, start, end, selected, cooldown_s, iou_threshold)


def audio_bucket_indices(times: Sequence[float], duration_s: float, bucket_s: float, count: int) -> list[list[int]]:
    """为主机端 RMS 数组构建稳定分桶索引。"""
    if _cython_backend is None:
        return _fallback_round2.audio_bucket_indices(times, duration_s, bucket_s, count)
    return _cython_backend.audio_bucket_indices(_float64_contiguous(times), duration_s, bucket_s, count)


def consecutive_repeat_details(text: str) -> tuple[int, int, int]:
    """检测精确字符循环；不访问模型、张量或设备。"""
    if not isinstance(text, str):
        raise TypeError("text 必须是 str")
    if len(text) < 3:
        return 0, 0, 0
    if _repeat_backend is None:
        return _fallback_round2.consecutive_repeat_details(text)
    return _repeat_backend.consecutive_repeat_details(text)


def review_text_statistics(text: str, hotwords: list[str] | None = None) -> tuple[int, int, str | None]:
    """返回复核所需的字符计数及首个热词冲突。"""
    if _cython_backend is None:
        return _fallback_round2.review_text_statistics(text, hotwords)
    return _cython_backend.review_text_statistics(text, hotwords)


def pcm16_statistics(raw: bytes) -> tuple[int, int, int]:
    """在独立 CPU 内核中汇总 PCM16，不接收设备张量或执行文件 I/O。"""
    if _cython_backend is None:
        return _fallback_round2.pcm16_statistics(raw)
    return _cython_backend.pcm16_statistics(raw)


def audio_peak_offsets(
    times: Sequence[float],
    rms: Sequence[float],
    *,
    limit: int = 4,
    min_distance_s: float = 25.0,
    min_prominence: float = 0.15,
) -> list[float]:
    """选择按能量排序且彼此分离的局部峰值。"""
    if _cython_backend is None:
        return _fallback_round2.audio_peak_offsets(times, rms, limit, min_distance_s, min_prominence)
    return _cython_backend.audio_peak_offsets(
        _float64_contiguous(times),
        _float64_contiguous(rms),
        limit,
        min_distance_s,
        min_prominence,
    )


def find_silence_ranges(
    times: Sequence[float],
    rms: Sequence[float],
    *,
    threshold_ratio: float = 0.15,
    min_silence_s: float = 0.3,
) -> list[tuple[float, float]]:
    """从 RMS 包络中提取连续静音区间。"""
    if _cython_backend is None:
        return _fallback_round2.find_silence_ranges(times, rms, threshold_ratio, min_silence_s)
    return _cython_backend.find_silence_ranges(
        _float64_contiguous(times),
        _float64_contiguous(rms),
        threshold_ratio,
        min_silence_s,
    )


def robust_relative_uplift(current: float, history: Sequence[float]) -> float:
    """按当前场次历史计算稳健相对增幅。"""
    if _cython_backend is None:
        return _fallback_round2.robust_relative_uplift(current, history)
    values = history if isinstance(history, (list, tuple)) else _float64_contiguous(history)
    return _cython_backend.robust_relative_uplift(current, values)


def danmaku_text_features(
    texts: Sequence[str],
    high_emotion_tokens: Sequence[str],
) -> tuple[float, float, float, tuple[str, ...]]:
    """汇总弹幕复读率、情绪强度和代表消息。"""
    if not texts:
        return 0.0, 0.0, 0.0, ()
    if _rust_backend is None:
        return _fallback_round2.danmaku_text_features(texts, high_emotion_tokens)
    try:
        repetition, intensity, high_emotion, representatives = _rust_backend.danmaku_text_features(
            list(texts) if isinstance(texts, str) else texts,
            list(high_emotion_tokens) if isinstance(high_emotion_tokens, str) else high_emotion_tokens,
        )
    except UnicodeEncodeError:
        # Python 字符串可含孤立代理码点；Rust UTF-8 无法表示，保留参考实现的文本语义。
        return _fallback_round2.danmaku_text_features(texts, high_emotion_tokens)
    return repetition, intensity, high_emotion, tuple(representatives)


def danmaku_baseline_rate(ts_seconds: list[float], bucket_s: float = 10.0) -> tuple[float, int]:
    """按固定秒数分桶并返回弹幕速率中位数和样本总数。"""
    if len(ts_seconds) < 10:
        return 0.0, 0
    if _cython_backend is None:
        return _fallback_round2.danmaku_baseline_rate(ts_seconds, bucket_s)
    return _cython_backend.danmaku_baseline_rate(ts_seconds, bucket_s)


def group_srt_blocks(
    words: list[tuple[float, float, str]],
    max_chars: int = 14,
    min_display_ms: int = 800,
    max_display_ms: int = 5000,
    line_gap_ms: int = 200,
    *,
    duration_s: float | None = None,
) -> str:
    """把词级条目聚合为 SRT 字幕块。"""
    if _cython_backend is None:
        return _fallback_round2.group_srt_blocks(
            words,
            max_chars,
            min_display_ms,
            max_display_ms,
            line_gap_ms,
            duration_s=duration_s,
        )
    return str(
        _cython_backend.group_srt_blocks(
            words,
            max_chars,
            min_display_ms,
            max_display_ms,
            line_gap_ms,
            duration_s=duration_s,
        )
    )


def _cluster_inputs(items: list[ClusterItem]) -> tuple[list[str], list[list[str]], list[int | None]] | None:
    texts: list[str] = []
    keywords: list[list[str]] = []
    dates: list[_dt | None] = []
    for item in items:
        texts.append(item.get("asr_text", "") or "")
        keywords.append([str(keyword) for keyword in (item.get("keywords", []) or [])])
        timestamp = item.get("start_ts")
        if isinstance(timestamp, str):
            try:
                timestamp = _dt.fromisoformat(timestamp)
            except ValueError:
                timestamp = None
        dates.append(timestamp if isinstance(timestamp, _dt) else None)
    anchor = next((value for value in dates if value is not None), None)
    timestamps: list[int | None] = []
    for value in dates:
        if value is None:
            timestamps.append(None)
            continue
        assert anchor is not None
        if value.tzinfo is not anchor.tzinfo:
            # 不同动态时区的 datetime 减法可能不满足可加性，保留逐对的 Python 时间语义。
            return None
        delta = value - anchor
        timestamps.append((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    return texts, keywords, timestamps


def cluster_similarity_matrix(items: list[ClusterItem]) -> list[list[float]]:
    """计算 N×N 相似度矩阵并选择当前最快可用后端。"""
    if not items:
        return []
    if len(items) == 1:
        return [[0.0]]
    if _rust_backend is not None:
        inputs = _cluster_inputs(items)
        if inputs is None:
            return _fallback_round2.cluster_similarity_matrix(items)
        try:
            return _rust_backend.cluster_similarity_matrix(*inputs)
        except UnicodeEncodeError:
            return _fallback_round2.cluster_similarity_matrix(items)

    if _cython_backend is not None:
        return _cython_backend.cluster_similarity_matrix(items)
    return _fallback_round2.cluster_similarity_matrix(items)


def cluster_candidate_groups(items: list[ClusterItem], threshold: float) -> list[tuple[int, list[int], float]]:
    """在一次 CPU 调用中计算并消费矩阵，保留根编号、顺序和簇内均值。"""
    if len(items) < 2:
        if not isfinite(threshold):
            raise ValueError("threshold must be finite")
        return []
    if _rust_backend is not None:
        inputs = _cluster_inputs(items)
        if inputs is not None:
            try:
                return _rust_backend.cluster_candidate_groups(*inputs, threshold)
            except UnicodeEncodeError:
                # 孤立代理码点无法传入 Rust，使用同语义参考实现。
                matrix = _fallback_round2.cluster_similarity_matrix(items)
                return _fallback_round2.cluster_groups(matrix, threshold)
    return cluster_groups(cluster_similarity_matrix(items), threshold)


def cluster_groups(matrix: list[list[float]], threshold: float) -> list[tuple[int, list[int], float]]:
    """以独立 CPU 内核执行并查集和簇内统计，保持原根编号与输出顺序。"""
    if _rust_backend is None:
        return _fallback_round2.cluster_groups(matrix, threshold)
    return _rust_backend.cluster_groups(matrix, threshold)


def novelty_features(text: str) -> tuple[set[str], set[str]]:
    """抽取滚动检测可复用的词项和实体，不保留全局文本缓存。"""
    if _cython_backend is None:
        return _fallback_round2.novelty_features(text)
    return _cython_backend.novelty_features(text)


def novelty_from_features(
    left: set[str], right: set[str], entities: set[str], baseline_entities: set[str]
) -> tuple[float, float, float]:
    """计算已提取集合之间的新颖度。"""
    if _cython_backend is None:
        return _fallback_round2.novelty_from_features(left, right, entities, baseline_entities)
    return _cython_backend.novelty_from_features(left, right, entities, baseline_entities)


def text_novelty(current: str, baseline: str, *, include_entities: bool = True) -> tuple[float, float, float | None]:
    """提取文本词项并计算新颖度；证据生成可明确跳过不需要的实体统计。"""
    if _cython_backend is None:
        return _fallback_round2.text_novelty(current, baseline, include_entities=include_entities)
    return _cython_backend.text_novelty(current, baseline, include_entities=include_entities)


def get_backend() -> str:
    """返回文本匹配后端：``C`` 或 ``python``。"""
    return _BACKEND


def get_cython_backend() -> str:
    """返回数值与字幕热点后端：``Cython`` 或 ``python``。"""
    return _CYTHON_BACKEND


def get_repeat_backend() -> str:
    """返回精确重复检测后端：``C++`` 或 ``python``。"""
    return "C++" if _repeat_backend is not None else "python"


def get_rust_backend() -> str:
    """返回并行聚类与弹幕文本后端：``Rust+rayon`` 或 ``python``。"""
    return _RUST_BACKEND


def get_cluster_backend() -> str:
    """返回聚类矩阵实际后端。"""
    if _rust_backend is not None:
        return "Rust+rayon"
    if _cython_backend is not None:
        return "Cython"
    return "python"


__all__ = [
    "interval_conflicts",
    "danmaku_sentiment_statistics",
    "representative_danmaku_statistics",
    "novelty_features",
    "novelty_from_features",
    "audio_bucket_indices",
    "normalize_asr_text",
    "normalize_asr_with_indexes",
    "cluster_candidate_groups",
    "levenshtein_distance",
    "cluster_groups",
    "text_novelty",
    "pcm16_statistics",
    "consecutive_repeat_details",
    "review_text_statistics",
    "audio_peak_offsets",
    "cluster_similarity_matrix",
    "danmaku_baseline_rate",
    "danmaku_text_features",
    "fast_aho_has_match",
    "fast_ahocorasick_build",
    "fast_ahocorasick_search",
    "fast_char_bigrams",
    "fast_cosine_similarity",
    "fast_match_keywords",
    "fast_meme_count",
    "find_silence_ranges",
    "get_backend",
    "get_cluster_backend",
    "get_cython_backend",
    "get_rust_backend",
    "get_repeat_backend",
    "group_srt_blocks",
    "robust_relative_uplift",
]
