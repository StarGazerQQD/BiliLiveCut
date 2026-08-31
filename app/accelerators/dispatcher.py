"""BiliLiveCut 原生加速统一分派层。

所有编译扩展只存在于 ``app.accelerators`` 命名空间。业务代码只调用本
模块；当前解释器缺少某个编译扩展时，按函数粒度回退到同语义 Python
参考实现，不保留旧 ``app.analysis`` 扩展接口。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime as _dt
from typing import Any

from app.accelerators.python_fallback import speedups_round2 as _fallback_round2

_logger = logging.getLogger(__name__)


# C: Aho-Corasick、字符 bigram、余弦相似度和梗词计数。
try:
    from app.accelerators._c_speedups import (  # type: ignore[import]
        fast_aho_has_match,
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_match_keywords,
        fast_meme_count,
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
        fast_match_keywords,
        fast_meme_count,
    )

    _BACKEND = "python"
    _logger.info("加速模块(C): 使用 Python 参考实现")


# Cython: 音频峰值/静音、稳健滚动基线、弹幕基线和 SRT 组装。
try:
    from app.accelerators import _cython_speedups as _cython_backend  # type: ignore[import]

    _CYTHON_BACKEND = "Cython"
    _logger.info("加速模块(Cython): 已加载 app.accelerators._cython_speedups")
except ImportError:
    _cython_backend = None
    _CYTHON_BACKEND = "python"
    _logger.info("加速模块(Cython): 使用 Python 参考实现")


# Rust: rayon 聚类矩阵与弹幕文本特征。
try:
    from app.accelerators import _rust_speedups as _rust_backend  # type: ignore[import]

    _RUST_BACKEND = "Rust+rayon"
    _logger.info("加速模块(Rust): 已加载 app.accelerators._rust_speedups")
except ImportError:
    _rust_backend = None
    _RUST_BACKEND = "python"
    _logger.info("加速模块(Rust): 使用 Python 参考实现")


def _float64_contiguous(values: Sequence[float]) -> Any:
    """把数值序列转换为 Cython ``double[::1]`` 所需的连续缓冲区。"""
    import numpy as np

    return np.ascontiguousarray(values, dtype=np.float64)


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
    return float(_cython_backend.robust_relative_uplift(current, _float64_contiguous(history)))


def danmaku_text_features(
    texts: Sequence[str],
    high_emotion_tokens: Sequence[str],
) -> tuple[float, float, float, tuple[str, ...]]:
    """汇总弹幕复读率、情绪强度和代表消息。"""
    if _rust_backend is None:
        return _fallback_round2.danmaku_text_features(texts, high_emotion_tokens)
    repetition, intensity, high_emotion, representatives = _rust_backend.danmaku_text_features(
        list(texts),
        list(high_emotion_tokens),
    )
    return float(repetition), float(intensity), float(high_emotion), tuple(representatives)


def danmaku_baseline_rate(ts_seconds: list[float], bucket_s: float = 10.0) -> tuple[float, int]:
    """按固定秒数分桶并返回弹幕速率中位数和样本总数。"""
    if _cython_backend is None:
        return _fallback_round2.danmaku_baseline_rate(ts_seconds, bucket_s)
    rate, count = _cython_backend.danmaku_baseline_rate(ts_seconds, bucket_s)
    return float(rate), int(count)


def group_srt_blocks(
    words: list[tuple[float, float, str]],
    max_chars: int = 14,
    min_display_ms: int = 800,
    max_display_ms: int = 5000,
    line_gap_ms: int = 200,
) -> str:
    """把词级条目聚合为 SRT 字幕块。"""
    if _cython_backend is None:
        return _fallback_round2.group_srt_blocks(
            words,
            max_chars,
            min_display_ms,
            max_display_ms,
            line_gap_ms,
        )
    return str(
        _cython_backend.group_srt_blocks(
            words,
            max_chars,
            min_display_ms,
            max_display_ms,
            line_gap_ms,
        )
    )


def cluster_similarity_matrix(items: list[dict]) -> list[list[float]]:
    """计算 N×N 相似度矩阵并选择当前最快可用后端。"""
    if _rust_backend is not None:
        n = len(items)
        if n < 2:
            return [[0.0] * n for _ in range(n)]

        texts: list[str] = []
        keywords: list[list[str]] = []
        timestamps: list[float | None] = []
        for item in items:
            texts.append(item.get("asr_text", "") or "")
            keywords.append([str(keyword) for keyword in (item.get("keywords", []) or [])])
            timestamp = item.get("start_ts")
            if timestamp is None:
                timestamps.append(None)
            elif isinstance(timestamp, _dt):
                timestamps.append(timestamp.timestamp())
            elif isinstance(timestamp, str):
                try:
                    timestamps.append(_dt.fromisoformat(timestamp).timestamp())
                except ValueError:
                    timestamps.append(None)
            else:
                timestamps.append(None)
        return _rust_backend.cluster_similarity_matrix(texts, keywords, timestamps)

    if _cython_backend is not None:
        return _cython_backend.cluster_similarity_matrix(items)
    return _fallback_round2.cluster_similarity_matrix(items)


def get_backend() -> str:
    """返回文本匹配后端：``C`` 或 ``python``。"""
    return _BACKEND


def get_cython_backend() -> str:
    """返回数值与字幕热点后端：``Cython`` 或 ``python``。"""
    return _CYTHON_BACKEND


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
    "group_srt_blocks",
    "robust_relative_uplift",
]
