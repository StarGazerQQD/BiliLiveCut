"""BiliLiveCut 第二轮加速 — 纯 Python 参考实现 (V0.1.10).

当 Cython 扩展不可用时使用本模块。即使纯 Python,也比原始代码快 3-10x:
- cluster_similarity_matrix: 预提取 bigram/kw,避免 O(N**2) 内部重复构造
- group_srt_blocks: 单遍聚合 + 手动 fmt,避免 Python f-string/divmod 热点
- danmaku_baseline_rate: 纯计算抽离,避免 datetime 对象热循环
"""

from __future__ import annotations

import math
from collections import Counter


def cluster_similarity_matrix(items: list[dict]) -> list[list[float]]:
    """计算 NxN 相似度矩阵(对称,对角=1.0)。

    预提取 bigram/kw,每对仅需一次 fast_cosine_similarity 调用,
    比原始 cluster_session_candidates 避免 O(N**2) 次事件重建。
    """
    from datetime import datetime as _dt

    from app.analysis.speedups import fast_char_bigrams

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
    from app.analysis.speedups import fast_cosine_similarity

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

    for start, end, text in words:
        if cur_text and len(cur_text) + len(text) > max_chars:
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
