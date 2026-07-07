"""数据库模型兼容门面 (V0.1.14.3)。

所有模型定义已迁移到 app/db/entities/ 子模块。
本文件仅作兼容重导出, 不包含主要业务逻辑。
"""

from __future__ import annotations

# ── 后向兼容: utcnow ──────────────────────────────────────
# ── 实体类 ─────────────────────────────────────────────
from app.db.entities.base import (
    CandidateStatus,
    ClipStatus,
    ClipVariantType,
    DanmakuType,
    RenderStatus,
    ReviewStatus,
    RoomMode,
    SegmentStatus,
    SessionStatus,
    TaskStatus,
    TopicStatus,
    UploadStatus,
    utcnow,  # noqa: F401
)
from app.db.entities.clip import ClipVariant, FinalClip
from app.db.entities.highlight import HighlightCandidate, HighlightEvent, HighlightTopic
from app.db.entities.publishing import UploadTask
from app.db.entities.recording import Danmaku, RawSegment, RecordingSchedule
from app.db.entities.room import LiveRoom, RecordingSession
from app.db.entities.settings import (
    AppSetting,
    IntroTemplate,
    SubtitleTemplate,
    SystemLog,
    ThresholdFeedback,
    TrendItem,
)
from app.db.entities.task import SegmentTask
from app.db.entities.topic import Topic
from app.db.entities.transcript import Transcript

__all__ = [
    "RoomMode",
    "SessionStatus",
    "SegmentStatus",
    "CandidateStatus",
    "ClipStatus",
    "ReviewStatus",
    "ClipVariantType",
    "TopicStatus",
    "TaskStatus",
    "RenderStatus",
    "UploadStatus",
    "DanmakuType",
    "FinalClip",
    "ClipVariant",
    "HighlightCandidate",
    "HighlightEvent",
    "HighlightTopic",
    "UploadTask",
    "RawSegment",
    "Danmaku",
    "RecordingSchedule",
    "LiveRoom",
    "RecordingSession",
    "ThresholdFeedback",
    "AppSetting",
    "SystemLog",
    "TrendItem",
    "SubtitleTemplate",
    "IntroTemplate",
    "SegmentTask",
    "Topic",
    "Transcript",
]
