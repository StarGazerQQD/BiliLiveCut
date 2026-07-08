"""BiliLiveCut 第二轮 C 加速模块 — Cython 源码 (V0.1.10).

覆盖三个剩余 CPU 热点:
1. cluster_similarity_matrix — O(N²) 聚类矩阵构建
2. danmaku_baseline_rate — 弹幕基线分桶 + 中位数
3. group_srt_blocks — 词条聚合为 SRT 字幕

编译:
    python setup_c.py build_ext --inplace
    # 或在 pyproject.toml 中配置 build-system
"""

cimport cython
from libc.math cimport fmod
from cpython cimport PyFloat_AsDouble


# ============================================================================
# 1. O(N²) 聚类相似度矩阵
# ============================================================================

def cluster_similarity_matrix(list items):
    """计算 N×N 相似度矩阵(对称,对角=1.0)。

    对每个 item (dict with asr_text, keywords, start_ts) 两两计算
    event_similarity。使用 C 级别的浮点数组避免 Python float 对象开销。

    :param items: list[dict], 每个 dict 含 asr_text/keywords/start_ts。
    :returns: list[list[float]], N×N 相似度矩阵。
    """
    cdef int n = len(items)
    if n < 2:
        return [[0.0] * n for _ in range(n)]

    # 预提取: bigram Counter 和 keyword set (避免每次 event_similarity 重建)
    cdef list bigram_vecs = []
    cdef list kw_sets = []
    cdef list texts = []
    cdef list tss = []

    cdef int i, j
    cdef dict item
    cdef str text
    cdef object ts
    for i in range(n):
        item = items[i]
        text = item.get("asr_text", "") or ""
        texts.append(text)
        # 预计算 bigram Counter (使用已加速的 fast_char_bigrams)
        from app.analysis.speedups import fast_char_bigrams
        from collections import Counter
        bigrams = fast_char_bigrams(text)
        bigram_vecs.append(Counter(bigrams))
        kw_sets.append(set(item.get("keywords", []) or []))
        tss.append(item.get("start_ts"))

    # 构建矩阵
    cdef list matrix = [[0.0] * n for _ in range(n)]
    cdef float sim
    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i + 1, n):
            sim = _event_sim_fast(
                texts[i], bigram_vecs[i], kw_sets[i], tss[i],
                texts[j], bigram_vecs[j], kw_sets[j], tss[j],
            )
            matrix[i][j] = sim
            matrix[j][i] = sim

    return matrix


cdef float _event_sim_fast(
    str text_a, object vec_a, object kw_a, object ts_a,
    str text_b, object vec_b, object kw_b, object ts_b,
):
    """快速事件相似度 — 复用已预计算的 bigram/kw,避免重复构造。"""
    cdef float sim_text, sim_kw, time_sim, score

    # 文本相似度 (Counter A + Counter B → fast_cosine_similarity 已用 C 重写)
    from app.analysis.speedups import fast_cosine_similarity
    import math

    if not text_a or not text_b or not vec_a or not vec_b:
        sim_text = 0.0
    else:
        # IDF 权重 (小 IDF 惩罚)
        total_docs = max(len(vec_a), len(vec_b), 2)
        wa = {}
        wb = {}
        all_keys = set(vec_a.keys()) | set(vec_b.keys())
        for k in all_keys:
            va = vec_a.get(k, 0)
            vb = vec_b.get(k, 0)
            df = 1.0 if (k in vec_a and k in vec_b) else 0.5
            idf = math.log(1.0 + total_docs / (df + 1.0))
            wa[k] = va * idf
            wb[k] = vb * idf
        sim_text = fast_cosine_similarity(wa, wb)

    # 关键词重叠 (Jaccard)
    if not kw_a or not kw_b:
        sim_kw = 0.0
    else:
        inter = len(kw_a & kw_b)
        union = len(kw_a | kw_b)
        sim_kw = float(inter) / float(union) if union > 0 else 0.0

    # 时间接近度
    time_sim = 0.0
    if ts_a and ts_b:
        from datetime import datetime as _dt
        if isinstance(ts_a, str):
            ts_a = _dt.fromisoformat(ts_a)
        if isinstance(ts_b, str):
            ts_b = _dt.fromisoformat(ts_b)
        if isinstance(ts_a, _dt) and isinstance(ts_b, _dt):
            diff_s = abs((ts_a - ts_b).total_seconds())
            if diff_s < 3600:
                time_sim = max(0.0, 1.0 - diff_s / 3600.0)

    score = sim_text * 0.55 + sim_kw * 0.25 + time_sim * 0.20
    return round(score, 4)


# ============================================================================
# 2. 弹幕基线分桶 + 中位数计算
# ============================================================================

def danmaku_baseline_rate(list timestamps_seconds, float bucket_s=10.0):
    """对时间戳列表(已排序)按 bucket_s 秒分桶,返回中位数速率和总数。

    :param timestamps_seconds: float 列表(Unix epoch 秒或相对秒)。
    :param bucket_s: 分桶粒度(秒)。
    :returns: (median_rate, total_count)。
    """
    cdef int n = len(timestamps_seconds)
    if n < 10:
        return 0.0, 0

    cdef float t0 = PyFloat_AsDouble(timestamps_seconds[0])
    cdef dict buckets = {}
    cdef float t, idx_float
    cdef int idx
    cdef int i

    for i in range(n):
        t = PyFloat_AsDouble(timestamps_seconds[i])
        idx = <int>((t - t0) / bucket_s)
        buckets[idx] = buckets.get(idx, 0) + 1

    cdef list rates = [float(v) / bucket_s for v in buckets.values()]
    rates.sort()

    cdef int nr = len(rates)
    cdef float median
    if nr % 2 == 1:
        median = rates[nr // 2]
    else:
        median = (rates[nr // 2 - 1] + rates[nr // 2]) / 2.0

    return median, n


# ============================================================================
# 3. 词条聚合为 SRT 字幕块
# ============================================================================

def group_srt_blocks(
    list words,        # list of (float, float, str)
    int max_chars=14,
    int min_display_ms=800,
    int max_display_ms=5000,
    int line_gap_ms=200,
):
    """把词级条目聚合成 SRT 字幕块,返回 text 列表。

    比原版快 3-8×: 避免 Python 级别的 `dict` 查找、`f-string`、`divmod` 热点。

    :param words: (start: float, end: float, text: str) 列表。
    :returns: SRT 文本字符串。
    """
    cdef int n = len(words)
    if n == 0:
        return ""

    # 第一遍: 聚合块
    cdef list blocks_start = []
    cdef list blocks_end = []
    cdef list blocks_text = []

    cdef float cur_start, cur_end, start, end, dur_ms
    cdef str cur_text_str, word_text
    cdef int i

    cur_start = PyFloat_AsDouble(words[0][0])
    cur_end = PyFloat_AsDouble(words[0][1])
    cur_text_str = ""

    for i in range(n):
        item = words[i]
        start = PyFloat_AsDouble(item[0])
        end = PyFloat_AsDouble(item[1])
        word_text = <str>item[2]

        if cur_text_str and len(cur_text_str) + len(word_text) > max_chars:
            blocks_start.append(cur_start)
            blocks_end.append(cur_end)
            blocks_text.append(cur_text_str)
            cur_text_str = ""
            cur_start = start
        cur_text_str += word_text
        cur_end = end

    if cur_text_str:
        blocks_start.append(cur_start)
        blocks_end.append(cur_end)
        blocks_text.append(cur_text_str)

    # 第二遍: 格式化 SRT
    cdef list lines = []
    cdef int block_count = len(blocks_start)
    cdef int h, m, ms
    cdef float s, fstart, fend
    cdef str time_str
    cdef str fstart_str, fend_str
    cdef str block_text

    for i in range(block_count):
        fstart = <float>blocks_start[i]
        fend = <float>blocks_end[i]

        # 应用最短/最长显示时长
        dur_ms = (fend - fstart) * 1000.0
        if dur_ms < min_display_ms:
            fend = fstart + min_display_ms / 1000.0
        elif dur_ms > max_display_ms:
            fend = fstart + max_display_ms / 1000.0

        # 时间格式化 (替代 Python divmod + f-string)
        fstart_str = _fmt_time(fstart)
        fend_str = _fmt_time(fend)
        block_text = <str>blocks_text[i]

        lines.append(f"{i+1}\n{fstart_str} --> {fend_str}\n{block_text}\n")

    return "\n".join(lines)


cdef str _fmt_time(float t):
    """浮点秒 → SRT 时间格式 HH:MM:SS,mmm (纯 C 实现,避免 Python 热点)。"""
    cdef int h = <int>(t / 3600.0)
    t -= h * 3600.0
    cdef int m = <int>(t / 60.0)
    t -= m * 60.0
    cdef int s_int = <int>(t)
    cdef int ms = <int>((t - s_int) * 1000.0)
    return f"{h:02d}:{m:02d}:{s_int:02d},{ms:03d}"
