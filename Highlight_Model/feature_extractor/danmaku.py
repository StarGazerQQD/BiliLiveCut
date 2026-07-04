"""弹幕交互特征提取器 (D1-D13)。

复用了母仓库 app.analysis.highlight 的弹幕评分函数（_danmaku_score、
danmaku_sentiment_score、_damaku_baseline 等），在此基础上新增
加速度、爆发计数、文本熵、去重人数、跨模态时差等维度。

国产模型策略：纯规则 + SQL 查询，无外部模型依赖。
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_DANMAKU_NAMES = [
    "dm_window_count", "dm_window_rate", "dm_baseline_rate",
    "dm_rate_ratio", "dm_rate_acceleration",
    "dm_center_weighted_rate", "dm_burst_count",
    "dm_text_entropy", "dm_exclaim_ratio", "dm_meme_hit_ratio",
    "dm_high_value_ratio", "dm_viewer_unique", "dm_lead_lag_ms",
]


class DanmakuExtractor(BaseFeatureExtractor):
    """弹幕交互特征提取器 — 13 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_DANMAKU_NAMES)

    @property
    def n_features(self) -> int:
        return 13

    def extract(self, segment_id: int) -> np.ndarray:
        feats = np.zeros(self.n_features, dtype=np.float32)

        # 获取片段时间窗口
        seg_start, seg_end, session_id = _get_segment_window(segment_id)
        if seg_start is None or seg_end is None or session_id is None:
            return feats

        window_s = max((seg_end - seg_start).total_seconds(), 1)

        # 1) 窗口弹幕
        window_dms = _fetch_danmaku(session_id, seg_start, seg_end)
        window_count = len(window_dms)
        if window_count < 5:
            return feats  # 样本不足全填 0
        feats[0] = float(window_count)           # D1 dm_window_count
        feats[1] = window_count / window_s        # D2 dm_window_rate

        # 2) 基线速率（复用母仓库函数）
        baseline_rate = _call_baseline(session_id, seg_start, seg_end)
        feats[2] = baseline_rate                  # D3 dm_baseline_rate
        feats[3] = feats[1] / (baseline_rate + 1e-8)  # D4 dm_rate_ratio

        # 3) 加速度
        before_start = seg_start - (seg_end - seg_start)
        before_dms = _fetch_danmaku(session_id, before_start, seg_start)
        before_rate = len(before_dms) / max((seg_end - seg_start).total_seconds(), 1)
        feats[4] = (feats[1] - before_rate) / (before_rate + 1e-8)  # D5

        # 4) 中心加权速率
        center = seg_start + (seg_end - seg_start) / 2
        w_count = 0.0
        for ts, val in window_dms:
            dist = abs((ts - center).total_seconds())
            w = 3.0 if dist < 15 else 1.0
            w_count += val * w
        feats[5] = w_count / window_s             # D6

        # 5) 爆发次数 (2s 窗)
        feats[6] = float(_count_bursts(window_dms, window_s))  # D7

        # 6) 文本熵 / 感叹号 / 梗
        texts = [t for t, _ in window_dms]
        feats[7] = _text_entropy(texts)           # D8
        feats[8] = sum(1 for t in texts if "!" in t or "！" in t) / len(texts)  # D9
        memes = {"卧槽", "绝了", "离谱", "破防", "高能", "泪目", "笑死", "666"}
        feats[9] = sum(1 for t in texts if any(m in t for m in memes)) / len(texts)  # D10

        # 7) 高价值弹幕比
        feats[10] = sum(v for _, v in window_dms if v > 100) / max(sum(v for _, v in window_dms), 1)  # D11

        # 8) 去重人数
        unique_ids = _fetch_unique_uids(session_id, seg_start, seg_end)
        feats[11] = float(unique_ids)             # D12

        # 9) 弹幕-音频时差
        feats[12] = _dm_lead_lag(session_id, seg_start, seg_end, segment_id)  # D13

        return feats


# ------------------------------------------------------------------ #
def _get_segment_window(segment_id: int) -> tuple[datetime | None, datetime | None, int | None]:
    try:
        from app.db.models import RawSegment
        from app.db.session import get_session
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return (None, None, None)
            return (seg.start_ts, seg.end_ts, seg.session_id)
    except Exception:
        return (None, None, None)


def _fetch_danmaku(session_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    try:
        from app.db.models import Danmaku
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            rows = db.exec(
                select(Danmaku.ts, Danmaku.value).where(
                    Danmaku.session_id == session_id,
                    Danmaku.ts >= start.replace(tzinfo=None),
                    Danmaku.ts <= end.replace(tzinfo=None),
                )
            ).all()
        return [(ts, float(val or 1)) for ts, val in rows]
    except Exception:
        return []


def _call_baseline(session_id: int, start: datetime, end: datetime) -> float:
    try:
        from app.analysis.highlight import _danmaku_baseline
        rate, _ = _danmaku_baseline(session_id, start, start, end)
        return float(rate)
    except Exception:
        return 0.0


def _count_bursts(dms: list, window_s: float, burst_win: float = 2.0) -> int:
    """统计 2 秒短窗内的爆发次数（密度 > 3条/2s）。"""
    if not dms:
        return 0
    times_sorted = sorted(ts.timestamp() for ts, _ in dms)
    bursts = 0
    j = 0
    for i in range(len(times_sorted)):
        while times_sorted[i] - times_sorted[j] > burst_win:
            j += 1
        if i - j + 1 >= 3:
            bursts += 1
            j = i + 1  # 跳过已计数窗口
    return bursts


def _text_entropy(texts: list[str]) -> float:
    if not texts:
        return 0.0
    counter = Counter(texts)
    total = len(texts)
    ent = 0.0
    for cnt in counter.values():
        p = cnt / total
        ent -= p * math.log(p + 1e-12)
    return float(ent / max(math.log(total), 1))


def _fetch_unique_uids(session_id: int, start: datetime, end: datetime) -> int:
    try:
        from app.db.models import Danmaku
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            rows = db.exec(
                select(Danmaku.uid).where(
                    Danmaku.session_id == session_id,
                    Danmaku.ts >= start.replace(tzinfo=None),
                    Danmaku.ts <= end.replace(tzinfo=None),
                    Danmaku.uid.isnot(None),
                )
            ).all()
        return len({uid for (uid,) in rows if uid})
    except Exception:
        return 0


def _dm_lead_lag(session_id: int, seg_start: datetime, seg_end: datetime,
                 segment_id: int) -> float:
    """计算弹幕爆发中心与音频爆点的时间差（毫秒），正=弹幕滞后。"""
    try:
        # 音频爆点
        from app.analysis.audio import analyze_audio
        from app.db.models import RawSegment
        from app.db.session import get_session
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return 0.0
            feats = analyze_audio(seg.file_path)
            audio_peak_offset = feats.peak_offset()
        audio_peak = seg_start.timestamp() + audio_peak_offset

        # 弹幕峰值时间
        dms = _fetch_danmaku(session_id, seg_start, seg_end)
        if not dms:
            return 0.0
        dm_peak = max(dms, key=lambda x: x[1])[0].timestamp()
        return float((dm_peak - audio_peak) * 1000)  # ms
    except Exception:
        return 0.0
