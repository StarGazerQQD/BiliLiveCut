"""串行比较完整 CPU 内核调用；合成基准不代表 ASR/CUDA 端到端吞吐。"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import timeit
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.accelerators import dispatcher as native  # noqa: E402
from app.accelerators.python_fallback import speedups_round2 as reference  # noqa: E402


def durations(before: Callable[[], object], after: Callable[[], object], count: int) -> tuple[float, float]:
    """预热后交替测量七轮，减少系统温度和频率变化对前后顺序的影响。"""
    before()
    after()
    samples: tuple[list[float], list[float]] = ([], [])
    functions = (before, after)
    for repeat in range(7):
        for index in (0, 1) if repeat % 2 == 0 else (1, 0):
            samples[index].append(timeit.timeit(functions[index], number=count) / count * 1e6)
    return statistics.median(samples[0]), statistics.median(samples[1])


def previous_docker_matrix(items: list[dict[str, object]]) -> list[list[float]]:
    """重现原 Docker 分派调用边界，不能省略其 Python 包装层后与新入口比较。"""
    if native._cython_backend is not None:
        return native._cython_backend.cluster_similarity_matrix(items)
    return reference.cluster_similarity_matrix(items)


def previous_cluster_groups(matrix: list[list[float]], threshold: float) -> list[tuple[int, list[int], float]]:
    """原业务循环基线；不把新边界校验的开销计入旧实现来夸大收益。"""
    n = len(matrix)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[py] = px

    for i in range(n):
        for j in range(i + 1, n):
            if matrix[i][j] >= threshold:
                union(i, j)
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters.setdefault(root, []).append(i)
    result: list[tuple[int, list[int], float]] = []
    for root, indices in clusters.items():
        if len(indices) < 2:
            continue
        total = 0.0
        count = 0
        for a in indices:
            for b in indices:
                if a < b:
                    total += matrix[a][b]
                    count += 1
        result.append((root, indices, total / count))
    return result


def previous_edit_distance(a: str, b: str) -> int:
    """保留原 ASR 逐字符动态规划作为性能基线。"""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        row = [i]
        for j, cb in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (ca != cb)))
        previous = row
    return previous[-1]


def previous_cluster_pipeline(items: list[native.ClusterItem]) -> list[tuple[int, list[int], float]]:
    """使用相同矩阵实现，仅测量省去 Python 矩阵往返的收益。"""
    return native.cluster_groups(native.cluster_similarity_matrix(items), 0.82)


def benchmark() -> list[dict[str, object]]:
    """要求真实原生扩展，校验结果相等，再逐项测量大小输入。"""
    if (
        native.get_cython_backend() != "Cython"
        or native.get_rust_backend() != "Rust+rayon"
        or native.get_repeat_backend() != "C++"
    ):
        raise RuntimeError("基准要求真实 Cython、C++ 和 Rust 扩展，不能用参考实现冒充")
    rows: list[dict[str, object]] = []

    def measure(name: str, size: int, before: Callable[[], object], after: Callable[[], object], count: int) -> None:
        if before() != after():
            raise AssertionError(f"{name}/{size} 输出不一致")
        baseline_us, native_us = durations(before, after, count)
        rows.append(
            {
                "kernel": name,
                "size": size,
                "baseline_us": baseline_us,
                "native_us": native_us,
                "ratio": native_us / baseline_us,
            }
        )

    for size in [0, 1, 8, 64, 256, 1024]:
        a = ("中文直播测试" * (size // 6 + 1))[:size]
        b = a[:-1] + "新" if a else ""
        measure(
            "exact_edit_distance",
            size,
            partial(previous_edit_distance, a, b),
            partial(native.levenshtein_distance, a, b),
            1000 if size < 10 else (20 if size < 300 else 2),
        )
        b = "".join(chr(0x6000 + index) for index in range(size))
        measure(
            "edit_distance_unrelated",
            size,
            partial(previous_edit_distance, a, b),
            partial(native.levenshtein_distance, a, b),
            1000 if size < 10 else (20 if size < 300 else 2),
        )
    for size in [0, 1, 8, 128, 4096]:
        text = ("Aßİ𠀀! 复读文本" * (size // 10 + 1))[:size]
        for name, before, after in [
            ("asr_normalize", reference.normalize_asr_text, native.normalize_asr_text),
            ("asr_normalize_indexes", reference.normalize_asr_with_indexes, native.normalize_asr_with_indexes),
            ("novelty_features", reference.novelty_features, native.novelty_features),
        ]:
            measure(name, size, partial(before, text), partial(after, text), 100 if size > 128 else 1000)
        words = [(index * 0.37, index * 0.37 + 0.13, "词") for index in range(size)]
        measure(
            "bounded_srt",
            size,
            partial(reference.group_srt_blocks, words, duration_s=max(1.0, size * 0.37)),
            partial(native.group_srt_blocks, words, duration_s=max(1.0, size * 0.37)),
            20 if size > 128 else 500,
        )
        messages = [f"剧情讲解第{index}段! 高能 哈哈" for index in range(size)]
        reactions, humor = {"666", "哈哈", "高能"}, ("哈哈", "节目效果")
        measure(
            "representative_statistics",
            size,
            partial(reference.representative_danmaku_statistics, messages, reactions, humor),
            partial(native.representative_danmaku_statistics, messages, reactions, humor),
            10 if size > 128 else 200,
        )
        times = [index * 0.01 for index in range(size)]
        measure(
            "audio_bucket_indices",
            size,
            partial(reference.audio_bucket_indices, times, 60.0, 10.0, 6),
            partial(native.audio_bucket_indices, times, 60.0, 10.0, 6),
            20 if size > 128 else 500,
        )
    for size in [0, 1, 8, 20, 128]:
        patterns = tuple(f"关键词{i}" for i in range(size))
        text = "关键词0和关键词7，每个房间保持独立" * 20
        measure(
            "keyword_reuse",
            size,
            partial(native._uncached_match_keywords, text, patterns),
            partial(native.fast_match_keywords, text, patterns),
            1000,
        )
    for size in [0, 1, 2, 8, 32, 128, 256]:
        items = [
            {
                "asr_text": f"中文直播游戏第{index % 7}个操作！",
                "keywords": ["游戏", str(index % 5)],
                "start_ts": datetime(2026, 1, 1) + timedelta(seconds=index * 10),
            }
            for index in range(size)
        ]
        measure(
            "cluster_fused_boundary",
            size,
            partial(previous_cluster_pipeline, items),
            partial(native.cluster_candidate_groups, items, 0.82),
            1000 if size < 10 else 10,
        )
    for size in [0, 1, 8, 64, 1024, 65536]:
        raw = bytes([0, 128]) * size
        measure(
            "pcm",
            size,
            partial(reference.pcm16_statistics, raw),
            partial(native.pcm16_statistics, raw),
            10000 if size < 100 else (500 if size < 2000 else 15),
        )
    for size in [0, 1, 8, 64, 4096]:
        texts = ["高能! 😀" if index % 3 else "日常测试" for index in range(size)]
        tokens = ["高能", "笑死"]
        measure(
            "danmaku",
            size,
            partial(reference.danmaku_text_features, texts, tokens),
            partial(native.danmaku_text_features, texts, tokens),
            2000 if size < 100 else 20,
        )
    for size in [0, 1, 2, 4, 8, 16, 32, 33, 64, 128]:
        items = [
            {
                "asr_text": f"中文直播游戏第{index % 7}个操作！",
                "keywords": ["游戏", str(index % 5)],
                "start_ts": datetime(2026, 1, 1) + timedelta(seconds=index * 10),
            }
            for index in range(size)
        ]
        measure(
            "matrix",
            size,
            partial(previous_docker_matrix, items),
            partial(native.cluster_similarity_matrix, items),
            500 if size < 8 else (50 if size < 64 else 3),
        )
    for size in [0, 1, 2, 8, 32, 128, 256]:
        matrix = [[1.0 if i == j else (0.9 if (i + j) % 3 == 0 else 0.2) for j in range(size)] for i in range(size)]
        measure(
            "groups",
            size,
            partial(previous_cluster_groups, matrix, 0.82),
            partial(native.cluster_groups, matrix, 0.82),
            1000 if size < 32 else 10,
        )
    for size in [0, 1, 8, 128, 4096]:
        text = "中文直播ABC1_！新主题" * size
        baseline = "中文直播ABC1_ 日常" * size
        for entities in [False, True]:
            measure(
                "novelty_entities" if entities else "novelty",
                size,
                partial(reference.text_novelty, text, baseline, include_entities=entities),
                partial(native.text_novelty, text, baseline, include_entities=entities),
                1000 if size < 10 else 5,
            )
    for size in [0, 1, 8, 128, 1024]:
        for repeated in [False, True]:
            text = ("重复文本" * (size // 4 + 1))[:size] if repeated else "".join(chr(0x4E00 + i) for i in range(size))
            measure(
                "repeat_loop" if repeated else "repeat_normal",
                size,
                partial(reference.consecutive_repeat_details, text),
                partial(native.consecutive_repeat_details, text),
                100 if size < 10 else 3,
            )
        text = ("ASR原神é\0 测试" * (size // 8 + 1))[:size]
        for name, words in [
            ("review", None),
            ("review_match", ["原神"]),
            ("review_miss", ["缺失热词" * 40, "原神", "测试中"]),
        ]:
            measure(
                name,
                size,
                partial(reference.review_text_statistics, text, words),
                partial(native.review_text_statistics, text, words),
                1000 if size < 10 else 100,
            )
    for size in [0, 1, 8, 64, 512, 4096]:
        history = [float((i * 13) % 37) for i in range(size)]
        measure(
            "rolling",
            size,
            partial(reference.robust_relative_uplift, 21.0, history),
            partial(native.robust_relative_uplift, 21.0, history),
            1000 if size < 100 else 50,
        )
    for size in [0, 9, 10, 64, 512, 4096]:
        timestamps = [float(i // 3) for i in range(size)]
        measure(
            "buckets",
            size,
            partial(reference.danmaku_baseline_rate, timestamps),
            partial(native.danmaku_baseline_rate, timestamps),
            1000 if size < 100 else 50,
        )
    for size in [1, 8, 64, 4096]:
        texts = [f"高能!第{i}条弹幕" for i in range(size)]
        measure(
            "danmaku_unique",
            size,
            partial(reference.danmaku_text_features, texts, ["高能"]),
            partial(native.danmaku_text_features, texts, ["高能"]),
            1000 if size < 100 else 20,
        )
    return rows


def main() -> int:
    """将平台、真实后端与原始测量输出为 JSON，不把局部加速称为整体加速。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"platform": platform.platform(), "python": platform.python_version(), "rows": benchmark()}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
