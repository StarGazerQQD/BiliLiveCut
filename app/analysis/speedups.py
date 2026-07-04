"""BiliLiveCut 加速模块分派层 (V0.1.9).

优先加载 C 编译扩展 (_c_speedups),若不可用则回退到纯 Python 实现 (_speedups_py)。
业务代码统一从此模块导入。
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

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
    _logger.info("加速模块: C 扩展已加载 (app.analysis._c_speedups)")
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
    _logger.info("加速模块: 使用纯 Python 后备 (_speedups_py)")


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
    "get_backend",
]
