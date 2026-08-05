"""高光判断:多维规则打分 + 可选 LLM 复核 + 边界吸附 + 查重 + 候选入库。

成本分层(对应"降低 AI 成本"):

1. 先用几乎零成本的规则特征(音量/关键词/语速/弹幕)算出 ``rule_score``;
2. 仅当 ``rule_score`` 超过初筛阈值,才花钱调用 LLM 复核;
3. 综合分超过房间阈值才写入候选池,并做区间去重。

边界处理(对应"避免切在奇怪位置"):用音频静音区间把"爆点±留白"的起止点
吸附到最近的自然停顿。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import select

from app.analysis import audio as audio_mod
from app.core.config import settings
from app.db.models import (
    HighlightCandidate,
    RawSegment,
    SegmentStatus,
)
from app.db.session import get_session


# --------------------------------------------------------------------------- #
# 纯函数:特征与几何(便于单测)
# --------------------------------------------------------------------------- #
def candidate_time_bounds(
    *,
    segment_start: datetime,
    available_start: datetime,
    available_end: datetime,
    peak_offset_s: float,
    pre_roll_s: float,
    post_roll_s: float,
    suggested_start_offset_s: float | None,
    suggested_end_offset_s: float | None,
    silences: list[tuple[float, float]],
    minimum_pre_roll_s: float | None = None,
    minimum_post_roll_s: float | None = None,
) -> tuple[datetime, datetime, datetime]:
    """计算可直接渲染的动态候选边界。

    ``pre_roll_s``/``post_roll_s`` 描述分析文本窗口；可选的
    ``minimum_*`` 则描述成片必须保留的最小上下文。未传最小值时保持旧行为，
    传入后允许 LLM 在完整分析窗口内给出更短或更长的自然事件边界，从而不再
    把每个候选固定为同一时长。静音吸附只向外扩展，最终边界限制在连续录像
    范围内。
    """
    normalized_available_start = _coerce_datetime_like(available_start, segment_start)
    normalized_available_end = _coerce_datetime_like(available_end, segment_start)
    required_pre_roll = pre_roll_s if minimum_pre_roll_s is None else max(0.0, minimum_pre_roll_s)
    required_post_roll = post_roll_s if minimum_post_roll_s is None else max(0.0, minimum_post_roll_s)
    required_start_offset = peak_offset_s - required_pre_roll
    requested_start_offset = suggested_start_offset_s if suggested_start_offset_s is not None else required_start_offset
    requested_start_offset = min(requested_start_offset, required_start_offset)
    start_offset = min(
        requested_start_offset,
        audio_mod.snap_to_silence(requested_start_offset, silences),
    )

    required_end_offset = peak_offset_s + required_post_roll
    requested_end_offset = suggested_end_offset_s if suggested_end_offset_s is not None else required_end_offset
    requested_end_offset = max(requested_end_offset, required_end_offset)
    end_offset = max(
        requested_end_offset,
        audio_mod.snap_to_silence(requested_end_offset, silences),
    )

    peak_ts = segment_start + timedelta(seconds=peak_offset_s)
    start_ts = max(normalized_available_start, segment_start + timedelta(seconds=start_offset))
    end_ts = min(normalized_available_end, segment_start + timedelta(seconds=end_offset))
    if end_ts <= start_ts:
        raise ValueError("候选边界在可用录像范围内没有有效时长。")
    return start_ts, end_ts, peak_ts


def contiguous_recording_start(
    segments: list[RawSegment],
    current_segment: RawSegment,
    *,
    gap_tolerance_s: float = 1.0,
) -> datetime:
    """返回当前片段向前连续可用的最早录像时间。

    前文只能跨越相邻或轻微重叠的片段；遇到断流缺口即停止，避免生成无法
    通过剪辑边界校验的候选。
    """
    if current_segment.start_ts is None:
        raise ValueError("当前片段缺少 start_ts，无法计算连续录像范围。")

    ordered = sorted(segments, key=lambda item: item.seq)
    current_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item is current_segment or (current_segment.id is not None and item.id == current_segment.id)
        ),
        None,
    )
    if current_index is None:
        return current_segment.start_ts

    contiguous_start = current_segment.start_ts
    for previous in reversed(ordered[:current_index]):
        if previous.start_ts is None or previous.end_ts is None:
            break
        previous_end = _coerce_datetime_like(previous.end_ts, contiguous_start)
        if (contiguous_start - previous_end).total_seconds() > gap_tolerance_s:
            break
        contiguous_start = _coerce_datetime_like(previous.start_ts, current_segment.start_ts)
    return contiguous_start


def contiguous_recording_range(
    segments: list[RawSegment],
    current_segment: RawSegment,
    *,
    gap_tolerance_s: float = 1.0,
) -> tuple[datetime, datetime]:
    """返回当前片段所在连续录像块的起止时间。

    与只向前查询的 :func:`contiguous_recording_start` 不同，本函数也会纳入
    已经落盘的后续分段，使分析滞后或会话收尾重分析时可以生成跨分段候选。

    :param segments: 同一录制会话的分段。
    :param current_segment: 当前评分分段。
    :param gap_tolerance_s: 允许的相邻分段时间缺口。
    :returns: 连续录像块的 ``(start_ts, end_ts)``。
    """
    if current_segment.start_ts is None or current_segment.end_ts is None:
        raise ValueError("当前片段缺少起止时间，无法计算连续录像范围。")
    ordered = sorted(segments, key=lambda item: item.seq)
    current_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item is current_segment or (current_segment.id is not None and item.id == current_segment.id)
        ),
        None,
    )
    if current_index is None:
        return current_segment.start_ts, current_segment.end_ts

    start = contiguous_recording_start(ordered, current_segment, gap_tolerance_s=gap_tolerance_s)
    end = current_segment.end_ts
    for following in ordered[current_index + 1 :]:
        if following.start_ts is None or following.end_ts is None:
            break
        following_start = _coerce_datetime_like(following.start_ts, end)
        if (following_start - end).total_seconds() > gap_tolerance_s:
            break
        end = _coerce_datetime_like(following.end_ts, current_segment.end_ts)
    return start, end


def _coerce_datetime_like(value: datetime, reference: datetime) -> datetime:
    """把 UTC 时间统一成与参考值相同的时区表示。"""
    if reference.tzinfo is None:
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).astimezone(reference.tzinfo)
    return value.astimezone(reference.tzinfo)


def speech_rate_score(words: list[dict], duration_s: float, window_s: float = 5.0) -> float:
    """根据词级时间戳估算"语速突增"得分。

    取最密集 ``window_s`` 窗口的词数,与整体平均词速比较;局部明显高于平均
    时(如激动连说)给高分。

    :param words: 词级时间戳列表,每项含 ``start`` 键。
    :param duration_s: 片段总时长(秒)。
    :param window_s: 滑窗宽度(秒)。
    :returns: 0-1 的语速突增分。
    """
    if not words or duration_s <= 0:
        return 0.0
    starts = sorted(float(w["start"]) for w in words if "start" in w)
    if len(starts) < 2:
        return 0.0

    avg_rate = len(starts) / duration_s  # 词/秒
    if avg_rate <= 0:
        return 0.0

    # 滑窗内最大词数 → 局部峰值词速。
    max_in_window = 0
    j = 0
    for i in range(len(starts)):
        while starts[i] - starts[j] > window_s:
            j += 1
        max_in_window = max(max_in_window, i - j + 1)
    peak_rate = max_in_window / window_s

    ratio = peak_rate / avg_rate  # >1 表示存在局部加速
    # ratio=1 -> 0 分;ratio>=3 -> 满分,中间线性。
    return float(min(max((ratio - 1.0) / 2.0, 0.0), 1.0))


def laughter_score(text: str) -> float:
    """从文本粗略估计"笑/惊呼"强度。

    统计"哈"等拟声字的出现,作为低成本的情绪代理(无需音频分类模型)。

    :param text: 转写文本。
    :returns: 0-1 的笑声分。
    """
    if not text:
        return 0.0
    count = text.count("哈") + text.count("笑") + text.count("草")
    return float(min(count / 5.0, 1.0))


def _audio_events_score(auxiliary_json: str | None) -> tuple[float, list[str]]:
    """V0.1.12.2: 从 SenseVoice 辅助特征计算音频事件评分。

    解析 auxiliary_json 中的 emotions/events, 生成:
    - laughter_density: 笑声密度
    - surprise_intensity: 惊讶强度
    - emotion_intensity: 情感突变强度
    - music_ratio: 音乐占比 (暂时不进入评分, 仅记录)

    :param auxiliary_json: Transcript.auxiliary_json 内容。
    :returns: ``(score 0-1, contributions)``。
    """
    if not auxiliary_json:
        return 0.0, []
    try:
        aux = json.loads(auxiliary_json)
    except (json.JSONDecodeError, TypeError):
        return 0.0, []

    emotions = aux.get("emotions", [])
    contributions: list[str] = []
    if not emotions:
        return 0.0, []

    laughter_count = sum(1 for e in emotions if isinstance(e, dict) and e.get("type", "") in ("laughter", "Laughter"))
    applause_count = sum(1 for e in emotions if isinstance(e, dict) and "applause" in e.get("type", "").lower())
    surprise_count = sum(1 for e in emotions if isinstance(e, dict) and "surprise" in e.get("type", "").lower())
    happy_count = sum(
        1
        for e in emotions
        if isinstance(e, dict) and ("happy" in e.get("type", "").lower() or "HAPPY" in e.get("type", ""))
    )
    anger_count = sum(
        1
        for e in emotions
        if isinstance(e, dict) and ("angry" in e.get("type", "").lower() or "anger" in e.get("type", "").lower())
    )

    score = 0.0
    if laughter_count > 0:
        score += min(laughter_count * 0.25, 0.5)
        contributions.append(f"laughter({laughter_count})")
    if applause_count > 0:
        score += min(applause_count * 0.2, 0.3)
        contributions.append(f"applause({applause_count})")
    if surprise_count > 0:
        score += min(surprise_count * 0.3, 0.5)
        contributions.append(f"surprise({surprise_count})")
    if happy_count > 0:
        score += min(happy_count * 0.2, 0.4)
        contributions.append(f"happy({happy_count})")
    if anger_count > 0:
        score += min(anger_count * 0.15, 0.3)
        contributions.append(f"anger({anger_count})")

    return min(score, 1.0), contributions


def danmaku_sentiment_score(session_id: int, start_ts: object, end_ts: object) -> float:
    """基于弹幕文本的情绪分析(规则:重复率、感叹号密度、特定梗命中)。

    完全不依赖 AI/ML,仅用启发式规则评估弹幕是否处于"炸裂"状态。
    典型高情绪信号:
    - 短时间内高度重复的弹幕(如满屏"???"或"666")
    - 高频感叹号密度
    - 特定高情绪梗的出现(卧槽、绝了、离谱、破防、高能等)

    :param session_id: 录制会话 id。
    :param start_ts: 窗口开始时间(datetime)。
    :param end_ts: 窗口结束时间(datetime)。
    :returns: 0-1 的弹幕情绪分。
    """
    import datetime as _dtmod

    def _naive(dt: object) -> _dtmod.datetime:
        if not isinstance(dt, _dtmod.datetime):
            return _dtmod.datetime.min.replace(tzinfo=None)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    if start_ts is None or end_ts is None:
        return 0.0

    start_n = _naive(start_ts)
    end_n = _naive(end_ts)

    # 直接按时间窗查询弹幕文本(SQL 级时间过滤,避免全表扫描)。
    window_texts: list[str] = []
    window_texts = _fetch_window_danmaku_texts(session_id, start_n, end_n)
    if len(window_texts) < 5:
        return 0.0

    # 1) 重复率:统计完全相同文本的出现率(归一化到 0-1)。
    text_counts: dict[str, int] = {}
    for t in window_texts:
        text_counts[t] = text_counts.get(t, 0) + 1
    max_dup = max(text_counts.values(), default=1)
    dup_rate = max_dup / max(len(window_texts), 1) if len(window_texts) > 0 else 0
    # 重复率 >= 10% 开始给分(如 20 条里 2 条相同不算), >= 40% 满分。
    dup_score = max(0.0, min((dup_rate - 0.1) / 0.3, 1.0))

    # 2) 感叹号密度:带"!"的弹幕占比。
    exclaim_count = sum(1 for t in window_texts if "!" in t or "！" in t)
    exclaim_rate = exclaim_count / len(window_texts)
    # >= 10% 开始给分, >= 50% 满分。
    exclaim_score = max(0.0, min((exclaim_rate - 0.1) / 0.4, 1.0))

    # 3) 高情绪梗:特定关键词的出现密度(V0.1.9 AC 加速)。
    hot_memes = (
        "卧槽",
        "绝了",
        "离谱",
        "破防",
        "高能",
        "泪目",
        "笑死",
        "什么?!",
        "无敌",
        "666",
        "??",
        "牛",
        "神",
        "厉害了",
        "这能忍?",
        "天秀",
        "牛逼",
    )
    meme_hits = _fast_meme_hit_count(window_texts, hot_memes)
    meme_rate = meme_hits / len(window_texts)
    # >= 5% 开始给分, >= 30% 满分。
    meme_score = max(0.0, min((meme_rate - 0.05) / 0.25, 1.0))

    # 加权合成:重复 0.4 + 感叹号 0.3 + 梗 0.3。
    return float(dup_score * 0.4 + exclaim_score * 0.3 + meme_score * 0.3)


def _fast_meme_hit_count(texts: list[str], memes: tuple[str, ...]) -> int:
    """使用 Aho-Corasick 加速统计梗词命中条数(V0.1.9)。

    :param texts: 弹幕文本列表。
    :param memes: 梗词元组。
    :returns: 命中条数。
    """
    from app.analysis.speedups import fast_meme_count

    return fast_meme_count(texts, memes)


def _fetch_window_danmaku_texts(session_id: int, start_n: object, end_n: object) -> list[str]:
    """获取指定时间窗口内的弹幕文本(SQL 级时间过滤,去时区)。

    :param session_id: 录制会话 id。
    :param start_n: 窗口开始(datetime,已去时区)。
    :param end_n: 窗口结束(datetime,已去时区)。
    :returns: 时间窗内的弹幕文本列表。
    """
    from app.db.models import Danmaku

    with get_session() as db:
        rows = db.exec(
            select(Danmaku.content, Danmaku.ts).where(
                Danmaku.session_id == session_id,
                Danmaku.msg_type == "danmaku",
                Danmaku.ts >= start_n,
                Danmaku.ts <= end_n,
            )
        ).all()
    texts: list[str] = []
    for content, _ts in rows:
        if content is not None:
            texts.append(content)
    return texts


def weighted_rule_score(features: dict[str, float], weights: dict[str, float]) -> float:
    """对各维度特征做加权求和(仅对出现的维度归一化权重)。

    :param features: 维度名 -> 0-1 分。
    :param weights: 维度名 -> 权重。
    :returns: 0-1 的规则综合分。
    """
    used = {k: weights.get(k, 0.0) for k in features}
    total_w = sum(used.values())
    if total_w <= 0:
        return 0.0
    return float(sum(features[k] * used[k] for k in features) / total_w)


def danmaku_rate_score(
    window_rate: float,
    baseline_rate: float,
    window_count: int = 0,
    min_samples: int = 10,
) -> float:
    """根据窗口弹幕速率与基线速率的比值,使用 Sigmoid 映射为 0-1 分数。

    设计原则:
    - 少量弹幕(低于 min_samples)不做过度放大,直接返回 0;
    - 基线为 0 时,若有足够样本则给中等置信分;
    - 使用平滑 Sigmoid 替代线性映射,避免极端比值主导评分。

    :param window_rate: 当前窗口的弹幕速率(条/秒)。
    :param baseline_rate: 基线弹幕速率(条/秒,来自中位数分桶)。
    :param window_count: 当前窗口弹幕总条数(用于最小样本量保护)。
    :param min_samples: 最低弹幕条数阈值,低于此值视为噪声。
    :returns: 0-1 的弹幕热度分。
    """
    import math

    if window_count < min_samples or window_rate <= 0:
        return 0.0

    # 基线为 0 但有足够弹幕:可能是第一波弹幕,给中等分。
    if baseline_rate <= 0:
        return 0.35

    ratio = window_rate / baseline_rate

    # Sigmoid 映射:ratio=1→0.05, ratio=2→0.35, ratio=3→0.73, ratio=5→0.95, ratio=10→~1.0
    # 公式: 1 / (1 + exp(-(ratio - 1.8) * 1.6))
    # 无量纲转换,ratio 越大越接近 1,但增速递减(避免单一极端值主导)。
    score = 1.0 / (1.0 + math.exp(-(ratio - 1.8) * 1.6))
    return float(round(score, 4))


def temporal_iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    """计算两个时间区间的 IoU(交并比)。

    :param a: 区间 A ``(start, end)``(秒)。
    :param b: 区间 B ``(start, end)``(秒)。
    :returns: 0-1 的 IoU。
    """
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return float(inter / union) if union > 0 else 0.0


def fuse_scores(rule: float, llm_score: float | None, alpha: float, beta: float) -> float:
    """融合规则分与 LLM 分。

    无 LLM 分时直接返回规则分,避免被 ``beta*0`` 拉低。

    :param rule: 规则分(0-1)。
    :param llm_score: LLM 分(0-1)或 ``None``。
    :param alpha: 规则分系数。
    :param beta: LLM 分系数。
    :returns: 0-1 的综合分。
    """
    if llm_score is None:
        return rule
    denom = alpha + beta
    if denom <= 0:
        return rule
    return float((alpha * rule + beta * llm_score) / denom)


# --------------------------------------------------------------------------- #
# 主流程:对一个片段评分并(可能)生成候选
# --------------------------------------------------------------------------- #
def score_segment(segment_id: int) -> HighlightCandidate | None:
    """对已转写片段做高光评分,达阈值则写入候选池。

    :param segment_id: ``raw_segments`` 主键。
    :returns: 新建的 :class:`HighlightCandidate`;未达阈值或重复时返回 ``None``。
    :raises ValueError: 片段不存在,或尚未转写时。
    """
    from app.pipeline.workers.analyze import score_segment_direct

    return score_segment_direct(segment_id)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _audio_meta(feats: audio_mod.AudioFeatures) -> dict:
    """提取用于落库的精简音频元信息。

    :param feats: 音频特征。
    :returns: 可 JSON 序列化的精简字典。
    """
    meta = asdict(feats)
    # numpy 数组不便落库,去除原始包络,仅保留摘要。
    meta.pop("times", None)
    meta.pop("rms", None)
    meta["peak_offset"] = feats.peak_offset()
    meta["n_silences"] = len(feats.silences)
    meta["silences"] = None
    return meta


def _is_duplicate(
    session_id: int,
    interval: tuple[float, float],
    iou_threshold: float,
    *,
    peak_ts: datetime | None = None,
    cooldown_s: float = 0.0,
) -> bool:
    """判断新候选区间是否与同会话既有候选高度重叠。

    :param session_id: 会话 id。
    :param interval: 新候选的 ``(start_epoch, end_epoch)`` 秒。
    :param iou_threshold: 判重的 IoU 阈值。
    :param peak_ts: 新候选峰值；提供时同时执行冷却时间判重。
    :param cooldown_s: 峰值冷却时间。
    :returns: 重复或落入冷却簇返回 ``True``。
    """
    from app.analysis.timeline import datetime_distance_s

    with get_session() as db:
        rows = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
    for c in rows:
        existing = (c.start_ts.timestamp(), c.end_ts.timestamp())
        if temporal_iou(interval, existing) >= iou_threshold:
            return True
        if peak_ts is not None and cooldown_s > 0:
            if datetime_distance_s(c.peak_ts, peak_ts) < cooldown_s:
                return True
    return False


def _trend_score(text: str) -> tuple[float, list[str]]:
    """计算片段文本与网感资料库近期热门内容的关联度。

    采集/查询失败不应影响评分主流程,异常时返回 0。

    :param text: 片段转写文本。
    :returns: ``(score, matched_terms)``。
    """
    try:
        from app.trends import store as trend_store

        return trend_store.match_text(text, days=settings.trend_match_days)
    except Exception as exc:  # noqa: BLE001 — 资料库异常不应中断评分
        logger.warning("网感关联度计算失败: {}", exc)
        return 0.0, []


# ---- 弹幕热度评分(P0 重构) ----
_DANMAKU_BUCKET_S = 10  # 基线计算的分桶粒度(秒)
_DANMAKU_BASELINE_MINUTES = 20  # 基线窗口:候选前 N 分钟(不足则用全场历史)
_DANMAKU_MIN_SAMPLES = 10  # 最低弹幕样本量,低于此值视为噪声(0 分)
# 中心加权窗口:越靠近候选中心时刻的弹幕权重越高(分段线性)。
_DANMAKU_CENTER_WEIGHT_WINDOW = 30.0  # 中心加权半径(秒)
_DANMAKU_CENTER_WEIGHT_PEAK = 3.0  # 中心权重峰值倍数


def _danmaku_baseline(
    session_id: int,
    before_end: object,
    window_start: object,
    window_end: object,
) -> tuple[float, int]:
    """计算弹幕基线速率(条/秒)。

    使用候选窗口前 _DANMAKU_BASELINE_MINUTES 分钟的数据,按 _DANMAKU_BUCKET_S
    秒分桶后取中位数速率;样本不足时扩大至排除当前窗口的整场历史。

    :param session_id: 录制会话 id。
    :param before_end: 基线的结束时间(当前窗口起点,不含窗口内弹幕)。
    :param window_start: 候选窗口起点(用于排除)。
    :param window_end: 候选窗口终点(用于排除)。
    :returns: ``(baseline_rate, total_baseline_count)``。
    """
    from datetime import datetime as _datetime
    from datetime import timedelta

    from app.db.models import Danmaku

    def _n(dt: _datetime) -> _datetime:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    before_n = _n(before_end)  # type: ignore[arg-type]
    baseline_start = before_n - timedelta(minutes=_DANMAKU_BASELINE_MINUTES)  # type: ignore[operator]

    with get_session() as db:
        rows = db.exec(
            select(Danmaku.ts).where(
                Danmaku.session_id == session_id,
                Danmaku.ts >= baseline_start,
                Danmaku.ts < before_n,
                Danmaku.msg_type == "danmaku",
            )
        ).all()

    if len(rows) < _DANMAKU_MIN_SAMPLES:
        # 扩大:取整场弹幕(排除当前窗口)。
        with get_session() as db:
            all_rows = db.exec(
                select(Danmaku.ts).where(
                    Danmaku.session_id == session_id,
                    Danmaku.msg_type == "danmaku",
                )
            ).all()
        # 排除落在窗口内的弹幕。
        w_start_n = _n(window_start) if window_start else None  # type: ignore[arg-type]
        w_end_n = _n(window_end) if window_end else None  # type: ignore[arg-type]
        filtered: list[_datetime] = []
        for ts in all_rows:
            t = _n(ts)  # type: ignore[arg-type]
            if w_start_n is not None and w_end_n is not None and w_start_n <= t <= w_end_n:  # type: ignore[operator]
                continue
            filtered.append(t)
        rows = filtered

    if not rows or len(rows) < _DANMAKU_MIN_SAMPLES:
        return 0.0, 0

    # V0.1.10: 使用加速版分桶+中位数 (排序→float 秒→分桶→速率→中位数)。
    from app.analysis.speedups import danmaku_baseline_rate

    timestamps_sorted = sorted(_n(ts) for ts in rows)  # type: ignore[arg-type]
    base_ts = timestamps_sorted[0]
    times_sorted = [(ts - base_ts).total_seconds() for ts in timestamps_sorted]
    return danmaku_baseline_rate(times_sorted, _DANMAKU_BUCKET_S)


def _danmaku_score(session_id: int, start_ts: object, end_ts: object) -> float:
    """查询会话弹幕并计算给定时间窗的弹幕热度分(P0 重构版)。

    - 当前窗口速率:统计 start_ts~end_ts 内弹幕,靠近中心时刻加权。
    - 基线速率:使用窗口前 20 分钟数据按 10 秒分桶取中位数。
    - 最终分:通过 Sigmoid 函数将窗口/基线比值映射为 0-1。

    :param session_id: 录制会话 id。
    :param start_ts: 窗口开始时间(datetime)。
    :param end_ts: 窗口结束时间(datetime)。
    :returns: 0-1 的弹幕热度分;无足够弹幕数据时返回 0。
    """
    from datetime import datetime as _datetime

    from app.db.models import Danmaku

    if start_ts is None or end_ts is None:
        return 0.0

    def _n(dt: _datetime) -> _datetime:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    start_n = _n(start_ts)  # type: ignore[arg-type]
    end_n = _n(end_ts)  # type: ignore[arg-type]

    # 1) 窗口弹幕:带中心加权。
    with get_session() as db:
        window_rows = db.exec(
            select(Danmaku.ts, Danmaku.value).where(
                Danmaku.session_id == session_id,
                Danmaku.ts >= start_n,
                Danmaku.ts <= end_n,
                Danmaku.msg_type == "danmaku",
            )
        ).all()

    if not window_rows or len(window_rows) < _DANMAKU_MIN_SAMPLES:
        return 0.0

    center = start_n + (end_n - start_n) / 2  # type: ignore[operator]
    window_seconds = (end_n - start_n).total_seconds()  # type: ignore[operator]
    if window_seconds <= 0:
        return 0.0

    # 中心加权:距 center 越近权重越高(分段线性,最大 _DANMAKU_CENTER_WEIGHT_PEAK 倍)。
    weighted_count = 0.0
    for ts_dt, value in window_rows:
        t = _n(ts_dt)  # type: ignore[arg-type]
        dist = abs((t - center).total_seconds())  # type: ignore[operator]
        if dist <= _DANMAKU_CENTER_WEIGHT_WINDOW:
            w = 1.0 + (_DANMAKU_CENTER_WEIGHT_PEAK - 1.0) * (1.0 - dist / _DANMAKU_CENTER_WEIGHT_WINDOW)
        else:
            w = 1.0
        weighted_count += float(value) * w

    window_rate = weighted_count / window_seconds

    # 2) 基线速率(排除当前窗口)。
    baseline_rate, baseline_count = _danmaku_baseline(
        session_id,
        start_ts,
        start_ts,
        end_ts,
    )

    # 3) 最终评分。
    score = danmaku_rate_score(
        window_rate=window_rate,
        baseline_rate=baseline_rate,
        window_count=len(window_rows),
        min_samples=_DANMAKU_MIN_SAMPLES,
    )
    return score


def danmaku_score_explain(session_id: int, start_ts: object, end_ts: object) -> dict:
    """返回弹幕评分的可解释数据,供审核页面展示。

    :returns: 包含 ``window_count``、``window_rate``、``baseline_rate``、
        ``ratio``、``score`` 等字段的字典。
    """
    from datetime import datetime as _datetime

    from app.db.models import Danmaku

    if start_ts is None or end_ts is None:
        return {"window_count": 0, "window_rate": 0.0, "baseline_rate": 0.0, "ratio": 0.0, "score": 0.0}

    def _n(dt: _datetime) -> _datetime:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    start_n = _n(start_ts)  # type: ignore[arg-type]
    end_n = _n(end_ts)  # type: ignore[arg-type]

    with get_session() as db:
        window_rows = db.exec(
            select(Danmaku.ts).where(
                Danmaku.session_id == session_id,
                Danmaku.ts >= start_n,
                Danmaku.ts <= end_n,
                Danmaku.msg_type == "danmaku",
            )
        ).all()

    window_count = len(window_rows)
    window_seconds = (end_n - start_n).total_seconds()  # type: ignore[operator]
    window_rate = window_count / max(window_seconds, 1)

    baseline_rate, baseline_count = _danmaku_baseline(
        session_id,
        start_ts,
        start_ts,
        end_ts,
    )

    score = _danmaku_score(session_id, start_ts, end_ts)
    ratio = (window_rate / baseline_rate) if baseline_rate > 0 else float("inf")

    return {
        "window_danmaku_count": window_count,
        "window_rate_ps": round(window_rate, 2),
        "baseline_rate_ps": round(baseline_rate, 2),
        "baseline_count": baseline_count,
        "ratio": f"{ratio:.1f}x" if ratio != float("inf") else "N/A(基线为0)",
        "score": round(score, 4),
    }


def _mark_scored(segment_id: int) -> None:
    """把片段标记为已评分。

    :param segment_id: 片段 id。
    """
    with get_session() as db:
        seg = db.get(RawSegment, segment_id)
        if seg is not None and seg.status != SegmentStatus.SCORED:
            seg.status = SegmentStatus.SCORED
            db.add(seg)
