"""元数据/画像特征提取器 (M1-M11)。

基于房间历史数据 + 时间周期编码，纯 SQL 查询，无模型依赖。
"""
from __future__ import annotations

import math

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_METADATA_NAMES = [
    "streamer_id", "room_hist_highlight_rate", "room_approval_rate",
    "room_current_threshold", "room_auto_approve_threshold",
    "time_of_day_sin", "time_of_day_cos",
    "day_of_week_sin", "day_of_week_cos",
    "stream_duration_minutes", "config_hotword_count",
]


class MetadataExtractor(BaseFeatureExtractor):
    """元数据/画像特征提取器 — 11 维。"""

    @property
    def feature_names(self) -> list[str]:
        return list(_METADATA_NAMES)

    @property
    def n_features(self) -> int:
        return 11

    def extract(self, segment_id: int) -> np.ndarray:
        feats = np.zeros(self.n_features, dtype=np.float32)
        profile = _get_room_profile(segment_id)
        if profile is None:
            return feats

        feats[0] = float(profile.get("room_id", 0))                      # M1
        feats[1] = profile.get("hist_rate", 0.0)                         # M2
        feats[2] = profile.get("approval_rate", 0.0)                     # M3
        feats[3] = profile.get("threshold", 0.65)                        # M4
        feats[4] = profile.get("auto_threshold", 0.82)                   # M5

        # M6-M9: 周期编码
        ts = profile.get("start_ts")
        if ts:
            hour = ts.hour + ts.minute / 60.0
            feats[5] = float(math.sin(2 * math.pi * hour / 24))  # M6
            feats[6] = float(math.cos(2 * math.pi * hour / 24))  # M7
            wd = ts.weekday()
            feats[7] = float(math.sin(2 * math.pi * wd / 7))     # M8
            feats[8] = float(math.cos(2 * math.pi * wd / 7))     # M9

        feats[9] = profile.get("duration_minutes", 0.0)                 # M10
        feats[10] = float(profile.get("hotword_count", 0))               # M11

        return feats


def _get_room_profile(segment_id: int) -> dict | None:
    try:
        from app.db.models import LiveRoom, RawSegment, RecordingSession
        from app.db.session import get_session
        from sqlmodel import select, func

        with get_session() as db:
            seg = db.get(RawSegment, segment_id)
            if seg is None:
                return None
            sess = db.get(RecordingSession, seg.session_id)
            if sess is None:
                return None
            room = db.get(LiveRoom, sess.room_id)
            if room is None:
                return None

            # 历史高光密度 (个/小时)
            total_highlights = db.scalar(
                select(func.count()).select_from(
                    __import__("app.db.models", fromlist=["HighlightCandidate"]).HighlightCandidate
                ).where(
                    __import__("app.db.models", fromlist=["HighlightCandidate"]).HighlightCandidate.session_id.in_(
                        select(RecordingSession.id).where(RecordingSession.room_id == room.id)
                    )
                )
            ) or 0
            total_hours = max(
                db.scalar(select(func.sum(RawSegment.duration_s)).where(
                    RawSegment.session_id.in_(
                        select(RecordingSession.id).where(RecordingSession.room_id == room.id)
                    )
                )) or 0, 3600
            ) / 3600
            hist_rate = total_highlights / max(total_hours, 0.5)

            # 审批率
            approved = db.scalar(
                select(func.count()).select_from(
                    __import__("app.db.models", fromlist=["ThresholdFeedback"]).ThresholdFeedback
                ).where(
                    __import__("app.db.models", fromlist=["ThresholdFeedback"]).ThresholdFeedback.room_id == room.id,
                    __import__("app.db.models", fromlist=["ThresholdFeedback"]).ThresholdFeedback.action == "approved",
                )
            ) or 0
            total_fb = db.scalar(
                select(func.count()).select_from(
                    __import__("app.db.models", fromlist=["ThresholdFeedback"]).ThresholdFeedback
                ).where(
                    __import__("app.db.models", fromlist=["ThresholdFeedback"]).ThresholdFeedback.room_id == room.id,
                )
            ) or 0
            approval_rate = approved / max(total_fb, 1)

            # 热词数
            import json as _json
            cfg = _json.loads(room.room_config_json) if room.room_config_json else {}
            hotword_count = len(cfg.get("hotwords", []))

        return {
            "room_id": room.room_id or room.id,
            "hist_rate": round(hist_rate, 4),
            "approval_rate": round(approval_rate, 4),
            "threshold": room.highlight_threshold,
            "auto_threshold": room.auto_approve_threshold,
            "start_ts": seg.start_ts,
            "duration_minutes": (seg.end_ts - sess.started_at).total_seconds() / 60
                if seg.end_ts and sess.started_at else 0.0,
            "hotword_count": hotword_count,
        }
    except Exception:
        return None
