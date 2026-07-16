"""时序/上下文特征提取器 (T1-T9)。

基于片段时间属性，纯 SQL 查询 + numpy 计算，无模型依赖。
"""
from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_TEMPORAL_NAMES = [
    "segment_duration_s", "segment_size_bytes", "session_elapsed_ratio",
    "time_since_last_highlight_s",
    "neighbor_volume_diff", "neighbor_dm_diff",
    "rolling_volume_avg", "rolling_dm_avg", "feature_change_rate",
]


class TemporalExtractor(BaseFeatureExtractor):
    """时序/上下文特征提取器 — 9 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_TEMPORAL_NAMES)

    @property
    def n_features(self) -> int:
        return 9

    def extract(self, segment_id: int) -> np.ndarray:
        feats = np.zeros(self.n_features, dtype=np.float32)
        seg = _get_segment_meta(segment_id)
        if seg is None:
            return feats

        duration = seg.get("duration_s", 60)
        feats[0] = float(duration)             # T1
        feats[1] = float(seg.get("size_bytes", 0) or 0) / 1e6  # T2 MB

        # T3: 直播进度比
        feats[2] = _session_elapsed_ratio(seg)

        # T4: 距上高光间隔
        feats[3] = _time_since_last_highlight(seg.get("session_id", 0),
                                               seg.get("start_ts"))

        # T5-T6: 邻段差异（用文件大小近似）
        feats[4] = _neighbor_size_diff(seg)
        feats[5] = _neighbor_dm_diff(seg)

        # T7-T8: 滑动均值
        feats[6] = _rolling_size_mean(seg, window=5)
        feats[7] = _rolling_dm_mean(seg, window=5)

        # T9: 特征突变率（前后段文件大小变化率）
        feats[8] = _feature_change_rate(seg)

        return feats


def _get_segment_meta(segment_id: int) -> dict | None:
    try:
        from app.db.models import RawSegment
        from app.db.session import get_session
        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return None
            return {
                "session_id": seg.session_id,
                "seq": seg.seq,
                "duration_s": seg.duration_s or 60.0,
                "size_bytes": seg.size_bytes or 0,
                "start_ts": seg.start_ts,
                "end_ts": seg.end_ts,
                "file_path": seg.file_path,
            }
    except Exception:
        return None


def _session_elapsed_ratio(seg: dict) -> float:
    """片段在整场直播中的位置比例。"""
    try:
        from app.db.models import RawSegment
        from app.db.session import get_session
        from sqlmodel import select, func
        with get_session() as db:
            total = db.scalar(
                select(func.count()).where(
                    RawSegment.session_id == seg["session_id"]
                )
            ) or 1
        return seg.get("seq", 0) / max(total, 1)
    except Exception:
        return 0.0


def _time_since_last_highlight(session_id: int, start_ts) -> float:
    """距上一个高光候选的秒数。"""
    if start_ts is None:
        return 300.0
    try:
        from app.db.models import HighlightCandidate
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            prev = db.exec(
                select(HighlightCandidate).where(
                    HighlightCandidate.session_id == session_id,
                    HighlightCandidate.start_ts < start_ts,
                ).order_by(HighlightCandidate.start_ts.desc())
            ).first()
        if prev and prev.end_ts:
            return (start_ts - prev.end_ts).total_seconds()
    except Exception:
        pass
    return 300.0


def _rolling_size_mean(seg: dict, window: int = 5) -> float:
    return _rolling_avg(seg, window, "size")


def _rolling_dm_mean(seg: dict, window: int = 5) -> float:
    return _rolling_avg(seg, window, "dm")


def _rolling_avg(seg: dict, window: int, kind: str) -> float:
    """前 N 段滑动窗口均值。"""
    try:
        from app.db.models import RawSegment, Danmaku
        from app.db.session import get_session
        from sqlmodel import select, func
        seq = seg.get("seq", 0); sid = seg.get("session_id", 0)
        if seq < 1: return 0.0
        with get_session() as db:
            prev = db.exec(
                select(RawSegment).where(
                    RawSegment.session_id == sid, RawSegment.seq < seq
                ).order_by(RawSegment.seq.desc()).limit(window)
            ).all()
        if not prev: return 0.0
        if kind == "size":
            sizes = [p.size_bytes or 0 for p in prev]
            return float(np.mean(sizes) / 1e6) if sizes else 0.0
        else:
            dm_counts = []
            for p in prev:
                cnt = db.scalar(
                    select(func.count()).select_from(Danmaku).where(
                        Danmaku.session_id == sid, Danmaku.ts >= p.start_ts,
                        Danmaku.ts <= p.end_ts
                    )
                ) or 0
                dur = p.duration_s or 60
                dm_counts.append(cnt / max(dur, 1))
            return float(np.mean(dm_counts)) if dm_counts else 0.0
    except Exception:
        return 0.0


def _neighbor_size_diff(seg: dict) -> float:
    """前后段文件大小差值（标准化）。"""
    try:
        from app.db.models import RawSegment
        from app.db.session import get_session
        from sqlmodel import select
        seq = seg.get("seq", 0); sid = seg.get("session_id", 0)
        cur_sz = seg.get("size_bytes", 0) or 0
        with get_session() as db:
            prev = db.exec(
                select(RawSegment).where(
                    RawSegment.session_id == sid, RawSegment.seq == seq - 1
                ).limit(1)
            ).first()
        prev_sz = prev.size_bytes if prev else cur_sz
        diff = abs(cur_sz - (prev_sz or 0))
        return diff / max(cur_sz, 1)
    except Exception:
        return 0.0


def _neighbor_dm_diff(seg: dict) -> float:
    """前后段弹幕密度差值。"""
    try:
        from app.db.models import RawSegment, Danmaku
        from app.db.session import get_session
        from sqlmodel import select, func
        seq = seg.get("seq", 0); sid = seg.get("session_id", 0)
        with get_session() as db:
            cur_cnt = db.scalar(
                select(func.count()).select_from(Danmaku).where(
                    Danmaku.session_id == sid, Danmaku.ts >= seg.get("start_ts"),
                    Danmaku.ts <= seg.get("end_ts")
                )
            ) or 0
            prev_seg = db.exec(
                select(RawSegment).where(
                    RawSegment.session_id == sid, RawSegment.seq == seq - 1
                ).limit(1)
            ).first()
        if prev_seg is None:
            return 0.0
        with get_session() as db:
            prev_cnt = db.scalar(
                select(func.count()).select_from(Danmaku).where(
                    Danmaku.session_id == sid, Danmaku.ts >= prev_seg.start_ts,
                    Danmaku.ts <= prev_seg.end_ts
                )
            ) or 0
        cur_dur = seg.get("duration_s", 60) or 1
        prev_dur = prev_seg.duration_s or 60
        return abs(cur_cnt / cur_dur - prev_cnt / prev_dur)
    except Exception:
        return 0.0


def _feature_change_rate(seg: dict) -> float:
    """当前段与前一段文件大小的变化率。"""
    try:
        from app.db.models import RawSegment
        from app.db.session import get_session
        from sqlmodel import select
        seq = seg.get("seq", 0); sid = seg.get("session_id", 0)
        cur_sz = seg.get("size_bytes", 0) or 1
        with get_session() as db:
            prev = db.exec(
                select(RawSegment).where(
                    RawSegment.session_id == sid, RawSegment.seq == seq - 1
                ).limit(1)
            ).first()
        if prev is None or prev.size_bytes is None: return 0.0
        return float((cur_sz - prev.size_bytes) / max(prev.size_bytes, 1))
    except Exception:
        return 0.0
