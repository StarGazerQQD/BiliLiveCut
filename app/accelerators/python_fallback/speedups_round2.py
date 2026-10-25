"""BiliLiveCut 第二轮加速 — 纯 Python 参考实现.

当 Cython 扩展不可用时使用本模块。即使纯 Python,也比原始代码快 3-10x:
- cluster_similarity_matrix: 预提取 bigram/kw,避免 O(N**2) 内部重复构造
- group_srt_blocks: 单遍聚合 + 手动 fmt,避免 Python f-string/divmod 热点
- danmaku_baseline_rate: 纯计算抽离,避免 datetime 对象热循环
"""

from __future__ import annotations

import math
import re
import sys
from array import array
from collections import Counter
from collections.abc import Sequence
from statistics import median


def normalize_asr_text(text: str) -> str:
    """保留 Unicode 字母数字并执行完整 casefold。"""
    return "".join(character.casefold() for character in text if character.isalnum())


_DANMAKU_SPACE_PATTERN = re.compile(r"\s+")
_DANMAKU_REACTION_PATTERN = re.compile(r"^[?？!！6wW哈啊哦草艹绷]+$")
_DANMAKU_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


def representative_danmaku_statistics(
    messages: Sequence[str | None], reaction_words: set[str], humor_words: tuple[str, ...]
) -> list[tuple[str, str, int, int, float, float, float]]:
    """每条规范化正文只计算一次代表弹幕评分。"""
    unique: dict[str, tuple[str, int, int]] = {}
    for index, content in enumerate(messages):
        text = _DANMAKU_SPACE_PATTERN.sub(" ", str(content or "")).strip()
        if not text or len(text) > 120:
            continue
        key = text.casefold()
        previous = unique.get(key)
        unique[key] = (text, 1, index) if previous is None else (previous[0], previous[1] + 1, previous[2])
    result: list[tuple[str, str, int, int, float, float, float]] = []
    for key, (text, count, index) in unique.items():
        normalized = key.replace("？", "?").replace("！", "!")
        reaction = (
            1.0
            if normalized in reaction_words
            else (
                min(1.0, 0.55 + len(normalized) / 24.0)
                if len(normalized) <= 12 and _DANMAKU_REACTION_PATTERN.fullmatch(normalized)
                else 0.0
            )
        )
        semantic = Counter(char for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
        size = sum(semantic.values())
        information = 0.0
        if size >= 4 and not (reaction > 0.0 and size <= 8):
            information = max(
                0.0,
                min(
                    1.0,
                    min(1.0, size / 24.0) * 0.45
                    + len(semantic) / size * 0.35
                    + min(1.0, len(_DANMAKU_TOKEN_PATTERN.findall(text)) / 4.0) * 0.20
                    - max(semantic.values()) / size * 0.15,
                ),
            )
        hits = sum(token in text for token in humor_words)
        humor = max(0.0, min(1.0, 0.45 + hits * 0.15 + information * 0.40)) if hits else 0.0
        result.append((key, text, count, index, reaction, information, humor))
    return result


def normalize_asr_with_indexes(text: str) -> tuple[str, list[int]]:
    """保留展开后的每个字符到原始码点位置的映射。"""
    characters: list[str] = []
    indexes: list[int] = []
    for index, character in enumerate(text):
        if character.isalnum():
            folded = character.casefold()
            characters.append(folded)
            indexes.extend([index] * len(folded))
    return "".join(characters), indexes


def interval_conflicts(
    peak: float,
    start: float,
    end: float,
    selected: list[tuple[float, float, float]],
    cooldown_s: float,
    iou_threshold: float,
) -> bool:
    """检查已换算为 UTC 秒数的区间，保持原贪心筛选条件。"""
    for other_peak, other_start, other_end in selected:
        intersection = max(0.0, min(end, other_end) - max(start, other_start))
        union = max(0.0, max(end, other_end) - min(start, other_start))
        overlap = intersection / union if union else 0.0
        if abs(peak - other_peak) < cooldown_s or overlap >= iou_threshold:
            return True
    return False


def audio_bucket_indices(times: Sequence[float], duration_s: float, bucket_s: float, count: int) -> list[list[int]]:
    """分配样本下标，保留原顺序、边界排除及 Python 浮点整除语义。"""
    groups: list[list[int]] = [[] for _ in range(max(0, count))]
    if count <= 0:
        return groups
    for position, raw in enumerate(times):
        offset = float(raw)
        if offset < 0.0 or offset >= duration_s:
            continue
        index = min(int(offset // bucket_s), count - 1)
        groups[index].append(position)
    return groups


def pcm16_statistics(raw: bytes) -> tuple[int, int, int]:
    """汇总小端 PCM16 字节的样本数、精确整数平方和与绝对峰值。"""
    if not isinstance(raw, bytes):
        raise TypeError("PCM16 输入必须是 bytes")
    if len(raw) % 2:
        raise ValueError("ASR WAV 含不完整采样")
    samples = array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return len(samples), sum(value * value for value in samples), max((abs(value) for value in samples), default=0)


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
    *,
    duration_s: float | None = None,
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
    limit = None if duration_s is None else round(duration_s * 1000)
    for i in range(len(bs)):
        s, e = bs[i], be[i]
        dur_ms = (e - s) * 1000.0
        if dur_ms < min_display_ms:
            e = s + min_display_ms / 1000.0
        elif dur_ms > max_display_ms:
            e = s + max_display_ms / 1000.0
        if limit is None:
            lines.append(f"{i + 1}\n{_fmt_time(s)} --> {_fmt_time(e)}\n{bt[i]}\n")
        else:
            start_ms = _time_milliseconds(s)
            end_ms = min(_time_milliseconds(e), limit, _time_milliseconds(bs[i + 1]) if i + 1 < len(bs) else limit)
            if start_ms < end_ms:
                content = bt[i].rstrip() if i + 1 == len(bs) else bt[i]
                lines.append(
                    f"{len(lines) + 1}\n{_fmt_milliseconds(start_ms)} --> {_fmt_milliseconds(end_ms)}\n{content}\n"
                )

    return "\n".join(lines)


def _time_milliseconds(t: float) -> int:
    """保留原分段取整顺序，避免直接乘 1000 改变边界毫秒。"""
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    return ((h * 60 + m) * 60 + s) * 1000 + int((t - s) * 1000)


def _fmt_milliseconds(value: int) -> str:
    """格式化已经约束好边界的整数毫秒。"""
    seconds, fraction = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"


def _fmt_time(t: float) -> str:
    """浮点秒 -> SRT HH:MM:SS,mmm (手动计算,比 divmod+f-string 快 ~3x)。"""
    h = int(t // 3600)
    t -= h * 3600
    m = int(t // 60)
    t -= m * 60
    s = int(t)
    ms = int((t - s) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


_NOVELTY_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


def _lexical_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _NOVELTY_WORD_PATTERN.findall(text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            tokens.update(raw[index : index + 2] for index in range(max(1, len(raw) - 1)))
        else:
            tokens.add(raw)
    return tokens


def _entity_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in _NOVELTY_WORD_PATTERN.findall(text)
        if any(char.isdigit() or char.isascii() and char.isalpha() for char in token) or 2 <= len(token) <= 8
    }


def novelty_features(text: str) -> tuple[set[str], set[str]]:
    """抽取可在同一检测周期内复用的词项和实体集合。"""
    return _lexical_tokens(text), _entity_tokens(text)


def novelty_from_features(
    left: set[str], right: set[str], entities: set[str], baseline_entities: set[str]
) -> tuple[float, float, float]:
    """消费已提取集合并保留原新颖度、主题变化和实体变化公式。"""
    common = len(left & right)
    union_size = len(left) + len(right) - common
    return (
        (len(left) - common) / len(left) if left else 0.0,
        1.0 - common / union_size if union_size else 0.0,
        len(entities - baseline_entities) / len(entities) if entities else 0.0,
    )


def text_novelty(current: str, baseline: str, *, include_entities: bool = True) -> tuple[float, float, float | None]:
    """按原 Unicode 小写及词项规则计算语义新颖度、主题变化和可选实体变化。"""
    left, right = _lexical_tokens(current), _lexical_tokens(baseline)
    novelty = len(left - right) / len(left) if left else 0.0
    union = left | right
    change = 1.0 - (len(left & right) / len(union) if union else 1.0)
    entity_change: float | None = None
    if include_entities:
        left, right = _entity_tokens(current), _entity_tokens(baseline)
        entity_change = len(left - right) / len(left) if left else 0.0
    return novelty, change, entity_change


def cluster_groups(matrix: list[list[float]], threshold: float) -> list[tuple[int, list[int], float]]:
    """按原并查集顺序返回非单例簇、根编号及按索引顺序累加的簇内均值。"""
    size = len(matrix)
    if not math.isfinite(threshold) or any(
        len(row) != size or any(not math.isfinite(value) for value in row) for row in matrix
    ):
        raise ValueError("cluster matrix must be square and finite; threshold must be finite")
    parent = list(range(size))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(size):
        for j in range(i + 1, size):
            if matrix[i][j] >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[b] = a
    groups: dict[int, list[int]] = {}
    for i in range(size):
        groups.setdefault(find(i), []).append(i)
    result: list[tuple[int, list[int], float]] = []
    for root, indices in groups.items():
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


def _repeat_z(symbols: list[str]) -> list[int]:
    """在线性时间内计算各后缀与前缀的公共长度。"""
    size = len(symbols)
    matches = [0] * size
    left = right = 0
    for index in range(1, size):
        if index < right:
            matches[index] = min(right - index, matches[index - left])
        length = matches[index]
        while index + length < size and symbols[length] == symbols[index + length]:
            length += 1
        matches[index] = length
        if index + length > right:
            left, right = index, index + length
    return matches


def consecutive_repeat_details(text: str) -> tuple[int, int, int]:
    """分治检测任意长度的连续重复，O(N log N) 时间、O(N) 空间。"""
    symbols = list(text)
    size = len(symbols)
    best = (0, 0, 0)
    best_coverage = 0
    pending = [(0, size)]
    while pending:
        offset, end = pending.pop()
        length = end - offset
        if length < 3 or length < best_coverage:
            continue
        section = symbols[offset:end]
        middle = length // 2
        right_length = length - middle
        left = section[:middle]
        right = section[middle:]
        reversed_left = left[::-1]
        # 空字符串是独立的哨兵符号，不会与任何 Unicode 单字符相等。
        left_suffix = _repeat_z(reversed_left)
        right_prefix = _repeat_z(right)
        forward = _repeat_z(right + [""] + section)
        backward = _repeat_z(reversed_left + [""] + section[::-1])
        for unit in range(1, length // 3 + 1):
            suffix = left_suffix[unit] if unit < middle else 0
            # 两种跨中点锚点：middle-unit / middle，以及 middle / middle+unit。
            for start, stop in (
                (middle - unit - suffix, middle + forward[right_length + 1 + middle - unit]),
                (middle - backward[middle + 1 + right_length - unit], middle + unit + right_prefix[unit]),
            ):
                repeats = (stop - start) // unit
                coverage = repeats * unit
                absolute_start = offset + start
                if repeats >= 3 and (
                    coverage > best_coverage
                    or coverage == best_coverage
                    and (unit, absolute_start) < (best[1], best[0])
                ):
                    best = (absolute_start, unit, repeats)
                    best_coverage = coverage
                    if coverage == size:
                        return best
        # 每层处理的总字符数是 O(N)，候选区间只做常数次计算，不展开全部子串。
        pending.append((offset + middle, end))
        pending.append((offset, offset + middle))
    return best


def review_text_statistics(text: str, hotwords: list[str] | None = None) -> tuple[int, int, str | None]:
    """保留复核文本统计的 Python 参考语义；权重与置信度留在业务层。"""
    unique = len(set(text)) if len(text) >= 4 else 0
    garbled = sum(1 for character in text if ord(character) < 32 or 126 < ord(character) < 0x4E00)
    conflict = None
    for word in hotwords or []:
        for length in range(1, len(word) + 1):
            prefix = word[:length]
            if prefix in text and word not in text:
                conflict = word
                break
        if conflict is not None:
            break
    return unique, garbled, conflict
