"""BiliLiveCut 加速模块分派层 (兼容门面).

V0.1.14: 实现已迁移到 app.accelerators.dispatcher。
本模块作为兼容门面保留，所有公开导出保持不变。
"""

from app.accelerators.dispatcher import (  # noqa: F401
    cluster_similarity_matrix,
    danmaku_baseline_rate,
    fast_aho_has_match,
    fast_ahocorasick_build,
    fast_ahocorasick_search,
    fast_char_bigrams,
    fast_cosine_similarity,
    fast_match_keywords,
    fast_meme_count,
    get_backend,
    get_cluster_backend,
    group_srt_blocks,
)

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
    "get_cluster_backend",
]
