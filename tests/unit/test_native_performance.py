from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.accelerators import dispatcher
from app.accelerators.python_fallback import speedups
from app.accelerators.python_fallback import speedups_round2 as reference


@pytest.mark.parametrize(
    "text,patterns",
    [
        ("a" * 10, tuple("a" * size for size in range(1, 11))),
        ("a" * 256, ("a" * 256,)),
        ("中文" * 128, ("中文" * 128, "文")),
        ("test", ("test",) * 32),
        ("test", ("test",) * 128),
        ("a\x00b", ("a\x00b", "\x00")),
    ],
)
def test_keyword_match_preserves_all_suffixes_lengths_and_duplicates(text: str, patterns: tuple[str, ...]) -> None:
    expected = speedups.fast_match_keywords(text, patterns)
    assert dispatcher.fast_match_keywords(text, patterns) == expected
    automaton = dispatcher.fast_ahocorasick_build(patterns)
    assert dispatcher.fast_ahocorasick_search(automaton, text) == expected


def test_keyword_automaton_keeps_patterns_after_caller_changes_list() -> None:
    patterns = ["中文", "hello"]
    automaton = dispatcher.fast_ahocorasick_build(patterns)
    patterns[:] = ["changed"]
    assert dispatcher.fast_ahocorasick_search(automaton, "中文 hello changed") == ["中文", "hello"]


def test_keyword_cache_keeps_room_settings_and_result_lists_independent() -> None:
    text = "游戏 唱歌 游戏"
    first = dispatcher.fast_match_keywords(text, ("游戏",))
    first.clear()
    assert dispatcher.fast_match_keywords(text, ("游戏",)) == ["游戏", "游戏"]
    assert dispatcher.fast_match_keywords(text, ("唱歌",)) == ["唱歌"]
    for index in range(24):
        assert dispatcher.fast_match_keywords(text, (f"房间{index}",)) == []
    assert dispatcher.fast_match_keywords(text, ("游戏",)) == ["游戏", "游戏"]
    assert dispatcher._keyword_automaton.cache_info().currsize <= 8


def _reference_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (ca != cb)))
        previous = row
    return previous[-1]


@pytest.mark.parametrize("size", [0, 1, 2, 16, 64, 129, 257])
def test_exact_edit_distance_preserves_unicode_and_insertion_deletion_costs(size: int) -> None:
    rng = random.Random(25102026 + size)
    alphabet = "中文abc😀𠀀İß\ud800\x00 \n"
    for _ in range(8):
        a = "".join(rng.choices(alphabet, k=size))
        b = "".join(rng.choices(alphabet, k=size // 2 + 1))
        expected = _reference_distance(a, b)
        assert dispatcher.levenshtein_distance(a, b) == expected
        assert dispatcher.levenshtein_distance(b, a) == expected


@pytest.mark.parametrize(
    "review,decision,reason",
    [
        ("aaaaaaaaab", "keep_base", "low_edit_distance(0.10)"),
        ("aaaaaaaabb", "use_review", "accept_review(edit_distance=0.20)"),
        ("aaaaabbbbb", "use_review", "accept_review(edit_distance=0.50)"),
        ("aaaabbbbbb", "manual_review_needed", "high_edit_distance(0.60)"),
    ],
)
def test_asr_review_keeps_exact_thresholds_and_reported_ratio(review: str, decision: str, reason: str) -> None:
    from app.analysis.transcription.backends import _merge_review_text

    text, actual_decision, reasons = _merge_review_text("aaaaaaaaaa", review, 0.0)
    assert text == (review if decision == "use_review" else "aaaaaaaaaa")
    assert actual_decision == decision
    assert reasons == [reason]


@pytest.mark.parametrize("size", [0, 1, 2, 8, 32, 128])
@pytest.mark.parametrize("threshold", [0.0, 0.6, 0.82, 1.0])
def test_fused_clustering_keeps_groups_roots_and_exact_average(size: int, threshold: float) -> None:
    rng = random.Random(2030 + size)
    start = datetime(2026, 10, 25)
    items: list[dispatcher.ClusterItem] = [
        {
            "asr_text": rng.choice(["中文直播", "😀中文123", "A\x00B", " ", "", "中"]),
            "keywords": rng.choice([[], ["中文"], ["ABC", "游戏"], ["中文", "中文"]]),
            "start_ts": start + timedelta(seconds=index * 0.125),
        }
        for index in range(size)
    ]
    expected = reference.cluster_groups(reference.cluster_similarity_matrix(items), threshold)
    assert dispatcher.cluster_candidate_groups(items, threshold) == expected


@pytest.mark.parametrize("text", ["中文", "\ud800abc", "😀𠀀\x00", "\U0010ffff"])
@pytest.mark.parametrize("backend", ["default", "without_rust", "python"])
def test_fused_clustering_preserves_mixed_timezone_and_unicode_behavior(
    text: str, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if backend != "default":
        monkeypatch.setattr(dispatcher, "_rust_backend", None)
    if backend == "python":
        monkeypatch.setattr(dispatcher, "_cython_backend", None)
    items: list[dispatcher.ClusterItem] = [
        {"asr_text": text, "start_ts": datetime(2026, 10, 25, tzinfo=UTC)},
        {"asr_text": text, "start_ts": datetime(2026, 10, 25, 8, tzinfo=timezone(timedelta(hours=8)))},
        {"asr_text": text, "start_ts": "invalid"},
    ]
    assert dispatcher.cluster_candidate_groups(items, 0.4) == reference.cluster_groups(
        reference.cluster_similarity_matrix(items), 0.4
    )


@pytest.mark.parametrize("timestamp", [None, "", "invalid", "2026-02-30", "2026-10-25T08:00:00+08:00"])
def test_cython_clustering_preserves_missing_invalid_and_iso_timestamps(
    timestamp: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "_rust_backend", None)
    items: list[dispatcher.ClusterItem] = [
        {"asr_text": "相同正文", "start_ts": datetime(2026, 10, 25, tzinfo=UTC)},
        {"asr_text": "相同正文", "start_ts": timestamp},
        {"asr_text": "相同正文", "start_ts": "2026-10-25T00:30:00+00:00"},
    ]
    assert dispatcher.cluster_similarity_matrix(items) == reference.cluster_similarity_matrix(items)
    assert dispatcher.cluster_candidate_groups(items, 0.4) == reference.cluster_groups(
        reference.cluster_similarity_matrix(items), 0.4
    )


def test_cython_clustering_preserves_naive_aware_datetime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher, "_rust_backend", None)
    items: list[dispatcher.ClusterItem] = [
        {"asr_text": "相同正文", "start_ts": datetime(2026, 10, 25, tzinfo=UTC)},
        {"asr_text": "相同正文", "start_ts": datetime(2026, 10, 25)},
    ]
    with pytest.raises(TypeError):
        dispatcher.cluster_candidate_groups(items, 0.4)


@pytest.mark.parametrize("threshold", [math.inf, -math.inf, math.nan])
def test_fused_clustering_rejects_nonfinite_threshold(threshold: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        dispatcher.cluster_candidate_groups([], threshold)
