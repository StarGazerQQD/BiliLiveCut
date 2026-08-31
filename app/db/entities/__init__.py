"""数据库实体模型的唯一公开入口。"""

from app.db.entities.base import (
    CandidateStatus,
    ClipStatus,
    ClipVariantType,
    DanmakuType,
    HotspotStatus,
    RenderStatus,
    ReviewStatus,
    SegmentStatus,
    SessionStatus,
    TaskStatus,
    TopicStatus,
    UploadStatus,
    utcnow,
)
from app.db.entities.clip import ClipVariant, FinalClip
from app.db.entities.highlight import HighlightCandidate, HighlightEvent, HighlightTopic
from app.db.entities.hotspot import HotspotEvent
from app.db.entities.publishing import UploadAttempt, UploadTask
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
    "AppSetting",
    "CandidateStatus",
    "ClipStatus",
    "ClipVariant",
    "ClipVariantType",
    "Danmaku",
    "DanmakuType",
    "FinalClip",
    "HighlightCandidate",
    "HighlightEvent",
    "HighlightTopic",
    "HotspotEvent",
    "HotspotStatus",
    "IntroTemplate",
    "LiveRoom",
    "RawSegment",
    "RecordingSchedule",
    "RecordingSession",
    "RenderStatus",
    "ReviewStatus",
    "SegmentStatus",
    "SegmentTask",
    "SessionStatus",
    "SubtitleTemplate",
    "SystemLog",
    "TaskStatus",
    "ThresholdFeedback",
    "Topic",
    "TopicStatus",
    "Transcript",
    "TrendItem",
    "UploadAttempt",
    "UploadStatus",
    "UploadTask",
    "utcnow",
]
