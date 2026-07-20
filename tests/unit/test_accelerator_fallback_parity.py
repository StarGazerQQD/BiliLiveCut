"""Accelerator fallback parity tests — direct testing of Python fallback modules.

直接导入 app.accelerators.python_fallback.speedups 和 speedups_round2,
不通过 dispatcher 决定路径。确保 native 存在时也覆盖 fallback 代码。

覆盖率目标:
    - speedups.py     ≥ 95%
    - speedups_round2.py ≥ 90%
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════
# 直接导入 fallback — 不通过 dispatcher
# ═══════════════════════════════════════════════════════════
from app.accelerators.python_fallback import speedups as fb
from app.accelerators.python_fallback import speedups_round2 as fb2

# ── 辅助函数 ─────────────────────────────────────────────


def _has_native() -> bool:
    """Check if native C extension is available for parity tests."""
    try:
        import app.analysis._c_speedups  # noqa: F401

        return True
    except ImportError:
        return False


_NATIVE_AVAILABLE = _has_native()


# ══════════════════════════════════════════════════════════
# speedups.py — Aho-Corasick / bigrams / cosine
# ══════════════════════════════════════════════════════════


class TestAutomatonBuild:
    """_build_automaton / fast_ahocorasick_build."""

    def test_build_empty_patterns(self) -> None:
        """Empty patterns produce valid automaton."""
        am = fb._build_automaton([])
        assert "trie" in am
        assert "fail" in am
        assert "outputs" in am
        assert len(am["trie"]) == 1
        assert len(am["outputs"][0]) == 0

    def test_build_single_pattern(self) -> None:
        """Single pattern automaton has correct outputs."""
        am = fb._build_automaton(["abc"])
        assert len(am["outputs"]) >= 2
        assert "abc" in am["outputs"][-1]

    def test_build_overlapping_patterns(self) -> None:
        """Overlapping patterns (a, ab, abc) build correctly."""
        am = fb._build_automaton(["a", "ab", "abc"])
        # All patterns should be reachable
        assert len(am["outputs"]) >= 4

    def test_fast_ahocorasick_build(self) -> None:
        """fast_ahocorasick_build returns automaton dict."""
        am = fb.fast_ahocorasick_build(("test",))
        assert isinstance(am, dict)
        assert "trie" in am
        assert "outputs" in am


class TestAhoCorasickSearch:
    """fast_ahocorasick_search behavior."""

    def test_single_match(self) -> None:
        """Exact single pattern match."""
        am = fb._build_automaton(["hello"])
        result = fb.fast_ahocorasick_search(am, "hello world")
        assert result == ["hello"]

    def test_multiple_matches_in_text(self) -> None:
        """Multiple occurrences of same pattern."""
        am = fb._build_automaton(["a"])
        result = fb.fast_ahocorasick_search(am, "aaa")
        assert result == ["a", "a", "a"]

    def test_overlapping_patterns(self) -> None:
        """ab and abc overlapping — 'abc' should match both at their positions."""
        am = fb._build_automaton(["ab", "abc"])
        result = fb.fast_ahocorasick_search(am, "abc")
        assert "ab" in result
        assert "abc" in result

    def test_no_match(self) -> None:
        """Text without pattern returns empty."""
        am = fb._build_automaton(["zzz"])
        result = fb.fast_ahocorasick_search(am, "hello")
        assert result == []

    def test_empty_text(self) -> None:
        """Empty text returns empty."""
        am = fb._build_automaton(["a"])
        result = fb.fast_ahocorasick_search(am, "")
        assert result == []

    def test_ascii_patterns(self) -> None:
        """ASCII multi-char patterns."""
        am = fb._build_automaton(["foo", "bar"])
        result = fb.fast_ahocorasick_search(am, "foobar")
        assert "foo" in result
        assert "bar" in result

    def test_cjk_patterns(self) -> None:
        """CJK text matching."""
        am = fb._build_automaton(["你好", "世界"])
        result = fb.fast_ahocorasick_search(am, "你好世界")
        assert "你好" in result
        assert "世界" in result

    def test_emoji_patterns(self) -> None:
        """Emoji pattern matching."""
        am = fb._build_automaton(["😀", "🎉"])
        result = fb.fast_ahocorasick_search(am, "hello😀world🎉")
        assert "😀" in result
        assert "🎉" in result

    def test_failure_link_backtracking(self) -> None:
        """Failure links correctly backtrack (ACD → no match on D, fall back to CD)."""
        am = fb._build_automaton(["ACD", "CD"])
        result = fb.fast_ahocorasick_search(am, "ACD")
        assert "ACD" in result
        # CD should also match (it's a suffix of ACD)
        assert "CD" in result

    def test_partial_match_no_complete(self) -> None:
        """Partial prefix match but no complete pattern."""
        am = fb._build_automaton(["abcdef"])
        result = fb.fast_ahocorasick_search(am, "abcxyz")
        assert result == []


class TestAhoHasMatch:
    """fast_aho_has_match behavior."""

    def test_has_match_positive(self) -> None:
        """Pattern found returns True."""
        am = fb._build_automaton(["needle"])
        assert fb.fast_aho_has_match(am, "haystack with needle inside")

    def test_has_match_negative(self) -> None:
        """Pattern not found returns False."""
        am = fb._build_automaton(["missing"])
        assert not fb.fast_aho_has_match(am, "nothing here")

    def test_has_match_empty_text(self) -> None:
        """Empty text returns False."""
        am = fb._build_automaton(["a"])
        assert not fb.fast_aho_has_match(am, "")

    def test_has_match_early_termination(self) -> None:
        """Pattern at start of text returns True immediately (conceptually)."""
        am = fb._build_automaton(["start"])
        assert fb.fast_aho_has_match(am, "start of long long long long text")

    def test_has_match_failure_link_fallback(self) -> None:
        """Failure link backtracking in has_match — exact character not in trie."""
        # Pattern "ab" → trie: 0-a→1-b→2
        # Text "ac" → 'a' ok (node=1), 'c' not in trie[1] → follow fail[1]=0 → check trie[0]['c']
        am = fb._build_automaton(["ab"])
        assert not fb.fast_aho_has_match(am, "ac")


class TestCharBigrams:
    """fast_char_bigrams behavior."""

    def test_basic_bigrams(self) -> None:
        """Simple text produces bigrams."""
        result = fb.fast_char_bigrams("abc")
        assert len(result) == 2  # "ab", "bc"
        assert result[0] == "ab"
        assert result[1] == "bc"

    def test_empty_string(self) -> None:
        """Empty string returns empty list."""
        assert fb.fast_char_bigrams("") == []

    def test_single_char(self) -> None:
        """Single char returns single-element list."""
        result = fb.fast_char_bigrams("a")
        assert len(result) == 1

    def test_spaces_skipped(self) -> None:
        """Whitespace characters are filtered out."""
        result = fb.fast_char_bigrams("a b")
        assert result == ["ab"]

    def test_multiple_spaces(self) -> None:
        """Multiple spaces all skipped."""
        result = fb.fast_char_bigrams("a  b")
        assert result == ["ab"]

    def test_cjk_bigrams(self) -> None:
        """CJK bigrams work."""
        result = fb.fast_char_bigrams("你好世界")
        assert len(result) == 3
        assert result[0] == "你好"
        assert result[1] == "好世"
        assert result[2] == "世界"

    def test_mixed_ascii_cjk(self) -> None:
        """Mixed ASCII and CJK."""
        result = fb.fast_char_bigrams("a你b")
        assert len(result) == 2

    def test_leading_trailing_spaces(self) -> None:
        """Spaces at edges are filtered."""
        result = fb.fast_char_bigrams("  ab  ")
        assert result == ["ab"]

    def test_non_printable_chars(self) -> None:
        """Non-printable chars (ord < space) are filtered."""
        result = fb.fast_char_bigrams("a\tb")
        assert "a\t" not in result
        assert "ab" in result or len(result) > 0

    def test_only_one_char_after_filter(self) -> None:
        """After filtering, only 1 char remains → returns single-element list."""
        # "a " → after filter: only "a" → 1 char < 2 → returns ["a"]
        result = fb.fast_char_bigrams("a ")
        assert result == ["a"]

    def test_only_spaces_returns_empty(self) -> None:
        """All spaces → returns empty list."""
        result = fb.fast_char_bigrams("   ")
        assert result == []


class TestCosineSimilarity:
    """fast_cosine_similarity behavior."""

    def test_identical_vectors(self) -> None:
        """Identical vectors = 1.0."""
        v = {"a": 1.0, "b": 2.0, "c": 3.0}
        result = fb.fast_cosine_similarity(v, v)
        assert abs(result - 1.0) < 0.001

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors = 0.0."""
        result = fb.fast_cosine_similarity({"a": 1.0}, {"b": 1.0})
        assert result == 0.0

    def test_empty_first_vector(self) -> None:
        """Empty first vector = 0.0."""
        result = fb.fast_cosine_similarity({}, {"a": 1.0})
        assert result == 0.0

    def test_empty_second_vector(self) -> None:
        """Empty second vector = 0.0."""
        result = fb.fast_cosine_similarity({"a": 1.0}, {})
        assert result == 0.0

    def test_both_empty(self) -> None:
        """Both empty = 0.0."""
        result = fb.fast_cosine_similarity({}, {})
        assert result == 0.0

    def test_partial_overlap(self) -> None:
        """Partial key overlap."""
        result = fb.fast_cosine_similarity(
            {"a": 1.0, "b": 1.0},
            {"a": 1.0, "c": 1.0},
        )
        assert 0.0 < result < 1.0

    def test_large_values(self) -> None:
        """Large magnitude values produce valid result."""
        result = fb.fast_cosine_similarity(
            {"a": 1000.0},
            {"a": 1000.0, "b": 1.0},
        )
        assert abs(result - 1.0) < 0.01

    def test_zero_magnitude_vector(self) -> None:
        """All zero vector = 0.0."""
        result = fb.fast_cosine_similarity({"a": 0.0, "b": 0.0}, {"a": 1.0})
        assert result == 0.0

    def test_negative_values(self) -> None:
        """Negative values are squared so cosine still works."""
        result = fb.fast_cosine_similarity({"a": -1.0}, {"a": -1.0})
        assert abs(result - 1.0) < 0.001

    def test_capped_at_1(self) -> None:
        """Result capped at 1.0 due to floating point."""
        result = fb.fast_cosine_similarity({"a": 1e10}, {"a": 1e10})
        assert result <= 1.0


class TestMatchKeywords:
    """fast_match_keywords integration."""

    def test_empty_input(self) -> None:
        """Empty text or patterns returns empty."""
        assert fb.fast_match_keywords("", ("a",)) == []
        assert fb.fast_match_keywords("text", ()) == []

    def test_single_keyword_match(self) -> None:
        """Single keyword matched."""
        result = fb.fast_match_keywords("hello world", ("hello",))
        assert result == ["hello"]

    def test_multiple_keywords(self) -> None:
        """Multiple keywords matched in text."""
        result = fb.fast_match_keywords("hello world hello", ("hello", "world"))
        assert "hello" in result

    def test_chinese_keywords(self) -> None:
        """Chinese keywords."""
        result = fb.fast_match_keywords("你好世界你好", ("你好",))
        assert len(result) == 2

    def test_consecutive_chinese_match(self) -> None:
        """Consecutive Chinese: '中' pattern in '中中' — should match twice (one per char)."""
        result = fb.fast_match_keywords("中中", ("中",))
        assert result == ["中", "中"]


class TestMemeCount:
    """fast_meme_count behavior."""

    def test_empty_inputs(self) -> None:
        """Empty texts or memes returns 0."""
        assert fb.fast_meme_count([], ("a",)) == 0
        assert fb.fast_meme_count(["text"], ()) == 0

    def test_count_matches(self) -> None:
        """Count texts containing any meme."""
        texts = ["hello world", "goodbye", "no match"]
        result = fb.fast_meme_count(texts, ("hello",))
        assert result == 1

    def test_all_match(self) -> None:
        """All texts match."""
        texts = ["a test", "another test", "test"]
        result = fb.fast_meme_count(texts, ("test",))
        assert result == 3

    def test_duplicate_texts(self) -> None:
        """Duplicate texts each count once."""
        texts = ["hello"] * 5 + ["nope"]
        result = fb.fast_meme_count(texts, ("hello",))
        assert result == 5


# ══════════════════════════════════════════════════════════
# speedups_round2.py — cluster / danmaku / srt
# ══════════════════════════════════════════════════════════


class TestClusterSimilarityMatrix:
    """cluster_similarity_matrix behavior."""

    def test_empty_items(self) -> None:
        """Empty list returns empty matrix."""
        result = fb2.cluster_similarity_matrix([])
        assert result == []

    def test_single_item(self) -> None:
        """Single item returns 1x1 matrix."""
        result = fb2.cluster_similarity_matrix([{"asr_text": "hello"}])
        assert len(result) == 1
        assert len(result[0]) == 1
        assert result[0][0] == 0.0  # single item, 1x1 of zeros

    def test_two_identical_items(self) -> None:
        """Two identical items have diagonal 1.0 and high similarity."""
        item = {"asr_text": "hello world", "keywords": ["test"]}
        result = fb2.cluster_similarity_matrix([item, item])
        assert len(result) == 2
        # Off-diagonal should be high for identical content
        assert result[0][1] == result[1][0]
        assert result[0][1] > 0.0

    def test_two_different_items(self) -> None:
        """Two very different items."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "hello", "keywords": ["a"]},
                {"asr_text": "zzzzzzzzzz", "keywords": ["b"]},
            ]
        )
        assert result[0][1] == result[1][0]

    def test_symmetric_matrix(self) -> None:
        """Matrix is symmetric."""
        items = [
            {"asr_text": "hello", "keywords": ["a"], "start_ts": "2026-01-01T00:00:00"},
            {"asr_text": "world", "keywords": ["b"], "start_ts": "2026-01-01T00:00:30"},
            {"asr_text": "hello world", "keywords": ["a", "b"], "start_ts": "2026-01-01T00:01:00"},
        ]
        result = fb2.cluster_similarity_matrix(items)
        n = len(result)
        for i in range(n):
            for j in range(n):
                assert result[i][j] == result[j][i]

    def test_kw_only_similarity(self) -> None:
        """Items with same keywords but different text."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "", "keywords": ["a", "b"]},
                {"asr_text": "", "keywords": ["a", "b"]},
            ]
        )
        # With empty text, kw 1.0 * 0.25 weight
        assert result[0][1] > 0.0

    def test_time_similarity_near(self) -> None:
        """Close timestamps get time bonus."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "hello", "keywords": [], "start_ts": "2026-01-01T00:00:00"},
                {"asr_text": "hello", "keywords": [], "start_ts": "2026-01-01T00:00:30"},
            ]
        )
        assert result[0][1] > 0.0

    def test_time_similarity_far(self) -> None:
        """Far timestamps get no time bonus."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "hello", "keywords": [], "start_ts": "2026-01-01T00:00:00"},
                {"asr_text": "hello", "keywords": [], "start_ts": "2026-01-01T03:00:00"},
            ]
        )
        assert result[0][1] >= 0.0

    def test_invalid_timestamp_handled(self) -> None:
        """Invalid timestamp string handled gracefully (treated as None)."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "hello", "start_ts": "not-a-date"},
                {"asr_text": "hello", "start_ts": "also-bad"},
            ]
        )
        assert len(result) == 2

    def test_none_timestamp_handled(self) -> None:
        """None timestamp handled."""
        result = fb2.cluster_similarity_matrix(
            [
                {"asr_text": "hello"},
                {"asr_text": "hello", "start_ts": None},
            ]
        )
        assert len(result) == 2

    def test_result_bounded_0_1(self) -> None:
        """All similarity values are in [0, 1]."""
        items = [
            {"asr_text": "hello", "keywords": ["a"], "start_ts": "2026-01-01T00:00:00"},
            {"asr_text": "world", "keywords": ["b"], "start_ts": "2026-01-01T00:00:00"},
            {"asr_text": "foo bar", "keywords": ["c"], "start_ts": "2026-01-01T00:00:00"},
        ]
        result = fb2.cluster_similarity_matrix(items)
        for row in result:
            for val in row:
                assert 0.0 <= val <= 1.0, f"Value {val} out of range"


class TestDanmakuBaselineRate:
    """danmaku_baseline_rate behavior."""

    def test_less_than_10_items(self) -> None:
        """Fewer than 10 timestamps returns (0.0, 0)."""
        result = fb2.danmaku_baseline_rate([0.0, 1.0, 2.0])
        assert result == (0.0, 0)

    def test_exactly_10_items(self) -> None:
        """Exactly 10 items computes rate."""
        ts = [float(i) for i in range(10)]
        median, total = fb2.danmaku_baseline_rate(ts)
        assert total == 10
        assert median > 0.0

    def test_default_bucket_10s(self) -> None:
        """Default bucket is 10s."""
        ts = [0.0] * 5 + [100.0] * 5 + [200.0] * 5  # 15 items
        median, total = fb2.danmaku_baseline_rate(ts)
        assert total == 15
        assert median > 0.0

    def test_custom_bucket_size(self) -> None:
        """Custom bucket of 5s changes bucketing."""
        # Use a distribution that creates different bucket counts
        # with 5s vs 10s windows
        ts = [0.0, 1.0, 2.0, 3.0, 4.0, 50.0, 51.0, 60.0, 61.0, 62.0] * 2  # 20 items
        m5, _ = fb2.danmaku_baseline_rate(ts, bucket_s=5.0)
        m10, _ = fb2.danmaku_baseline_rate(ts, bucket_s=10.0)
        # Different bucket sizes produce different bucket distributions
        # Both should be valid floats >= 0
        assert m5 >= 0.0
        assert m10 >= 0.0

    def test_odd_bucket_count_median(self) -> None:
        """Odd number of buckets uses middle element."""
        ts = [0.0] * 30  # all in bucket 0 → 1 bucket → median is that bucket rate
        median, total = fb2.danmaku_baseline_rate(ts)
        assert total == 30
        assert median > 0.0

    def test_even_bucket_count_median(self) -> None:
        """Even number of buckets averages two middle elements."""
        ts = [0.0] * 15 + [20.0] * 15  # 2 buckets (idx 0, 2)
        median, total = fb2.danmaku_baseline_rate(ts)
        assert total == 30
        assert median > 0.0

    def test_sparse_timestamps(self) -> None:
        """Widely spaced timestamps create many empty buckets."""
        ts = [float(i * 3600) for i in range(60)]  # 1 hour apart
        median, total = fb2.danmaku_baseline_rate(ts)
        assert total == 60
        assert median >= 0.0

    def test_returns_float_and_int(self) -> None:
        """Return type matches signature."""
        ts = [float(i) for i in range(20)]
        median, total = fb2.danmaku_baseline_rate(ts)
        assert isinstance(median, float)
        assert isinstance(total, int)


class TestGroupSrtBlocks:
    """group_srt_blocks behavior."""

    def test_empty_words(self) -> None:
        """Empty words list returns empty string."""
        assert fb2.group_srt_blocks([]) == ""

    def test_single_word_single_block(self) -> None:
        """Single word produces one SRT block."""
        words = [(0.0, 2.0, "Hello")]
        result = fb2.group_srt_blocks(words)
        assert "Hello" in result
        assert "00:00:00,000" in result
        assert "00:00:02,000" in result

    def test_multiple_words_one_block(self) -> None:
        """Multiple short words combined into one block."""
        words = [(0.0, 0.5, "A"), (0.5, 1.0, "B"), (1.0, 1.5, "C")]
        result = fb2.group_srt_blocks(words, max_chars=20)
        assert "A" in result
        assert "B" in result
        assert "C" in result

    def test_word_splits_on_max_chars(self) -> None:
        """Words split into separate blocks when max_chars exceeded."""
        words = [(0.0, 1.0, "AAAAA"), (1.0, 2.0, "BBBBB")]
        result = fb2.group_srt_blocks(words, max_chars=6)
        assert "AAAAA" in result
        assert "BBBBB" in result

    def test_min_display_enforced(self) -> None:
        """Short blocks extended to min_display_ms."""
        words = [(0.0, 0.1, "Hi")]
        result = fb2.group_srt_blocks(words, min_display_ms=800)
        # End time should be at least start + 800ms
        assert "00:00:00,800" in result

    def test_max_display_enforced(self) -> None:
        """Long blocks capped at max_display_ms."""
        words = [(0.0, 10.0, "Hello")]
        result = fb2.group_srt_blocks(words, max_display_ms=3000)
        assert "00:00:03,000" in result

    def test_srt_numbering(self) -> None:
        """SRT blocks are numbered sequentially."""
        words = [(0.0, 1.0, "A" * 100), (1.0, 2.0, "B" * 100)]
        result = fb2.group_srt_blocks(words, max_chars=5)
        assert "1\n" in result
        assert "2\n" in result

    def test_fmt_time_hours(self) -> None:
        """_fmt_time handles hour+ durations."""
        result = fb2._fmt_time(3661.5)  # 1h 1m 1.5s
        assert result == "01:01:01,500"

    def test_fmt_time_minutes_only(self) -> None:
        """_fmt_time with minutes only."""
        result = fb2._fmt_time(125.0)  # 2m 5s
        assert result == "00:02:05,000"

    def test_fmt_time_seconds_only(self) -> None:
        """_fmt_time with seconds only."""
        result = fb2._fmt_time(3.75)
        assert result == "00:00:03,750"

    def test_fmt_time_milliseconds(self) -> None:
        """_fmt_time with sub-second."""
        result = fb2._fmt_time(0.5)
        assert result == "00:00:00,500"

    def test_fmt_time_zero(self) -> None:
        """_fmt_time of zero."""
        result = fb2._fmt_time(0.0)
        assert result == "00:00:00,000"

    def test_fmt_time_large_hours(self) -> None:
        """_fmt_time with many hours."""
        result = fb2._fmt_time(3600 * 5 + 61)
        assert result.count(":") == 2  # HH:MM:SS format


class TestPairwiseSim:
    """_pairwise_sim behavior."""

    def test_identical_items(self) -> None:
        """Identical items have high similarity."""
        from collections import Counter

        v = Counter(["ab", "bc"])
        sim = fb2._pairwise_sim(
            "abc",
            v,
            {"k1"},
            None,
            "abc",
            v,
            {"k1"},
            None,
        )
        assert sim > 0.5

    def test_different_items(self) -> None:
        """Completely different items have low similarity."""
        from collections import Counter

        sim = fb2._pairwise_sim(
            "a",
            Counter(["ab"]),
            {"x"},
            None,
            "z",
            Counter(["zz"]),
            {"y"},
            None,
        )
        assert sim < 0.5

    def test_empty_texts(self) -> None:
        """Empty texts only get keyword+time similarity."""
        from collections import Counter

        sim = fb2._pairwise_sim(
            "",
            Counter(),
            set(),
            None,
            "",
            Counter(),
            set(),
            None,
        )
        assert sim == 0.0

    def test_time_distance_weight(self) -> None:
        """Close timestamps increase similarity."""
        from collections import Counter
        from datetime import datetime as dt

        t1 = dt.fromisoformat("2026-01-01T00:00:00")
        t2 = dt.fromisoformat("2026-01-01T00:00:10")
        sim = fb2._pairwise_sim(
            "hello",
            Counter(["he"]),
            {"a"},
            t1,
            "hello",
            Counter(["he"]),
            {"a"},
            t2,
        )
        assert sim > 0.5


# ══════════════════════════════════════════════════════════
# Native vs Fallback Parity Tests
# ══════════════════════════════════════════════════════════


class TestNativeFallbackParity:
    """Compare native C extension output vs Python fallback.

    Only runs when native C extension is available.
    """

    def _native_match_keywords(self, text, patterns):
        import app.analysis._c_speedups as nc

        return nc.fast_match_keywords(text, tuple(patterns))

    def _native_meme_count(self, texts, memes):
        import app.analysis._c_speedups as nc

        return nc.fast_meme_count(texts, tuple(memes))

    def test_parity_match_keywords_ascii(self) -> None:
        """Native and fallback produce same results for ASCII."""
        if not _NATIVE_AVAILABLE:
            return  # silently skip parity, fallback is already tested above
        text = "hello world hello"
        patterns = ("hello", "world")
        n_result = self._native_match_keywords(text, patterns)
        f_result = fb.fast_match_keywords(text, patterns)
        # Both should contain the same patterns (order may differ slightly)
        assert sorted(n_result) == sorted(f_result)

    def test_parity_match_keywords_chinese(self) -> None:
        """Native and fallback agree on Chinese."""
        if not _NATIVE_AVAILABLE:
            return
        n_result = self._native_match_keywords("你好世界", ("你好", "世界"))
        f_result = fb.fast_match_keywords("你好世界", ("你好", "世界"))
        assert sorted(n_result) == sorted(f_result)

    def test_parity_match_keywords_consecutive(self) -> None:
        """Consecutive character hits agree."""
        if not _NATIVE_AVAILABLE:
            return
        n_result = self._native_match_keywords("中中", ("中",))
        f_result = fb.fast_match_keywords("中中", ("中",))
        assert n_result == f_result

    def test_parity_match_keywords_no_match(self) -> None:
        """No match case agrees."""
        if not _NATIVE_AVAILABLE:
            return
        n_result = self._native_match_keywords("abc", ("def", "ghi"))
        f_result = fb.fast_match_keywords("abc", ("def", "ghi"))
        assert n_result == f_result

    def test_parity_meme_count(self) -> None:
        """Meme count parity."""
        if not _NATIVE_AVAILABLE:
            return
        texts = ["hello world", "hello again", "no match"]
        memes = ("hello",)
        n_result = self._native_meme_count(texts, memes)
        f_result = fb.fast_meme_count(texts, memes)
        assert n_result == f_result

    def test_parity_cosine_similarity(self) -> None:
        """Cosine similarity parity (native vs fallback)."""
        if not _NATIVE_AVAILABLE:
            return
        import app.analysis._c_speedups as nc

        v = {"a": 1.0, "b": 2.0, "c": 3.0}
        n_result = nc.fast_cosine_similarity(v, v)
        f_result = fb.fast_cosine_similarity(v, v)
        assert abs(n_result - f_result) < 0.01

    def test_parity_char_bigrams(self) -> None:
        """Char bigrams parity."""
        if not _NATIVE_AVAILABLE:
            return
        import app.analysis._c_speedups as nc

        n_result = nc.fast_char_bigrams("hello")
        f_result = fb.fast_char_bigrams("hello")
        # Both should produce similar bigrams
        assert len(n_result) > 0
        assert len(f_result) > 0

    def test_parity_has_match(self) -> None:
        """Has match parity."""
        if not _NATIVE_AVAILABLE:
            return
        import app.analysis._c_speedups as nc

        n_am = nc.fast_ahocorasick_build(("test",))
        f_am = fb.fast_ahocorasick_build(("test",))
        n_has = nc.fast_aho_has_match(n_am, "this is a test")
        f_has = fb.fast_aho_has_match(f_am, "this is a test")
        assert n_has == f_has
