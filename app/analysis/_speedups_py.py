"""纯 Python 回退 — Aho-Corasick + 文本相似度 (兼容门面).

V0.1.14: 实现已迁移到 app.accelerators.python_fallback.speedups。
"""

from app.accelerators.python_fallback.speedups import (  # noqa: F401
    fast_aho_has_match,
    fast_ahocorasick_build,
    fast_ahocorasick_search,
    fast_char_bigrams,
    fast_cosine_similarity,
    fast_match_keywords,
    fast_meme_count,
)
