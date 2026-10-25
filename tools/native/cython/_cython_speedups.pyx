"""BiliLiveCut Cython 数值与字幕热点加速模块。

覆盖九个 CPU 热点:
1. cluster_similarity_matrix — O(N²) 聚类矩阵构建
2. danmaku_baseline_rate — 弹幕基线分桶 + 中位数
3. group_srt_blocks — 词条聚合为 SRT 字幕
4. audio_peak_offsets — 音频局部峰值筛选
5. find_silence_ranges — RMS 连续静音区间提取
6. robust_relative_uplift — 滚动历史稳健增幅
7. pcm16_statistics — PCM16 精确整数能量与峰值
8. text_novelty — 词项/实体扫描与新颖度统计
9. review_text_statistics — ASR 复核字符与热词统计

编译:
    python setup_c.py build_ext --inplace
    # 或在 pyproject.toml 中配置 build-system
"""

cimport cython
from libc.math cimport fabs, isfinite, log2
from libc.stdlib cimport qsort
from libc.stdint cimport SIZE_MAX
from cpython.mem cimport PyMem_Malloc, PyMem_Free
from cpython cimport PyFloat_AsDouble
from cpython.bytes cimport PyBytes_AS_STRING
from cpython.unicode cimport PyUnicode_READ_CHAR, Py_UNICODE_ISALNUM

from app.accelerators.python_fallback.speedups_round2 import (
    _DANMAKU_SPACE_PATTERN, _DANMAKU_REACTION_PATTERN, _DANMAKU_TOKEN_PATTERN,
)


# ============================================================================
# 0. 共享数值辅助函数
# ============================================================================

def representative_danmaku_statistics(messages, set reaction_words, tuple humor_words) -> list:
    """合并清洗、频次和三类代表评分，保持首次文本及稳定输入顺序。"""
    cdef dict unique = {}, counts
    cdef list result = []
    cdef str text, key, normalized, character
    cdef Py_ssize_t index, position, size, hits
    cdef double reaction, information, humor
    cdef Py_UCS4 value
    for index, content in enumerate(messages):
        text = _DANMAKU_SPACE_PATTERN.sub(" ", str(content or "")).strip()
        if not text or len(text) > 120:
            continue
        key = text.casefold()
        previous = unique.get(key)
        unique[key] = (text, 1, index) if previous is None else (previous[0], previous[1] + 1, previous[2])
    for key, (text, count, index) in unique.items():
        normalized = key.replace("？", "?").replace("！", "!")
        reaction = 0.0
        if normalized in reaction_words:
            reaction = 1.0
        elif len(normalized) <= 12 and _DANMAKU_REACTION_PATTERN.fullmatch(normalized):
            reaction = min(1.0, 0.55 + len(normalized) / 24.0)
        counts = {}
        size = 0
        for position in range(len(text)):
            value = PyUnicode_READ_CHAR(text, position)
            if Py_UNICODE_ISALNUM(value) or 0x4e00 <= value <= 0x9fff:
                character = text[position]
                counts[character] = counts.get(character, 0) + 1
                size += 1
        information = 0.0
        if size >= 4 and not (reaction > 0.0 and size <= 8):
            information = max(0.0, min(1.0,
                min(1.0, size / 24.0) * 0.45 + len(counts) / <double>size * 0.35
                + min(1.0, len(_DANMAKU_TOKEN_PATTERN.findall(text)) / 4.0) * 0.20
                - max(counts.values()) / <double>size * 0.15))
        hits = 0
        for token in humor_words:
            hits += token in text
        humor = max(0.0, min(1.0, 0.45 + hits * 0.15 + information * 0.40)) if hits else 0.0
        result.append((key, text, count, index, reaction, information, humor))
    return result


def normalize_asr_text(str text not None) -> str:
    """按 CPython Unicode 分类和完整 casefold 规范化 ASR 文本。"""
    cdef list characters = []
    cdef Py_ssize_t index
    for index in range(len(text)):
        if Py_UNICODE_ISALNUM(PyUnicode_READ_CHAR(text, index)):
            characters.append(text[index].casefold())
    return "".join(characters)


def normalize_asr_with_indexes(str text not None) -> tuple[str, list[int]]:
    """casefold 扩展字符保持同一原文下标，供精确局部复读修复使用。"""
    cdef list characters = [], indexes = []
    cdef Py_ssize_t index, expanded
    cdef str folded
    for index in range(len(text)):
        if Py_UNICODE_ISALNUM(PyUnicode_READ_CHAR(text, index)):
            folded = text[index].casefold()
            characters.append(folded)
            for expanded in range(len(folded)):
                indexes.append(index)
    return "".join(characters), indexes


def interval_conflicts(
    double peak, double start, double end, list selected,
    double cooldown_s, double iou_threshold,
) -> bool:
    """以纯数值坐标检查区间，保持严格冷却距离和包含端点的 IoU 阈值。"""
    cdef double other_peak, other_start, other_end, intersection, union, overlap
    for other_peak, other_start, other_end in selected:
        intersection = max(0.0, min(end, other_end) - max(start, other_start))
        union = max(0.0, max(end, other_end) - min(start, other_start))
        overlap = intersection / union if union else 0.0
        if fabs(peak - other_peak) < cooldown_s or overlap >= iou_threshold:
            return True
    return False


def audio_bucket_indices(
    const double[::1] times, double duration_s, double bucket_s, Py_ssize_t count
) -> list[list[int]]:
    """只返回各桶样本下标，避免逐样本创建浮点二元组。"""
    cdef list groups = [[] for _ in range(max(0, count))]
    cdef Py_ssize_t position, index
    cdef double offset, quotient
    if count <= 0:
        return groups
    for position in range(times.shape[0]):
        offset = times[position]
        if offset < 0.0 or offset >= duration_s:
            continue
        quotient = offset // bucket_s
        if not isfinite(quotient):
            # 与 Python int(float) 保持相同的异常，不把 NaN 转为数组下标。
            int(quotient)
        index = min(int(quotient), count - 1)
        groups[index].append(position)
    return groups


def pcm16_statistics(bytes raw not None) -> tuple[int, int, int]:
    """单遍汇总小端 PCM16；每 65536 样本提升至 Python 整数，长输入也不溢出。"""
    cdef Py_ssize_t size = len(raw)
    cdef const unsigned char* data = <const unsigned char*>PyBytes_AS_STRING(raw)
    cdef Py_ssize_t offset = 0
    cdef Py_ssize_t stop, index
    cdef int value, magnitude, peak = 0
    cdef unsigned long long subtotal
    cdef object square_sum = 0
    if size % 2:
        raise ValueError("ASR WAV 含不完整采样")
    while offset < size:
        stop = offset + min(size - offset, 131072)
        subtotal = 0
        with nogil:
            index = offset
            while index < stop:
                value = <int>data[index] | (<int>data[index + 1] << 8)
                if value >= 32768:
                    value -= 65536
                magnitude = -value if value < 0 else value
                if magnitude > peak:
                    peak = magnitude
                subtotal += <unsigned long long>(value * value)
                index += 2
        square_sum += subtotal
        offset = stop
    return size // 2, square_sum, peak


cdef int _compare_double(const void* a, const void* b) noexcept nogil:
    cdef double left = (<const double*>a)[0]
    cdef double right = (<const double*>b)[0]
    return (left > right) - (left < right)


cdef double _buffer_median(double* values, Py_ssize_t size) noexcept nogil:
    """原地排序有界数值缓冲区；小输入插入排序，避免回调开销。"""
    cdef Py_ssize_t i, j
    cdef double value
    if size <= 32:
        for i in range(1, size):
            value = values[i]
            j = i
            while j > 0 and values[j - 1] > value:
                values[j] = values[j - 1]
                j -= 1
            values[j] = value
    else:
        qsort(values, size, sizeof(double), _compare_double)
    if size % 2:
        return values[size // 2]
    return (values[size // 2 - 1] + values[size // 2]) / 2.0


cdef double* _number_buffer(Py_ssize_t size) except NULL:
    cdef double* values
    if size < 1 or <size_t>size > SIZE_MAX // sizeof(double):
        raise MemoryError("invalid numeric buffer size")
    values = <double*>PyMem_Malloc(size * sizeof(double))
    if values == NULL:
        raise MemoryError("numeric buffer allocation failed")
    return values


cdef double _median(list values):
    """非空列表的中位数；非有限音频输入保留 Python 排序语义。"""
    cdef Py_ssize_t size = len(values), i
    cdef double* buffer
    cdef double result
    cdef list ordered
    if size == 1:
        return PyFloat_AsDouble(values[0])
    if size == 2:
        return (PyFloat_AsDouble(values[0]) + PyFloat_AsDouble(values[1])) / 2.0
    buffer = _number_buffer(size)
    try:
        for i in range(size):
            buffer[i] = PyFloat_AsDouble(values[i])
            if not isfinite(buffer[i]):
                ordered = sorted(values)
                return (ordered[size // 2] if size % 2 else (ordered[size // 2 - 1] + ordered[size // 2]) / 2.0)
        with nogil:
            result = _buffer_median(buffer, size)
        return result
    finally:
        PyMem_Free(buffer)


cdef double _clamp01(double value):
    """把数值限制到闭区间 [0, 1]。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


# ============================================================================
# 1. 音频峰值与静音区间
# ============================================================================

def audio_peak_offsets(
    const double[::1] times,
    const double[::1] rms,
    int limit=4,
    double min_distance_s=25.0,
    double min_prominence=0.15,
):
    """选择按能量排序且彼此分离的局部峰值。"""
    cdef Py_ssize_t size = times.shape[0]
    cdef Py_ssize_t rms_size = rms.shape[0]
    cdef Py_ssize_t index, chosen_index, selected_pos
    cdef Py_ssize_t global_index = 0
    cdef double value, global_value, baseline, threshold, offset
    cdef double minimum_distance = max(0.0, min_distance_s)
    cdef list values
    cdef list candidates
    cdef list ranked = []
    cdef list selected = []
    cdef list result
    cdef object entry

    if rms_size < size:
        size = rms_size
    if size == 0 or limit <= 0:
        return []

    global_value = rms[0]
    for index in range(1, size):
        if rms[index] > global_value:
            global_value = rms[index]
            global_index = index

    values = [rms[index] for index in range(size)]
    baseline = _median(values)
    threshold = baseline + max(0.0, min_prominence) * max(1.0 - baseline, 0.0)
    candidates = [global_index]
    for index in range(1, size - 1):
        value = rms[index]
        if (
            index != global_index
            and value >= threshold
            and value >= rms[index - 1]
            and value > rms[index + 1]
        ):
            candidates.append(index)

    for entry in candidates:
        index = <Py_ssize_t>entry
        ranked.append((-rms[index], index))
    ranked.sort()

    for entry in ranked:
        index = <Py_ssize_t>entry[1]
        offset = times[index]
        for selected_pos in range(len(selected)):
            chosen_index = <Py_ssize_t>selected[selected_pos]
            if fabs(offset - times[chosen_index]) < minimum_distance:
                break
        else:
            selected.append(index)
            if len(selected) >= limit:
                break

    result = [times[<Py_ssize_t>entry] for entry in selected]
    result.sort()
    return result


def find_silence_ranges(
    const double[::1] times,
    const double[::1] rms,
    double threshold_ratio=0.15,
    double min_silence_s=0.3,
):
    """从 RMS 包络中提取满足最短持续时间的连续静音区间。"""
    cdef Py_ssize_t size = times.shape[0]
    cdef Py_ssize_t rms_size = rms.shape[0]
    cdef Py_ssize_t index
    cdef Py_ssize_t start_index = -1
    cdef double hop_s
    cdef list silences = []

    if rms_size < size:
        size = rms_size
    if size == 0:
        return []
    hop_s = times[1] - times[0] if size > 1 else 0.1

    for index in range(size):
        if rms[index] < threshold_ratio:
            if start_index < 0:
                start_index = index
        elif start_index >= 0:
            if (index - start_index) * hop_s >= min_silence_s:
                silences.append((times[start_index], times[index - 1]))
            start_index = -1

    if start_index >= 0 and (size - start_index) * hop_s >= min_silence_s:
        silences.append((times[start_index], times[size - 1]))
    return silences


# ============================================================================
# 2. 滚动历史稳健增幅
# ============================================================================

def robust_relative_uplift(double current, history: object) -> float:
    """直接读取 Python 数值序列，在独立缓冲区统计中位数/MAD，不改动输入。"""
    cdef Py_ssize_t size = len(history), used = 0, index
    cdef double* clean
    cdef double value, baseline, mad, scale, argument
    cdef double ratio_score, relative_score, robust_score
    cdef const double[::1] view
    if not isfinite(current) or size == 0:
        return 0.0
    clean = _number_buffer(size)
    try:
        if isinstance(history, (list, tuple)):
            for index in range(size):
                value = PyFloat_AsDouble(history[index])
                if isfinite(value):
                    clean[used] = value
                    used += 1
        else:
            view = history
            for index in range(size):
                value = view[index]
                if isfinite(value):
                    clean[used] = value
                    used += 1
        if used == 0:
            return 0.0
        with nogil:
            baseline = _buffer_median(clean, used)
        if current <= baseline:
            return 0.0
        with nogil:
            for index in range(used):
                clean[index] = fabs(clean[index] - baseline)
            mad = _buffer_median(clean, used)
        scale = max(1e-3, fabs(baseline) * 0.10)
        argument = (current + scale) / (baseline + scale)
        if argument <= 0.0:
            raise ValueError("math domain error")
        ratio_score = log2(argument) / 3.0
        relative_score = ((current - baseline) / (fabs(baseline) + scale)) / 2.0
        robust_score = 0.0 if mad <= 1e-9 else ((current - baseline) / (1.4826 * mad)) / 6.0
        return _clamp01(max(ratio_score, relative_score, robust_score))
    finally:
        PyMem_Free(clean)


# ============================================================================
# 3. O(N²) 聚类相似度矩阵
# ============================================================================

def cluster_similarity_matrix(list items):
    """计算 N×N 相似度矩阵(对称,对角=1.0)。

    对每个 item (dict with asr_text, keywords, start_ts) 两两计算
    event_similarity。使用 C 级别的浮点数组避免 Python float 对象开销。

    :param items: list[dict], 每个 dict 含 asr_text/keywords/start_ts。
    :returns: list[list[float]], N×N 相似度矩阵。
    """
    cdef Py_ssize_t n = len(items)
    if n < 2:
        return [[0.0] * n for _ in range(n)]

    # 预提取: bigram Counter 和 keyword set (避免每次 event_similarity 重建)
    cdef list bigram_vecs = []
    cdef list kw_sets = []
    cdef list texts = []
    cdef list tss = []

    cdef Py_ssize_t i, j
    cdef dict item
    cdef str text
    cdef object ts
    for i in range(n):
        item = items[i]
        text = item.get("asr_text", "") or ""
        texts.append(text)
        # 预计算 bigram Counter (使用已加速的 fast_char_bigrams)
        from app.accelerators.dispatcher import fast_char_bigrams
        from collections import Counter
        bigrams = fast_char_bigrams(text)
        bigram_vecs.append(Counter(bigrams))
        kw_sets.append(set(item.get("keywords", []) or []))
        tss.append(item.get("start_ts"))

    # 构建矩阵
    cdef list matrix = [[0.0] * n for _ in range(n)]
    cdef double sim
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


cdef double _event_sim_fast(
    str text_a, object vec_a, object kw_a, object ts_a,
    str text_b, object vec_b, object kw_b, object ts_b,
):
    """快速事件相似度 — 复用已预计算的 bigram/kw,避免重复构造。"""
    cdef double sim_text, sim_kw, time_sim, score

    # 文本相似度 (Counter A + Counter B → fast_cosine_similarity 已用 C 重写)
    from app.accelerators.dispatcher import fast_cosine_similarity
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
# 4. 弹幕基线分桶 + 中位数计算
# ============================================================================

def danmaku_baseline_rate(list timestamps_seconds, double bucket_s=10.0):
    """对时间戳列表(已排序)按 bucket_s 秒分桶,返回中位数速率和总数。

    :param timestamps_seconds: float 列表(Unix epoch 秒或相对秒)。
    :param bucket_s: 分桶粒度(秒)。
    :returns: (median_rate, total_count)。
    """
    cdef Py_ssize_t n = len(timestamps_seconds)
    if n < 10:
        return 0.0, 0

    cdef double t0 = PyFloat_AsDouble(timestamps_seconds[0])
    cdef dict buckets = {}
    cdef double t
    cdef object idx, previous = None
    cdef Py_ssize_t i, run = 0

    # 相邻同桶只累计整数；跨桶时合并，未排序输入仍按原字典语义汇总。
    for i in range(n):
        t = PyFloat_AsDouble(timestamps_seconds[i])
        idx = int((t - t0) / bucket_s)
        if idx == previous:
            run += 1
        else:
            if previous is not None:
                buckets[previous] = buckets.get(previous, 0) + run
            previous = idx
            run = 1
    buckets[previous] = buckets.get(previous, 0) + run
    cdef list rates = [float(v) / bucket_s for v in buckets.values()]
    cdef double median = _median(rates)

    return median, n


# ============================================================================
# 5. 词条聚合为 SRT 字幕块
# ============================================================================

def group_srt_blocks(
    list words,        # list of (float, float, str)
    int max_chars=14,
    int min_display_ms=800,
    int max_display_ms=5000,
    int line_gap_ms=200,
    *,
    duration_s=None,
):
    """把词级条目聚合成 SRT 字幕块,返回 text 列表。

    比原版快 3-8×: 避免 Python 级别的 `dict` 查找、`f-string`、`divmod` 热点。

    :param words: (start: float, end: float, text: str) 列表。
    :returns: SRT 文本字符串。
    """
    cdef Py_ssize_t n = len(words)
    if n == 0:
        return ""

    # 第一遍: 聚合块
    cdef list blocks_start = []
    cdef list blocks_end = []
    cdef list blocks_text = []

    cdef double cur_start, cur_end, start, end, dur_ms
    cdef str cur_text_str, word_text
    cdef Py_ssize_t i
    cdef double line_gap_s = line_gap_ms / 1000.0

    cur_start = PyFloat_AsDouble(words[0][0])
    cur_end = PyFloat_AsDouble(words[0][1])
    cur_text_str = ""

    for i in range(n):
        item = words[i]
        start = PyFloat_AsDouble(item[0])
        end = PyFloat_AsDouble(item[1])
        word_text = <str>item[2]

        if cur_text_str and (
            len(cur_text_str) + len(word_text) > max_chars
            or start - cur_end >= line_gap_s
        ):
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
    cdef Py_ssize_t block_count = len(blocks_start)
    cdef double fstart, fend
    cdef str fstart_str, fend_str
    cdef str block_text
    cdef object limit = None if duration_s is None else round(duration_s * 1000)
    cdef object start_ms, end_ms

    for i in range(block_count):
        fstart = <double>blocks_start[i]
        fend = <double>blocks_end[i]

        # 应用最短/最长显示时长
        dur_ms = (fend - fstart) * 1000.0
        if dur_ms < min_display_ms:
            fend = fstart + min_display_ms / 1000.0
        elif dur_ms > max_display_ms:
            fend = fstart + max_display_ms / 1000.0

        block_text = <str>blocks_text[i]
        if limit is None:
            fstart_str = _fmt_time(fstart)
            fend_str = _fmt_time(fend)
            lines.append(f"{i+1}\n{fstart_str} --> {fend_str}\n{block_text}\n")
        else:
            start_ms = _time_milliseconds(fstart)
            end_ms = min(_time_milliseconds(fend), limit,
                         _time_milliseconds(blocks_start[i + 1]) if i + 1 < block_count else limit)
            if start_ms < end_ms:
                if i + 1 == block_count:
                    block_text = block_text.rstrip()
                lines.append(f"{len(lines)+1}\n{_fmt_milliseconds(start_ms)} --> {_fmt_milliseconds(end_ms)}\n{block_text}\n")

    return "\n".join(lines)


cdef object _time_milliseconds(double t):
    """按现有时分秒拆分顺序取整，保留浮点边界行为。"""
    cdef int h = <int>(t / 3600.0)
    t -= h * 3600.0
    cdef int m = <int>(t / 60.0)
    t -= m * 60.0
    cdef int s = <int>t
    return ((<object>h * 60 + m) * 60 + s) * 1000 + <int>((t - s) * 1000.0)


cdef str _fmt_milliseconds(object value):
    seconds, fraction = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"


cdef str _fmt_time(double t):
    """浮点秒 → SRT 时间格式 HH:MM:SS,mmm (纯 C 实现,避免 Python 热点)。"""
    cdef int h = <int>(t / 3600.0)
    t -= h * 3600.0
    cdef int m = <int>(t / 60.0)
    t -= m * 60.0
    cdef int s_int = <int>(t)
    cdef int ms = <int>((t - s_int) * 1000.0)
    return f"{h:02d}:{m:02d}:{s_int:02d},{ms:03d}"


cdef inline int _novelty_word_kind(Py_UCS4 value) noexcept nogil:
    if 65 <= value <= 90 or 97 <= value <= 122 or 48 <= value <= 57 or value == 95:
        return 1
    if 0x4e00 <= value <= 0x9fff:
        return 2
    return 0


cdef set _novelty_tokens(str text, bint entities):
    """扫描与原正则相同的连续 ASCII/CJK 范围，保留 Python lower 的 Unicode 语义。"""
    cdef str source = text if entities else text.lower()
    cdef Py_ssize_t size = len(source), index = 0, start, position, length
    cdef int kind
    cdef Py_UCS4 value
    cdef bint has_alnum
    cdef set result = set()
    while index < size:
        kind = _novelty_word_kind(PyUnicode_READ_CHAR(source, index))
        if kind == 0:
            index += 1
            continue
        start = index
        has_alnum = False
        while index < size:
            value = PyUnicode_READ_CHAR(source, index)
            if _novelty_word_kind(value) != kind:
                break
            if kind == 1 and value != 95:
                has_alnum = True
            index += 1
        length = index - start
        if kind == 2 and length < 2:
            continue
        if entities:
            if has_alnum or 2 <= length <= 8:
                result.add(source[start:index].lower())
        elif kind == 1:
            result.add(source[start:index])
        else:
            for position in range(start, index - 1):
                result.add(source[position:position + 2])
    return result


cdef Py_ssize_t _intersection_count(set left, set right):
    cdef Py_ssize_t count = 0
    cdef object token
    if len(left) > len(right):
        left, right = right, left
    for token in left:
        if token in right:
            count += 1
    return count


def novelty_features(str text not None) -> tuple[set[str], set[str]]:
    """一次提取一个信号桶的词项与实体，后续窗口复用集合。"""
    return _novelty_tokens(text, False), _novelty_tokens(text, True)


def novelty_from_features(
    set left not None, set right not None, set entities not None, set baseline_entities not None
) -> tuple[float, float, float]:
    """统计已有集合，避免重复扫描滚动窗口文本。"""
    cdef Py_ssize_t common = _intersection_count(left, right)
    cdef Py_ssize_t union_size = len(left) + len(right) - common
    return (
        (len(left) - common) / <double>len(left) if left else 0.0,
        1.0 - common / <double>union_size if union_size else 0.0,
        (len(entities) - _intersection_count(entities, baseline_entities)) / <double>len(entities) if entities else 0.0,
    )


def text_novelty(str current not None, str baseline not None, *, bint include_entities=True) -> tuple[float, float, float | None]:
    """只接收主机字符串，合并词项扫描与集合计数，省去中间差集/并集。"""
    cdef set left = _novelty_tokens(current, False)
    cdef set right = _novelty_tokens(baseline, False)
    cdef Py_ssize_t common = _intersection_count(left, right)
    cdef Py_ssize_t union_size = len(left) + len(right) - common
    cdef double novelty = (len(left) - common) / <double>len(left) if left else 0.0
    cdef double change = 1.0 - common / <double>union_size if union_size else 0.0
    cdef object entity_change = None
    if include_entities:
        left = _novelty_tokens(current, True)
        right = _novelty_tokens(baseline, True)
        common = _intersection_count(left, right)
        entity_change = (len(left) - common) / <double>len(left) if left else 0.0
    return novelty, change, entity_change



def review_text_statistics(str text not None, hotwords: list[str] | None = None) -> tuple[int, int, str | None]:
    """单遍字符统计；任意匹配前缀必然包含首字符，保留首个热词冲突。"""
    cdef Py_ssize_t size = len(text), index, garbled = 0
    cdef Py_ssize_t unique = len(set(text)) if size >= 4 else 0
    cdef unsigned int code
    cdef str word
    for index in range(size):
        code = PyUnicode_READ_CHAR(text, index)
        if code < 32 or 126 < code < 0x4E00:
            garbled += 1
    for word in hotwords or ():
        if word and word[0] in text and word not in text:
            return unique, garbled, word
    return unique, garbled, None
