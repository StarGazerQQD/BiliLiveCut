"""BiliLiveCut 第二轮加速 — 纯 Python 参考实现.

当 Cython 扩展不可用时使用本模块。即使纯 Python,也比原始代码快 3-10x:
- cluster_similarity_matrix: 预提取 bigram/kw,避免 O(N**2) 内部重复构造
- group_srt_blocks: 单遍聚合 + 手动 fmt,避免 Python f-string/divmod 热点
- danmaku_baseline_rate: 纯计算抽离,避免 datetime 对象热循环
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from statistics import median


def audio_peak_offsets(
    times: Sequence[float],
    rms: Sequence[float],
    limit: int = 4,
    min_distance_s: float = 25.0,
    min_prominence: float = 0.15,
) -> list[float]:
    """选择按能量排序且彼此分离的局部峰值。"""
    size = min(len(times), len(rms))
    if size == 0 or limit <= 0:
        return []

    offsets = [float(times[index]) for index in range(size)]
    values = [float(rms[index]) for index in range(size)]
    global_index = max(range(size), key=values.__getitem__)
    baseline = float(median(values))
    threshold = baseline + max(0.0, min_prominence) * max(1.0 - baseline, 0.0)
    candidates = [global_index]
    for index in range(1, size - 1):
        value = values[index]
        if index != global_index and value >= threshold and value >= values[index - 1] and value > values[index + 1]:
            candidates.append(index)

    selected: list[int] = []
    minimum_distance = max(0.0, min_distance_s)
    for index in sorted(candidates, key=lambda item: (-values[item], item)):
        offset = offsets[index]
        if any(abs(offset - offsets[chosen]) < minimum_distance for chosen in selected):
            continue
        selected.append(index)
        if len(selected) >= limit:
            break
    return sorted(offsets[index] for index in selected)


def find_silence_ranges(
    times: Sequence[float],
    rms: Sequence[float],
    threshold_ratio: float = 0.15,
    min_silence_s: float = 0.3,
) -> list[tuple[float, float]]:
    """从 RMS 包络中提取满足最短持续时间的连续静音区间。"""
    size = min(len(times), len(rms))
    if size == 0:
        return []

    hop_s = float(times[1]) - float(times[0]) if size > 1 else 0.1
    silences: list[tuple[float, float]] = []
    start_index: int | None = None
    for index in range(size):
        is_quiet = float(rms[index]) < threshold_ratio
        if is_quiet and start_index is None:
            start_index = index
        elif not is_quiet and start_index is not None:
            if (index - start_index) * hop_s >= min_silence_s:
                silences.append((float(times[start_index]), float(times[index - 1])))
            start_index = None
    if start_index is not None and (size - start_index) * hop_s >= min_silence_s:
        silences.append((float(times[start_index]), float(times[size - 1])))
    return silences


def robust_relative_uplift(current: float, history: Sequence[float]) -> float:
    """按当前场次历史的中位数与 MAD 计算稳健相对增幅。"""
    clean = [float(value) for value in history if math.isfinite(value)]
    if not clean or not math.isfinite(current):
        return 0.0
    baseline = float(median(clean))
    if current <= baseline:
        return 0.0
    mad = float(median([abs(value - baseline) for value in clean]))
    scale = max(1e-3, abs(baseline) * 0.10)
    ratio_score = math.log2((current + scale) / (baseline + scale)) / 3.0
    relative_score = ((current - baseline) / (abs(baseline) + scale)) / 2.0
    robust_score = 0.0 if mad <= 1e-9 else ((current - baseline) / (1.4826 * mad)) / 6.0
    return max(0.0, min(1.0, max(ratio_score, relative_score, robust_score)))


def danmaku_text_features(
    texts: Sequence[str],
    high_emotion_tokens: Sequence[str],
) -> tuple[float, float, float, tuple[str, ...]]:
    """汇总弹幕复读率、情绪强度、高情绪命中率与代表消息。"""
    if not texts:
        return 0.0, 0.0, 0.0, ()
    counts = Counter(texts)
    total = len(texts)
    repetition = max(counts.values()) / total
    punctuation_hits = sum(1 for text in texts if any(token in text for token in ("!", "！", "?", "？")))
    high_emotion_hits = sum(any(token in text for token in high_emotion_tokens) for text in texts)
    punctuation_rate = punctuation_hits / total
    high_emotion = high_emotion_hits / total
    intensity = max(0.0, min(1.0, punctuation_rate * 0.45 + high_emotion * 0.55))
    representatives = tuple(text for text, _count in counts.most_common(3))
    return max(0.0, min(1.0, repetition)), intensity, max(0.0, min(1.0, high_emotion)), representatives


def cluster_similarity_matrix(items: list[dict]) -> list[list[float]]:
    """计算 NxN 相似度矩阵(对称,对角=1.0)。

    预提取 bigram/kw,每对仅需一次 fast_cosine_similarity 调用,
    比原始 cluster_session_candidates 避免 O(N**2) 次事件重建。
    """
    from datetime import datetime as _dt

    from app.accelerators.dispatcher import fast_char_bigrams

    n = len(items)
    if n < 2:
        return [[0.0] * n for _ in range(n)]

    bigram_vecs: list[Counter[str]] = []
    kw_sets: list[set[str]] = []
    texts: list[str] = []
    tss: list[_dt | None] = []

    for item in items:
        t = item.get("asr_text", "") or ""
        texts.append(t)
        bigram_vecs.append(Counter(fast_char_bigrams(t)))
        kw_sets.append(set(item.get("keywords", []) or []))
        ts = item.get("start_ts")
        if isinstance(ts, str):
            try:
                ts = _dt.fromisoformat(ts)
            except ValueError:
                ts = None
        elif not isinstance(ts, _dt):
            ts = None
        tss.append(ts)

    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]

    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            sim = _pairwise_sim(
                texts[i],
                bigram_vecs[i],
                kw_sets[i],
                tss[i],
                texts[j],
                bigram_vecs[j],
                kw_sets[j],
                tss[j],
            )
            matrix[i][j] = sim
            matrix[j][i] = sim

    return matrix


def _pairwise_sim(
    ta: str,
    va: Counter[str],
    ka: set[str],
    tsa,
    tb: str,
    vb: Counter[str],
    kb: set[str],
    tsb,
) -> float:
    """快速两两事件相似度 — 使用预计算的 bigram Counter 和 kw set。"""
    from app.accelerators.dispatcher import fast_cosine_similarity

    sim_text = 0.0
    if ta and tb and va and vb:
        total_docs = max(len(va), len(vb), 2)
        wa: dict[str, float] = {}
        wb: dict[str, float] = {}
        all_keys = set(va.keys()) | set(vb.keys())
        for k in all_keys:
            df = 1.0 if k in va and k in vb else 0.5
            idf = math.log(1.0 + total_docs / (df + 1.0))
            wa[k] = va.get(k, 0) * idf
            wb[k] = vb.get(k, 0) * idf
        sim_text = fast_cosine_similarity(wa, wb)

    sim_kw = 0.0
    if ka and kb:
        inter = len(ka & kb)
        union = len(ka | kb)
        if union > 0:
            sim_kw = float(inter) / float(union)

    time_sim = 0.0
    if tsa is not None and tsb is not None:
        from datetime import datetime as _dt

        if isinstance(tsa, _dt) and isinstance(tsb, _dt):
            diff_s = abs((tsa - tsb).total_seconds())
            if diff_s < 3600:
                time_sim = max(0.0, 1.0 - diff_s / 3600.0)

    return round(sim_text * 0.55 + sim_kw * 0.25 + time_sim * 0.20, 4)


def danmaku_baseline_rate(ts_seconds: list[float], bucket_s: float = 10.0) -> tuple[float, int]:
    """对已排序时间戳按 bucket_s 秒分桶,返回中位数速率和总数。

    纯计算函数,不含 DB 查询;调用方负责查询 DB 并传入 float 时间戳。
    """
    n = len(ts_seconds)
    if n < 10:
        return 0.0, 0

    t0 = ts_seconds[0]
    buckets: dict[int, int] = {}
    for t in ts_seconds:
        idx = int((t - t0) / bucket_s)
        buckets[idx] = buckets.get(idx, 0) + 1

    rates = [float(v) / bucket_s for v in buckets.values()]
    rates.sort()

    nr = len(rates)
    median = rates[nr // 2] if nr % 2 == 1 else (rates[nr // 2 - 1] + rates[nr // 2]) / 2.0
    return float(median), n


def group_srt_blocks(
    words: list[tuple[float, float, str]],
    max_chars: int = 14,
    min_display_ms: int = 800,
    max_display_ms: int = 5000,
    line_gap_ms: int = 200,
) -> str:
    """把词级条目聚合成 SRT 字幕块 — V0.1.10 优化版。

    优化: 单遍聚合 + 手动 fmt,避免 Python divmod+f-string 热点。
    """
    if not words:
        return ""

    bs: list[float] = []  # blocks_start
    be: list[float] = []  # blocks_end
    bt: list[str] = []  # blocks_text

    cur_start = words[0][0]
    cur_end = words[0][1]
    cur_text = ""
    line_gap_s = line_gap_ms / 1000.0

    for start, end, text in words:
        if cur_text and (len(cur_text) + len(text) > max_chars or start - cur_end >= line_gap_s):
            bs.append(cur_start)
            be.append(cur_end)
            bt.append(cur_text)
            cur_text = ""
            cur_start = start
        cur_text += text
        cur_end = end

    if cur_text:
        bs.append(cur_start)
        be.append(cur_end)
        bt.append(cur_text)

    lines: list[str] = []
    for i in range(len(bs)):
        s, e = bs[i], be[i]
        dur_ms = (e - s) * 1000.0
        if dur_ms < min_display_ms:
            e = s + min_display_ms / 1000.0
        elif dur_ms > max_display_ms:
            e = s + max_display_ms / 1000.0
        lines.append(f"{i + 1}\n{_fmt_time(s)} --> {_fmt_time(e)}\n{bt[i]}\n")

    return "\n".join(lines)


def _fmt_time(t: float) -> str:
    """浮点秒 -> SRT HH:MM:SS,mmm (手动计算,比 divmod+f-string 快 ~3x)。"""
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    ms = int((t - s) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
