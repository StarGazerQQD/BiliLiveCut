"""数据库实体 — Base (V0.1.14.3)."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """返回当前 UTC 时间(带时区)。

    :returns: 当前的 :class:`~datetime.datetime`(UTC)。
    """
    return datetime.now(UTC)


class RoomMode:
    """直播间审核模式。"""

    MANUAL = "manual"  # 候选需人工审核才发布
    SEMI = "semi"  # 高置信自动、中置信待审
    AUTO = "auto"  # 达阈值自动发布


class SessionStatus:
    """录制会话状态。"""

    STARTING = "starting"
    RECORDING = "recording"
    RECONNECTING = "reconnecting"
    RECONNECTED = "reconnected"  # 断流后成功重连(短暂状态,很快切回 RECORDING)
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"  # 进程异常退出,可自动恢复
    ERROR = "error"


class SegmentStatus:
    """原始片段处理状态。"""

    RECORDED = "recorded"
    TRANSCRIBED = "transcribed"
    SCORED = "scored"
    ARCHIVED = "archived"


class CandidateStatus:
    """高光候选状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CLIPPED = "clipped"
    MERGED = "merged"
    CLEANED = "cleaned"  # V0.1.7 P3:已清理


class ClipStatus:
    """成品切片状态。"""

    GENERATED = "generated"
    REVIEWING = "reviewing"
    READY = "ready"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ReviewStatus:
    """V0.1.6 P1 审核决断(细化原 approve/reject 二值)。"""

    APPROVED_SOLO = "approved_solo"  # 独立成片
    APPROVED_COLLECTION = "approved_collection"  # 同主题合集候选
    IN_COLLECTION = "in_collection"  # 已加入主题合集
    MAYBE_TOPIC = "maybe_topic"  # 可能属于某主题
    HOLD = "hold"  # 保留待定
    NOT_EXCITING = "not_exciting"  # 不够精彩
    INSUFFICIENT_CONTEXT = "insufficient_context"  # 上下文不足
    START_TOO_LATE = "start_too_late"  # 开头截晚
    END_TOO_EARLY = "end_too_early"  # 结尾截早
    DUPLICATE_CONTENT = "duplicate_content"  # 内容重复
    SUBTITLE_ERROR = "subtitle_error"  # 字幕错误
    VISUAL_ISSUE = "visual_issue"  # 画面异常
    SENSITIVE = "sensitive"  # 涉及敏感内容
    REJECTED = "rejected"  # 拒绝
    PENDING = "pending"  # 待审

    # V0.1.12.7: 向后兼容别名
    APPROVED = "approved_solo"  # 兼容旧代码中的 ReviewStatus.APPROVED

    # 正面状态集合(可用于统计)。
    POSITIVE = {APPROVED_SOLO, APPROVED_COLLECTION, IN_COLLECTION}
    # 需要持久化边界和数据的状态。
    KEEP_ASSETS = {APPROVED_SOLO, APPROVED_COLLECTION, IN_COLLECTION, MAYBE_TOPIC, HOLD}


class ClipVariantType:
    """成品版本类型。"""

    SINGLE = "single"  # 单段高光版
    FULL_CONTEXT = "full_context"  # 完整上下文版
    COLLECTION_CHAPTER = "collection_chapter"  # 同主题合集章节
    SUBTITLED = "subtitled"  # 带字幕版
    NO_SUBTITLES = "no_subtitles"  # 无字幕净版
    COMPRESSED = "compressed"  # 投稿压制版
    ARCHIVE = "archive"  # 高码率归档版


class TopicStatus:
    """主题状态。"""

    AUTO = "auto"  # 自动聚类,待确认
    CONFIRMED = "confirmed"  # 人工确认
    SPLIT = "split"  # 已拆分(错误聚类)
    BLOCKED = "blocked"  # 不适合生成合集


class TaskStatus:
    """分段处理任务状态(V0.1.12.5 重构审核→渲染→发布顺序)。"""

    RECORDED = "recorded"  # 片段已录制,待入队
    QUEUED_FOR_TRANS = "queued_for_transcription"  # 等待转写
    TRANSCRIBING = "transcribing"  # 正在转写
    TRANSCRIBED = "transcribed"  # 转写完成
    QUEUED_FOR_ANALYSIS = "queued_for_analysis"  # 等待分析
    ANALYZING = "analyzing"  # 正在分析(规则+LLM)
    CANDIDATE_CREATED = "candidate_created"  # 已生成候选
    AWAITING_REVIEW = "awaiting_review"  # 候选待审核/自动批准
    APPROVED = "approved"  # 已批准
    APPROVED_WAITING_RENDER = "approved_waiting_render"  # 已批准,等待手动渲染
    QUEUED_FOR_RENDER = "queued_for_render"  # 等待渲染
    RENDERING = "rendering"  # 正在渲染(FFmpeg)
    RENDERED = "rendered"  # 渲染完成,待发布决策
    AWAITING_PUBLISH_CONFIRMATION = "awaiting_publish_confirmation"  # 渲染完成,等待手动发布
    QUEUED_FOR_PUBLISH = "queued_for_publish"  # 等待发布
    PUBLISHING = "publishing"  # 正在发布
    COMPLETED = "completed"  # 最终完成
    FAILED = "failed"  # 永久失败(不可重试)
    CANCELLED = "cancelled"  # 已取消
    STALE = "stale"  # 心跳超时,待恢复

    # 临时失败子状态
    TRANSIENT_FAILED = "transient_failed"  # 临时失败,等待重试


class RenderStatus:
    """ClipVariant 渲染状态 (V0.1.12.8)。"""

    QUEUED = "queued"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


class UploadStatus:
    """上传任务状态。"""

    QUEUED = "queued"
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    MANUAL_EXPORT_READY = "manual_export_ready"  # V0.1.12.7: 手动上传清单已导出, 尚未发布


class DanmakuType:
    """弹幕/互动消息类型。"""

    DANMAKU = "danmaku"  # 普通弹幕
    GIFT = "gift"  # 礼物
    SUPERCHAT = "superchat"  # 醒目留言(SC)
    INTERACT = "interact"  # 进场/关注等互动
    OTHER = "other"
