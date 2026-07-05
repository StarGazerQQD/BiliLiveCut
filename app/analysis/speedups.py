"""BiliLiveCut 加速模块分派层 (V0.1.10).

优先加载 C 编译扩展,若不可用则回退到纯 Python 实现。
业务代码统一从此模块导入。

第一轮 (V0.1.9):
  - Aho-Corasick 多模式匹配 (fast_match_keywords, fast_meme_count)
  - 余弦相似度 (fast_cosine_similarity)
  - 字符级 bigram (fast_char_bigrams)

第二轮 (V0.1.10):
  - O(N**2) 聚类相似度矩阵 (cluster_similarity_matrix)
  - 弹幕基线分桶+中位数 (danmaku_baseline_rate)
  - 词条聚合 SRT 字幕 (group_srt_blocks)
"""

from __future__ import annotations

import logging

_logger = logging.getLogger(__name__)

# -- 第一轮: Aho-Corasick + 文本相似度 --
try:
    from app.analysis._c_speedups import (  # type: ignore[import]
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_aho_has_match,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_match_keywords,
        fast_meme_count,
    )
    _BACKEND = "C"
    _logger.info("加速模块(C): 已加载 app.analysis._c_speedups")
except ImportError:
    from app.analysis._speedups_py import (  # type: ignore[no-redef]
        fast_ahocorasick_build,
        fast_ahocorasick_search,
        fast_aho_has_match,
        fast_char_bigrams,
        fast_cosine_similarity,
        fast_match_keywords,
        fast_meme_count,
    )
    _BACKEND = "python"
    _logger.info("加速模块(Python): 使用 _speedups_py")

# -- 第二轮: 聚类矩阵 + 弹幕基线 + SRT --
try:
    from app.analysis._speedups_round2 import (  # type: ignore[import]
        cluster_similarity_matrix,
        danmaku_baseline_rate,
        group_srt_blocks,
    )
    _logger.info("加速模块(第二轮): Cython 扩展已加载")
except ImportError:
    from app.analysis._speedups_round2_py import (  # type: ignore[no-redef]
        cluster_similarity_matrix,
        danmaku_baseline_rate,
        group_srt_blocks,
    )
    _logger.info("加速模块(第二轮): 使用 _speedups_round2_py")


def get_backend() -> str:
    """返回当前加速后端: 'C' 或 'python'."""
    return _BACKEND


__all__ = [
    "fast_ahocorasick_build",
    "fast_ahocorasick_search",
    "fast_aho_has_match",
    "fast_char_bigrams",
    "fast_cosine_similarity",
    "fast_match_keywords",
    "fast_meme_count",
    "cluster_similarity_matrix",
    "danmaku_baseline_rate",
    "group_srt_blocks",
    "get_backend",
]
