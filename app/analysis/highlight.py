"""高光判断:多维规则打分 + 可选 LLM 复核 + ML 模型(可选) + 边界吸附 + 查重 + 候选入库。

成本分层(对应"降低 AI 成本"):

1. 先用几乎零成本的规则特征(音量/关键词/语速/弹幕)算出 ``rule_score``;
2. 仅当 ``rule_score`` 超过初筛阈值,才花钱调用 LLM 复核;
3. 综合分超过房间阈值才写入候选池,并做区间去重。

V0.1.9: 新增 ML 高光模型支持。当房间启用 ``ml_highlight_enabled`` 且模型文件存在时,
优先使用本地 XGBoost 模型预测替代规则+LLM 管线。Shadow 模式下同时跑规则+ML 双轨并记录对比。

边界处理(对应"避免切在奇怪位置"):用音频静音区间把"爆点±留白"的起止点
吸附到最近的自然停顿。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

from loguru import logger
from sqlmodel import select

from app.analysis import audio as audio_mod
from app.analysis import llm as llm_mod
from app.analysis.keywords import match_keywords
from app.analysis.scoring_config import get_scoring_config
from app.core.config import settings
from app.db.models import (
    CandidateStatus,
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    Transcript,
)
from app.db.session import get_session


# --------------------------------------------------------------------------- #
# 纯函数:特征与几何(便于单测)
# --------------------------------------------------------------------------- #
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
    from app.db.models import Danmaku

    def _naive(dt: object) -> object:
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

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

    # 3) 高情绪梗:特定关键词的出现密度。
    hot_memes = {"卧槽", "绝了", "离谱", "破防", "高能", "泪目", "笑死", "什么?!", "无敌",
                 "666", "??", "牛", "神", "厉害了", "这能忍?", "天秀", "牛逼"}
    meme_hits = sum(
        1 for t in window_texts if any(meme in t for meme in hot_memes)
    )
    meme_rate = meme_hits / len(window_texts)
    # >= 5% 开始给分, >= 30% 满分。
    meme_score = max(0.0, min((meme_rate - 0.05) / 0.25, 1.0))

    # 加权合成:重复 0.4 + 感叹号 0.3 + 梗 0.3。
    return float(dup_score * 0.4 + exclaim_score * 0.3 + meme_score * 0.3)


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
    for content, ts in rows:
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
    cfg = get_scoring_config()

    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        transcript = db.exec(
            select(Transcript).where(Transcript.segment_id == segment_id)
        ).first()
        session = db.get(RecordingSession, segment.session_id)
        room = db.get(LiveRoom, session.room_id) if session else None
        # 取出需要的标量,避免会话关闭后再访问 ORM 对象(DetachedInstanceError)。
        file_path = segment.file_path
        seg_start_ts = segment.start_ts
        seg_end_ts = segment.end_ts
        if seg_start_ts is None or seg_end_ts is None:
            logger.error("片段 {} 缺少时间戳,无法评分", segment_id)
            return None
        duration = segment.duration_s or float(settings.segment_duration_s)
        session_id = segment.session_id
        threshold = room.highlight_threshold if room else settings.highlight_threshold
        has_transcript = transcript is not None
        text = transcript.text if transcript else ""
        words_json = transcript.words_json if transcript else None

    if not has_transcript:
        raise ValueError(f"片段尚未转写: id={segment_id}")

    words = json.loads(words_json) if words_json else []

    # ---- V0.1.9: ML 高光模型 (可选) ----
    use_ml = (room is not None and bool(room.ml_highlight_enabled)
              and _ml_model_available())
    ml_shadow = use_ml and settings.ml_shadow_mode
    ml_score: float | None = None
    ml_used_for_decision = False

    if use_ml:
        try:
            ml_score = _ml_predict(segment_id)
            if ml_shadow:
                logger.info("Shadow模式: segment={} ML={:.3f} (规则管线继续运行以供对比)",
                            segment_id, ml_score)
            else:
                logger.info("ML模型预测: segment={} score={:.3f}", segment_id, ml_score)
        except Exception as exc:
            logger.warning("ML预测失败,回退规则管线: {}", exc)

    # 非 Shadow 模式下 ML 可用 → 直接用 ML 结果替代规则+LLM
    if use_ml and not ml_shadow and ml_score is not None:
        highlight_score = float(ml_score)
        reason = f"ML模型预测 (score={highlight_score:.3f})"
        rule_score = 0.0
        llm_score = 0.0
        ml_used_for_decision = True
        feats = audio_mod.analyze_audio(file_path)  # 仍需音频特征用于边界吸附
        kw_hits: list[str] = []
        logger.info("片段 {} 使用ML模型直接判定 score={:.3f}", segment_id, highlight_score)
        # 跳到阈值判断
        if highlight_score < threshold:
            _mark_scored(segment_id)
            return None
        # 跳到候选入库
        peak_off = feats.peak_offset()
        start_off = peak_off - cfg.pre_roll_s
        end_off = peak_off + cfg.post_roll_s
        start_off = audio_mod.snap_to_silence(start_off, feats.silences)
        end_off = audio_mod.snap_to_silence(end_off, feats.silences)
        peak_ts = seg_start_ts + timedelta(seconds=peak_off)
        start_ts = seg_start_ts + timedelta(seconds=start_off)
        end_ts = seg_start_ts + timedelta(seconds=end_off)
        if _is_duplicate(session_id, (start_ts.timestamp(), end_ts.timestamp()), cfg.iou_threshold):
            _mark_scored(segment_id)
            return None
        _ml_create_candidate(segment_id, session_id, highlight_score, reason, peak_ts, start_ts, end_ts,
                             threshold, room)
        return

    # ---- 1) 规则特征 ----
    feats = audio_mod.analyze_audio(file_path)
    kw_score, kw_hits = match_keywords(text)
    features: dict[str, float] = {
        "volume": feats.volume_score(),
        "keywords": kw_score,
        "speech_rate": speech_rate_score(words, duration),
        "laughter": laughter_score(text),
        # 弹幕热度:本片段时间窗内弹幕强度相对全场平均的倍数(无弹幕数据则为 0)。
        "danmaku": _danmaku_score(session_id, seg_start_ts, seg_end_ts),
    }
    # 弹幕情绪(V0.1.2 新增):仅当房间级开关启用且弹幕采集开启时才计入。
    use_dm_sentiment = (
        room is not None
        and bool(room.danmaku_sentiment_enabled)
        and settings.collect_danmaku
    )
    if use_dm_sentiment:
        features["danmaku_sentiment"] = danmaku_sentiment_score(
            session_id, seg_start_ts, seg_end_ts
        )
    # 网感维度:片段题材与资料库近期热门内容的关联度(仅在启用时计入)。
    trend_hits: list[str] = []
    if settings.trend_enabled:
        trend_score, trend_hits = _trend_score(text)
        features["trend"] = trend_score
    rule_score = weighted_rule_score(features, cfg.weights)
    logger.info(
        "片段 {} 规则分={:.3f} 特征={} 命中词={} 网感词={}",
        segment_id,
        rule_score,
        {k: round(v, 3) for k, v in features.items()},
        kw_hits,
        trend_hits,
    )

    # ---- 2) 初筛:不够分就不调 LLM(省钱) ----
    if rule_score < settings.highlight_init_threshold:
        _mark_scored(segment_id)
        logger.debug("片段 {} 低于初筛阈值,跳过 LLM。", segment_id)
        return None

    # ---- 3) LLM 复核(可选) ----
    judgement = llm_mod.judge_highlight(text, features)
    llm_score = judgement.score if judgement else None
    reason = judgement.reason if judgement else "规则命中(未启用/未触发 LLM)"

    highlight_score = fuse_scores(rule_score, llm_score, cfg.alpha, cfg.beta)
    logger.info(
        "片段 {} 综合分={:.3f}(rule={:.3f} llm={}) 阈值={:.2f}",
        segment_id,
        highlight_score,
        rule_score,
        f"{llm_score:.3f}" if llm_score is not None else "N/A",
        threshold,
    )

    if highlight_score < threshold:
        _mark_scored(segment_id)
        return None

    # ---- 4) 边界吸附:爆点±留白,并对齐到最近静音 ----
    peak_off = feats.peak_offset()
    if judgement and judgement.suggested_start_offset is not None:
        start_off = judgement.suggested_start_offset
    else:
        start_off = peak_off - cfg.pre_roll_s
    if judgement and judgement.suggested_end_offset is not None:
        end_off = judgement.suggested_end_offset
    else:
        end_off = peak_off + cfg.post_roll_s

    start_off = audio_mod.snap_to_silence(start_off, feats.silences)
    end_off = audio_mod.snap_to_silence(end_off, feats.silences)

    # 起止偏移可超出本片段(向前/向后留白),切片阶段会跨片段拼接。
    peak_ts = seg_start_ts + timedelta(seconds=peak_off)
    start_ts = seg_start_ts + timedelta(seconds=start_off)
    end_ts = seg_start_ts + timedelta(seconds=end_off)

    # ---- 5) 去重:与本会话既有候选做时间 IoU 比较 ----
    if _is_duplicate(session_id, (start_ts.timestamp(), end_ts.timestamp()), cfg.iou_threshold):
        _mark_scored(segment_id)
        logger.info("片段 {} 候选与既有候选重叠,跳过。", segment_id)
        return None

    # ---- 5b) V0.1.6 审核状态:根据房间阈值自动决定初始状态 ----
    # P0 重构:取代旧 mode 逻辑。
    auto_approve_threshold = room.auto_approve_threshold if room else 0.82
    review_threshold = room.review_threshold if room else 0.50
    room_auto_approve = bool(room.auto_approve) if room else False

    if room_auto_approve and highlight_score >= auto_approve_threshold:
        initial_status = CandidateStatus.APPROVED
        logger.info("片段 {} 达自动批准阈值({}≥{}),自动批准。", segment_id, highlight_score, auto_approve_threshold)
    elif highlight_score >= review_threshold:
        initial_status = CandidateStatus.PENDING
    else:
        initial_status = CandidateStatus.REJECTED
        logger.info("片段 {} 低于审核阈值({}<{}),自动淘汰。", segment_id, highlight_score, review_threshold)

    # 自动淘汰的候选仍然入库(供后续调参参考),但标记为 REJECTED。
    dedup_hash = hashlib.sha1(
        f"{session_id}:{round(start_ts.timestamp())}:{round(end_ts.timestamp())}".encode()
    ).hexdigest()

    # 弹幕可解释数据(P0):供审核页展示。
    danmaku_explain = danmaku_score_explain(session_id, seg_start_ts, seg_end_ts)

    candidate = HighlightCandidate(
        session_id=session_id,
        peak_ts=peak_ts,
        start_ts=start_ts,
        end_ts=end_ts,
        rule_score=rule_score,
        llm_score=llm_score or 0.0,
        highlight_score=highlight_score,
        features_json=json.dumps(
            {
                "features": features,
                "keyword_hits": kw_hits,
                "audio": _audio_meta(feats),
                "danmaku_explain": danmaku_explain,
            },
            ensure_ascii=False,
        ),
        reason=reason,
        status=initial_status,
        dedup_hash=dedup_hash,
    )
    with get_session() as db:
        db.add(candidate)
        db.flush()
        db.refresh(candidate)
        cid = candidate.id

    _mark_scored(segment_id)
    logger.success(
        "★ 新高光候选 id={} segment={} 分数={:.3f} 时长={:.0f}s 理由={}",
        cid,
        segment_id,
        highlight_score,
        end_off - start_off,
        reason,
    )
    return candidate


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
) -> bool:
    """判断新候选区间是否与同会话既有候选高度重叠。

    :param session_id: 会话 id。
    :param interval: 新候选的 ``(start_epoch, end_epoch)`` 秒。
    :param iou_threshold: 判重的 IoU 阈值。
    :returns: 重复返回 ``True``。
    """
    with get_session() as db:
        rows = db.exec(
            select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)
        ).all()
    for c in rows:
        existing = (c.start_ts.timestamp(), c.end_ts.timestamp())
        if temporal_iou(interval, existing) >= iou_threshold:
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
_DANMAKU_BUCKET_S = 10          # 基线计算的分桶粒度(秒)
_DANMAKU_BASELINE_MINUTES = 20  # 基线窗口:候选前 N 分钟(不足则用全场历史)
_DANMAKU_MIN_SAMPLES = 10       # 最低弹幕样本量,低于此值视为噪声(0 分)
# 中心加权窗口:越靠近候选中心时刻的弹幕权重越高(分段线性)。
_DANMAKU_CENTER_WEIGHT_WINDOW = 30.0  # 中心加权半径(秒)
_DANMAKU_CENTER_WEIGHT_PEAK = 3.0     # 中心权重峰值倍数


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
    from datetime import datetime as _datetime, timedelta
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
        for (ts,) in all_rows:
            t = _n(ts)  # type: ignore[arg-type]
            if w_start_n is not None and w_end_n is not None and w_start_n <= t <= w_end_n:  # type: ignore[operator]
                continue
            filtered.append(t)
        rows = [(t,) for t in filtered]

    if not rows or len(rows) < _DANMAKU_MIN_SAMPLES:
        return 0.0, 0

    # 按 _DANMAKU_BUCKET_S 秒分桶,计算每桶速率。
    times_sorted = sorted(_n(r[0]) for r in rows)  # type: ignore[arg-type]
    t0 = times_sorted[0]
    buckets: dict[int, int] = {}
    for t in times_sorted:
        idx = int((t - t0).total_seconds() / _DANMAKU_BUCKET_S)  # type: ignore[operator]
        buckets[idx] = buckets.get(idx, 0) + 1

    rates = [v / _DANMAKU_BUCKET_S for v in buckets.values()]

    # 中位数基线
    rates.sort()
    n = len(rates)
    if n == 0:
        return 0.0, 0
    median = rates[n // 2] if n % 2 == 1 else (rates[n // 2 - 1] + rates[n // 2]) / 2
    return float(median), len(rows)


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
        session_id, start_ts, start_ts, end_ts,
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
        session_id, start_ts, start_ts, end_ts,
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


# --------------------------------------------------------------------------- #
# V0.1.9: ML 高光模型集成
# --------------------------------------------------------------------------- #
_ml_inference = None  # 模块级懒加载单例


def _ml_model_available() -> bool:
    """检查 ML 模型文件是否存在。"""
    from pathlib import Path as _Path
    return _Path("storage/models/highlight_model.pkl").exists()


def _ml_predict(segment_id: int) -> float:
    """调用 ML 模型预测高光概率。"""
    global _ml_inference
    if _ml_inference is None:
        from Highlight_Model.models.inference import ModelInference
        _ml_inference = ModelInference(threshold=0.5)
        _ml_inference.load()
    return _ml_inference.predict_proba(segment_id)


def _ml_create_candidate(segment_id: int, session_id: int, highlight_score: float,
                         reason: str, peak_ts, start_ts, end_ts,
                         threshold: float, room) -> None:
    """用 ML 模型评分结果创建高光候选（规则管线复用此逻辑）。"""
    from app.db.models import CandidateStatus, HighlightCandidate

    auto_approve_threshold = room.auto_approve_threshold if room else 0.82
    review_threshold = room.review_threshold if room else 0.50
    room_auto_approve = bool(room.auto_approve) if room else False

    if room_auto_approve and highlight_score >= auto_approve_threshold:
        initial_status = CandidateStatus.APPROVED
    elif highlight_score >= review_threshold:
        initial_status = CandidateStatus.PENDING
    else:
        initial_status = CandidateStatus.REJECTED

    dedup_hash = hashlib.sha1(
        f"{session_id}:{round(start_ts.timestamp())}:{round(end_ts.timestamp())}".encode()
    ).hexdigest()

    candidate = HighlightCandidate(
        session_id=session_id, peak_ts=peak_ts, start_ts=start_ts, end_ts=end_ts,
        rule_score=0.0, llm_score=0.0, highlight_score=highlight_score,
        features_json=json.dumps({"source": "ml_model", "score": highlight_score}, ensure_ascii=False),
        reason=reason, status=initial_status, dedup_hash=dedup_hash,
    )
    with get_session() as db:
        db.add(candidate)
        db.flush()
        db.refresh(candidate)
        cid = candidate.id

    _mark_scored(segment_id)
    logger.success("★ ML高光候选 id={} segment={} score={:.3f} status={}", cid, segment_id, highlight_score, initial_status)
