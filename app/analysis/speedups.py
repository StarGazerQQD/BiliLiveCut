"""BiliLiveCut 加速模块分派层 (V0.1.10.1).

优先加载编译扩展,若不可用则回退到纯 Python 实现。
业务代码统一从此模块导入。

加速链 (优先级从高到低):
  第一轮 (V0.1.9) — Aho-Corasick + 余弦相似度 + bigram
    1. C 扩展 (_c_speedups) → 2. 纯 Python (_speedups_py)

  第二轮 (V0.1.10) — 聚类矩阵 + 弹幕基线 + SRT 组装
    cluster_similarity_matrix: Rust → Cython → Python 三级回退
    danmaku_baseline_rate / group_srt_blocks: Cython → Python 两级回退
"""

from __future__ import annotations

import logging
from datetime import datetime as _dt

_logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# 第一轮: Aho-Corasick + 文本相似度 (V0.1.9)
# ════════════════════════════════════════════════════════════════════

try:
    from app.analysis._c_speedups import (  # type: ignore[import]
        fast_aho_has_match,
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_match_keywords,
        fast_meme_count,
        fast_multi_emotion,
        fast_sliding_max,
        fast_count_bursts,
    )
    _BACKEND = "C"
    _logger.info("加速模块(C): 已加载 app.analysis._c_speedups")
except ImportError:
    from app.analysis._speedups_py import (  # type: ignore[no-redef]
        fast_aho_has_match,
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_match_keywords,
        fast_meme_count,
        fast_multi_emotion,
        fast_sliding_max,
        fast_count_bursts,
    )
    _BACKEND = "python"
    _logger.info("加速模块(Python): 使用 _speedups_py")

# ════════════════════════════════════════════════════════════════════
# 第二轮: 聚类矩阵 + 弹幕基线 + SRT 组装 (V0.1.10)
# ════════════════════════════════════════════════════════════════════

# -- danmaku_baseline_rate / group_srt_blocks: Cython → Python --
_CLUSTER_BACKEND = "python"

try:
    from app.analysis._speedups_round2 import (  # type: ignore[import]
        danmaku_baseline_rate,
        group_srt_blocks,
    )
    _logger.info("加速模块(第二轮): Cython 扩展已加载")
except ImportError:
    from app.analysis._speedups_round2_py import (  # type: ignore[no-redef]
        danmaku_baseline_rate,
        group_srt_blocks,
    )
    _logger.info("加速模块(第二轮): 使用 _speedups_round2_py")

# -- cluster_similarity_matrix: Rust → Cython → Python 三级回退 --
_cluster_sim_func = None
_cluster_sim_raw = False  # True = needs raw args (Rust), False = takes dicts (Cython/Python)

try:
    from app.analysis._rust_cluster import cluster_similarity_matrix_rust  # type: ignore[import]
    _cluster_sim_func = cluster_similarity_matrix_rust
    _cluster_sim_raw = True
    _CLUSTER_BACKEND = "Rust+rayon"
    _logger.info("加速模块(cluster): Rust+rayon 已加载")
except ImportError:
    pass

if _cluster_sim_func is None:
    try:
        from app.analysis._speedups_round2 import (
            cluster_similarity_matrix as _csim,  # type: ignore[import]
        )
        _cluster_sim_func = _csim
        _cluster_sim_raw = False
        _CLUSTER_BACKEND = "Cython"
        _logger.info("加速模块(cluster): Cython 已加载")
    except ImportError:
        from app.analysis._speedups_round2_py import (
            cluster_similarity_matrix as _csim,  # type: ignore[no-redef]
        )
        _cluster_sim_func = _csim
        _cluster_sim_raw = False
        _CLUSTER_BACKEND = "python"
        _logger.info("加速模块(cluster): 使用 _speedups_round2_py")


def cluster_similarity_matrix(items: list[dict]) -> list[list[float]]:
    """计算 N×N 相似度矩阵 — 自动选择最快后端。

    :param items: list[dict], 每个 dict 含 asr_text/keywords/start_ts。
    :returns: list[list[float]], N×N 对称矩阵。
    """
    if _cluster_sim_raw:
        # Rust 后端: 一次提取字段后传入 Rust
        n = len(items)
        if n < 2:
            return [[0.0] * n for _ in range(n)]

        texts: list[str] = []
        keywords: list[list[str]] = []
        timestamps: list[float | None] = []

        for item in items:
            texts.append(item.get("asr_text", "") or "")
            kw = item.get("keywords", []) or []
            keywords.append([str(k) for k in kw])
            ts = item.get("start_ts")
            if ts is None:
                timestamps.append(None)
            elif isinstance(ts, _dt):
                timestamps.append(ts.timestamp())
            elif isinstance(ts, str):
                try:
                    timestamps.append(_dt.fromisoformat(ts).timestamp())
                except ValueError:
                    timestamps.append(None)
            else:
                timestamps.append(None)

        return _cluster_sim_func(texts, keywords, timestamps)  # type: ignore[operator]

    # Cython / Python 后端: 直接传 dicts
    return _cluster_sim_func(items)  # type: ignore[operator]


def get_backend() -> str:
    """返回当前加速后端: 'C' 或 'python'."""
    return _BACKEND


def get_cluster_backend() -> str:
    """返回聚类矩阵加速后端: 'Rust+rayon' / 'Cython' / 'python'."""
    return _CLUSTER_BACKEND


__all__ = [
    "fast_ahocorasick_build",
    "fast_ahocorasick_search",
    "fast_aho_has_match",
    "fast_char_bigrams",
    "fast_cosine_similarity",
    "fast_match_keywords",
    "fast_meme_count",
    "fast_multi_emotion",
    "fast_sliding_max",
    "fast_count_bursts",
    "cluster_similarity_matrix",
    "danmaku_baseline_rate",
    "group_srt_blocks",
    "get_backend",
    "get_cluster_backend",
]
