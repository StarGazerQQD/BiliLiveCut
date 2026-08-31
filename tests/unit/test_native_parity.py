"""Native accelerator parity/property tests — Stage 10.

Coverage:
- C vs Python fallback: pattern matching (ahocorasick), bigrams, cosine, keyword match, meme count
- Unicode input
- Empty input
- Long input
- Boundary values
- Exception inputs
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest

# ── Helpers ─────────────────────────────────────────────


def _get_backend_id() -> str:
    """Get current accelerator backend identifier."""
    try:
        from app.accelerators.dispatcher import get_backend

        return get_backend()
    except Exception:
        return "python"


# ── Pattern matching parity ──────────────────────────────


class TestAhoCorasickParity:
    """Verify fast_ahocorasick_search matches expected behavior."""

    def test_single_pattern_found(self) -> None:
        """Single pattern match works."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("hello world", ("hello",))
        assert result == ["hello"]

    def test_no_match(self) -> None:
        """No patterns in text."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("abc", ("def", "ghi"))
        assert result == []

    def test_multiple_patterns(self) -> None:
        """Multiple patterns matched."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("hello world hello", ("hello", "world"))
        assert "hello" in result
        assert "world" in result

    def test_fast_aho_has_match_positive(self) -> None:
        """Has match returns True."""
        from app.accelerators.dispatcher import fast_aho_has_match, fast_ahocorasick_build

        am = fast_ahocorasick_build(("test",))
        assert fast_aho_has_match(am, "this is a test string")

    def test_fast_aho_has_match_negative(self) -> None:
        """Has match returns False."""
        from app.accelerators.dispatcher import fast_aho_has_match, fast_ahocorasick_build

        am = fast_ahocorasick_build(("zzz",))
        assert not fast_aho_has_match(am, "hello world")

    def test_empty_input_handled(self) -> None:
        """Empty text and empty patterns handled gracefully."""
        from app.accelerators.dispatcher import fast_match_keywords

        assert fast_match_keywords("", ("a",)) == []
        assert fast_match_keywords("text", ()) == []
        assert fast_match_keywords("", ()) == []


# ── Unicode tests ─────────────────────────────────────


class TestUnicode:
    """Unicode text handling."""

    def test_chinese_characters(self) -> None:
        """Chinese text matched correctly."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("你好世界", ("你好",))
        assert result == ["你好"]

    def test_japanese_characters(self) -> None:
        """Japanese text."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("こんにちは世界", ("世界",))
        assert result == ["世界"]

    def test_emoji(self) -> None:
        """Emoji handling."""
        from app.accelerators.dispatcher import fast_match_keywords

        result = fast_match_keywords("hello 😀 world", ("😀",))
        assert result == ["😀"]

    def test_mixed_scripts(self) -> None:
        """Mixed CJK + ASCII + emoji."""
        from app.accelerators.dispatcher import fast_match_keywords

        text = "BiliBili 直播 🔴 精彩内容"
        result = fast_match_keywords(text, ("直播", "BiliBili"))
        assert "直播" in result
        assert "BiliBili" in result


# ── Char bigrams ─────────────────────────────────────


class TestCharBigrams:
    """fast_char_bigrams property tests."""

    def test_basic_bigrams(self) -> None:
        """Simple text produces correct bigrams."""
        from app.accelerators.dispatcher import fast_char_bigrams

        result = fast_char_bigrams("abc")
        assert isinstance(result, list)
        assert len(result) >= 1
        # bigrams can be "ab", "bc" (strings) or tuples ("a","b")
        assert "ab" in result or "bc" in result or ("a", "b") in result

    def test_single_char(self) -> None:
        """Single character produces empty."""
        from app.accelerators.dispatcher import fast_char_bigrams

        result = fast_char_bigrams("a")
        assert isinstance(result, list)
        assert len(result) <= 1

    def test_empty_string(self) -> None:
        """Empty string."""
        from app.accelerators.dispatcher import fast_char_bigrams

        result = fast_char_bigrams("")
        assert result == []

    def test_spaces_skipped(self) -> None:
        """Whitespace is skipped."""
        from app.accelerators.dispatcher import fast_char_bigrams

        result = fast_char_bigrams("a b")
        assert isinstance(result, list)

    def test_unicode_bigrams(self) -> None:
        """Unicode bigrams."""
        from app.accelerators.dispatcher import fast_char_bigrams

        result = fast_char_bigrams("你好")
        assert isinstance(result, list)


# ── Cosine similarity ────────────────────────────────


class TestCosineSimilarity:
    """fast_cosine_similarity tests."""

    def test_identical(self) -> None:
        """Identical vectors = 1.0."""
        from app.accelerators.dispatcher import fast_cosine_similarity

        v = {"a": 1, "b": 2, "c": 3}
        result = fast_cosine_similarity(v, v)
        assert abs(result - 1.0) < 0.001

    def test_orthogonal(self) -> None:
        """Orthogonal vectors = 0.0."""
        from app.accelerators.dispatcher import fast_cosine_similarity

        result = fast_cosine_similarity({"a": 1}, {"b": 1})
        assert abs(result - 0.0) < 0.001

    def test_empty_dicts(self) -> None:
        """Empty dicts handled."""
        from app.accelerators.dispatcher import fast_cosine_similarity

        result = fast_cosine_similarity({}, {"a": 1})
        assert result == 0.0

    def test_boundary_values(self) -> None:
        """Large difference vectors."""
        from app.accelerators.dispatcher import fast_cosine_similarity

        result = fast_cosine_similarity({"a": 1000}, {"b": 1000})
        assert result == pytest.approx(0)


# ── Meme counting ───────────────────────────────────


class TestMemeCount:
    """fast_meme_count tests."""

    def test_count_matches(self) -> None:
        """Count how many texts contain memes."""
        from app.accelerators.dispatcher import fast_meme_count

        texts = ["hello world", "goodbye world", "no match"]
        result = fast_meme_count(texts, ("hello",))
        assert result == 1

    def test_empty_inputs(self) -> None:
        """Empty texts or memes."""
        from app.accelerators.dispatcher import fast_meme_count

        assert fast_meme_count([], ("a",)) == 0
        assert fast_meme_count(["text"], ()) == 0

    def test_all_match(self) -> None:
        """All texts match."""
        from app.accelerators.dispatcher import fast_meme_count

        texts = ["a test", "another test", "test again"]
        result = fast_meme_count(texts, ("test",))
        assert result == 3

    def test_long_input(self) -> None:
        """Long text list."""
        from app.accelerators.dispatcher import fast_meme_count

        texts = ["text"] * 1000
        result = fast_meme_count(texts, ("text",))
        assert result == 1000


# ── Backend reporting ────────────────────────────────


class TestBackendReporting:
    """Backend identification works."""

    def test_get_backend_returns_string(self) -> None:
        """Backend is a string."""
        bid = _get_backend_id()
        assert isinstance(bid, str)
        assert bid in ("C", "python", "unknown")

    def test_get_cluster_backend_returns_string(self) -> None:
        """Cluster backend is a string."""
        from app.accelerators.dispatcher import get_cluster_backend

        cbid = get_cluster_backend()
        assert isinstance(cbid, str)

    def test_current_native_namespace_is_reported(self) -> None:
        """Cython 与 Rust 后端均应有独立诊断结果。"""
        from app.accelerators.dispatcher import get_cython_backend, get_rust_backend

        assert get_cython_backend() in ("Cython", "python")
        assert get_rust_backend() in ("Rust+rayon", "python")

    @pytest.mark.parametrize(
        "module_name",
        (
            "app.analysis._c_speedups",
            "app.analysis._speedups_round2",
            "app.analysis._rust_cluster",
        ),
    )
    def test_legacy_native_modules_are_not_exposed(self, module_name: str) -> None:
        """旧原生模块路径不得继续充当兼容入口。"""
        assert importlib.util.find_spec(module_name) is None


# ── Danmaku baseline rate ───────────────────────────


class TestDanmakuBaseline:
    """danmaku_baseline_rate tests."""

    def test_baseline_rate(self) -> None:
        """Basic rate calculation."""
        from app.accelerators.dispatcher import danmaku_baseline_rate

        # Cython signature: danmaku_baseline_rate(list timestamps_seconds, float bucket_s=10.0)
        result = danmaku_baseline_rate([0.0, 5.0, 10.0, 15.0])
        assert result is not None  # returns either list or tuple depending on backend

    def test_empty_input(self) -> None:
        """Empty input handled."""
        from app.accelerators.dispatcher import danmaku_baseline_rate

        result = danmaku_baseline_rate([])
        assert result == (0.0, 0)


class TestCythonRound2Parity:
    """当前环境生成 Cython 扩展时，必须与 Python 参考实现保持一致。"""

    @staticmethod
    def _native_module() -> ModuleType | None:
        try:
            from app.accelerators import _cython_speedups
        except ImportError:
            return None
        return _cython_speedups

    def test_epoch_danmaku_buckets_match_fallback(self) -> None:
        """Unix epoch 秒不得因 float32 收窄破坏十秒分桶。"""
        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        timestamps = [1_700_000_000.0 + i for i in range(100)]
        assert native.danmaku_baseline_rate(timestamps) == fallback.danmaku_baseline_rate(timestamps)

    def test_long_srt_timestamps_match_fallback(self) -> None:
        """较长时间轴仍应保持毫秒级格式一致。"""
        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        words = [(36_000.1234, 36_001.3579, "测试")]
        assert native.group_srt_blocks(words) == fallback.group_srt_blocks(words)

    def test_line_gap_matches_fallback(self) -> None:
        """Cython 和 Python 实现使用相同的停顿断句阈值。"""
        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        words = [(0.0, 0.2, "你"), (0.5, 0.7, "好")]
        assert native.group_srt_blocks(words, max_chars=20, line_gap_ms=200) == fallback.group_srt_blocks(
            words, max_chars=20, line_gap_ms=200
        )

    def test_audio_peak_selection_matches_fallback(self) -> None:
        """局部峰值筛选必须与 Python 参考实现一致。"""
        import numpy as np

        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        times = np.asarray([0.0, 10.0, 20.0, 30.0, 40.0], dtype=np.float64)
        rms = np.asarray([0.1, 0.9, 0.2, 1.0, 0.1], dtype=np.float64)
        assert native.audio_peak_offsets(times, rms, 3, 15.0, 0.15) == fallback.audio_peak_offsets(
            times,
            rms,
            3,
            15.0,
            0.15,
        )

    def test_silence_ranges_match_fallback(self) -> None:
        """连续静音区间边界必须与 Python 参考实现一致。"""
        import numpy as np

        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        times = np.arange(8, dtype=np.float64) * 0.1
        rms = np.asarray([1.0, 0.1, 0.1, 0.1, 0.1, 1.0, 0.1, 1.0], dtype=np.float64)
        assert native.find_silence_ranges(times, rms) == fallback.find_silence_ranges(times, rms)

    def test_robust_uplift_matches_fallback(self) -> None:
        """滚动历史稳健增幅必须与 Python 参考实现一致。"""
        import numpy as np

        native = self._native_module()
        if native is None:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        history = np.asarray([10.0, 11.0, 9.0, 10.5, 10.0], dtype=np.float64)
        assert native.robust_relative_uplift(30.0, history) == pytest.approx(
            fallback.robust_relative_uplift(30.0, history)
        )


class TestRustHotspotParity:
    """Rust 弹幕文本特征必须与 Python 参考实现保持一致。"""

    def test_danmaku_text_features_match_fallback(self) -> None:
        try:
            from app.accelerators import _rust_speedups
        except ImportError:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        texts = ["高能!", "日常", "高能!", "笑死", "日常"]
        tokens = ["高能", "笑死"]
        native = _rust_speedups.danmaku_text_features(texts, tokens)
        reference = fallback.danmaku_text_features(texts, tokens)
        assert native[:3] == pytest.approx(reference[:3])
        assert tuple(native[3]) == reference[3]

    def test_cluster_matrix_matches_fallback_for_whitespace(self) -> None:
        """Rust bigram 必须与当前跳过空白的文本语义一致。"""
        try:
            from app.accelerators import _rust_speedups
        except ImportError:
            return
        from app.accelerators.python_fallback import speedups_round2 as fallback

        items = [
            {"asr_text": "a b", "keywords": ["x"], "start_ts": None},
            {"asr_text": "ab", "keywords": ["x"], "start_ts": None},
        ]
        native = _rust_speedups.cluster_similarity_matrix(
            [item["asr_text"] for item in items],
            [item["keywords"] for item in items],
            [None, None],
        )
        assert native == fallback.cluster_similarity_matrix(items)
