"""数据库表模型(SQLModel)。

对应设计文档第四步的八张表。所有时间字段统一使用 UTC。
枚举状态用 ``str`` + 常量类表达,避免数据库层枚举迁移的复杂度。

设计原则:

* 字段含义见各 :class:`~sqlmodel.Field` 的 ``description``;
* 复杂结构(词级时间戳、特征、标签)以 JSON 字符串存于 ``*_json`` 字段;
* 外键以 ``*_id`` 命名,跨表只存 id,不做强约束级联(SQLite 友好)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """返回当前 UTC 时间(带时区)。

    :returns: 当前的 :class:`~datetime.datetime`(UTC)。
    """
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# 状态常量(避免散落的魔法字符串)
# --------------------------------------------------------------------------- #
class RoomMode:
    """直播间审核模式。"""

    MANUAL = "manual"  # 候选需人工审核才发布
    SEMI = "semi"      # 高置信自动、中置信待审
    AUTO = "auto"      # 达阈值自动发布


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

    APPROVED_SOLO = "approved_solo"          # 独立成片
    APPROVED_COLLECTION = "approved_collection"  # 同主题合集候选
    IN_COLLECTION = "in_collection"          # 已加入主题合集
    MAYBE_TOPIC = "maybe_topic"              # 可能属于某主题
    HOLD = "hold"                            # 保留待定
    NOT_EXCITING = "not_exciting"            # 不够精彩
    INSUFFICIENT_CONTEXT = "insufficient_context"  # 上下文不足
    START_TOO_LATE = "start_too_late"         # 开头截晚
    END_TOO_EARLY = "end_too_early"           # 结尾截早
    DUPLICATE_CONTENT = "duplicate_content"   # 内容重复
    SUBTITLE_ERROR = "subtitle_error"         # 字幕错误
    VISUAL_ISSUE = "visual_issue"             # 画面异常
    SENSITIVE = "sensitive"                   # 涉及敏感内容
    REJECTED = "rejected"                     # 拒绝
    PENDING = "pending"                       # 待审

    # 正面状态集合(可用于统计)。
    POSITIVE = {APPROVED_SOLO, APPROVED_COLLECTION, IN_COLLECTION}
    # 需要持久化边界和数据的状态。
    KEEP_ASSETS = {APPROVED_SOLO, APPROVED_COLLECTION, IN_COLLECTION, MAYBE_TOPIC, HOLD}


class ClipVariantType:
    """成品版本类型。"""

    SINGLE = "single"               # 单段高光版
    FULL_CONTEXT = "full_context"   # 完整上下文版
    COLLECTION_CHAPTER = "collection_chapter"  # 同主题合集章节
    SUBTITLED = "subtitled"         # 带字幕版
    NO_SUBTITLES = "no_subtitles"   # 无字幕净版
    COMPRESSED = "compressed"       # 投稿压制版
    ARCHIVE = "archive"             # 高码率归档版


class TopicStatus:
    """主题状态。"""

    AUTO = "auto"           # 自动聚类,待确认
    CONFIRMED = "confirmed"  # 人工确认
    SPLIT = "split"         # 已拆分(错误聚类)
    BLOCKED = "blocked"     # 不适合生成合集


class TaskStatus:
    """分段处理任务状态(V0.1.11-alpha 重构)。"""

    RECORDED = "recorded"                    # 片段已录制,待入队
    QUEUED_FOR_TRANS = "queued_for_transcription"  # 等待转写
    TRANSCRIBING = "transcribing"            # 正在转写(Whisper GPU)
    TRANSCRIBED = "transcribed"              # 转写完成,待评分
    QUEUED_FOR_ANALYSIS = "queued_for_analysis"    # 等待分析
    ANALYZING = "analyzing"                  # 正在分析(规则+LLM)
    CANDIDATE_CREATED = "candidate_created"  # 已生成候选
    QUEUED_FOR_RENDER = "queued_for_render"  # 等待渲染
    RENDERING = "rendering"                  # 正在渲染(FFmpeg)
    AWAITING_REVIEW = "awaiting_review"      # 候选待审核
    APPROVED = "approved"                    # 人工/自动批准
    COMPLETED = "completed"                  # 最终完成
    FAILED = "failed"                        # 永久失败(不可重试)
    CANCELLED = "cancelled"                  # 已取消
    STALE = "stale"                          # V0.1.11-alpha: 心跳超时,待恢复
    QUEUED_FOR_PUBLISH = "queued_for_publish"  # V0.1.11-alpha: 等待发布

    # 临时失败子状态
    TRANSIENT_FAILED = "transient_failed"    # 临时失败,等待重试


class UploadStatus:
    """上传任务状态。"""

    QUEUED = "queued"
    UPLOADING = "uploading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


# --------------------------------------------------------------------------- #
# 表模型
# --------------------------------------------------------------------------- #
class LiveRoom(SQLModel, table=True):
    """直播间(``live_rooms``):被监控的直播源及其策略配置。"""

    __tablename__ = "live_rooms"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(default="bilibili", description="平台标识")
    input_url: str = Field(description="用户输入的原始 URL 或短号")
    room_id: int | None = Field(default=None, index=True, description="归一化后的真实房间号")
    uploader_name: str | None = Field(default=None, description="主播名")
    title: str | None = Field(default=None, description="直播间标题")
    mode: str = Field(default=RoomMode.MANUAL, description="[已废弃 V0.1.6]审核模式:manual/semi/auto;请改用 auto_* 开关")
    highlight_threshold: float = Field(default=0.65, description="进入候选池的综合评分阈值")
    auto_publish_threshold: float = Field(default=0.85, description="自动发布阈值")
    enabled: bool = Field(default=False, description="是否启用监控/录制")
    authorized: bool = Field(default=False, description="是否已确认拥有录制授权(合规闸)")

    # V0.1.6: 独立自动化开关(替代旧 mode)。
    auto_record: bool = Field(default=False, description="是否允许自动开始录制")
    auto_analyze: bool = Field(default=False, description="是否自动执行转写+高光分析")
    auto_render: bool = Field(default=False, description="是否自动生成切片成品")
    auto_approve: bool = Field(default=False, description="是否自动批准高分候选(免人工审核)")
    auto_upload: bool = Field(default=False, description="是否自动提交上传任务")

    # V0.1.6: 审核阈值。
    auto_approve_threshold: float = Field(default=0.82, description="≥此分自动批准")
    review_threshold: float = Field(default=0.50, description="≥此分进入人工审核;低于此分自动淘汰")

    # V0.1.2 新增:房间级功能开关(录制启动后锁定,不可更改启用状态)
    schedule_enabled: bool = Field(default=False, description="是否启用录制预约")
    auto_threshold_enabled: bool = Field(default=False, description="是否启用阈值自学习")
    danmaku_sentiment_enabled: bool = Field(default=False, description="是否启用弹幕情绪分析")

    # V0.1.6 P2:房间级配置(热词/别名/高光关键词/屏蔽主题,存储为 JSON)。
    room_config_json: str | None = Field(default=None, description="房间配置 JSON(hotwords/aliases/highlight_keywords/blocked_topics)")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RecordingSession(SQLModel, table=True):
    """录制会话(``recording_sessions``):一次连续录制的生命周期。"""

    __tablename__ = "recording_sessions"

    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True, description="所属 live_rooms.id")
    stream_url: str | None = Field(default=None, description="本次拉流地址(短期,可空)")
    stream_format: str | None = Field(default=None, description="hls / flv")
    quality: int | None = Field(default=None, description="清晰度码 qn")
    status: str = Field(default=SessionStatus.STARTING, description="会话状态")
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = Field(default=None)
    reconnect_count: int = Field(default=0, description="断流重连次数")
    last_reconnected_at: datetime | None = Field(default=None, description="最近一次重连成功时刻(UTC)")
    error_message: str | None = Field(default=None, description="最后一次错误信息")


class RawSegment(SQLModel, table=True):
    """原始片段(``raw_segments``):FFmpeg 按固定时长切出的录制文件。"""

    __tablename__ = "raw_segments"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    seq: int = Field(description="片段序号(从 0 递增)")
    file_path: str = Field(description="本地文件路径")
    start_ts: datetime | None = Field(default=None, description="片段对应直播起始时间")
    end_ts: datetime | None = Field(default=None, description="片段对应直播结束时间")
    duration_s: float | None = Field(default=None, description="片段时长(秒)")
    size_bytes: int | None = Field(default=None, description="文件大小(字节)")
    status: str = Field(default=SegmentStatus.RECORDED, description="处理状态")


class Transcript(SQLModel, table=True):
    """转写结果(``transcripts``):某片段的语音转文字。"""

    __tablename__ = "transcripts"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True, description="所属 raw_segments.id")
    language: str | None = Field(default=None, description="识别语言")
    text: str = Field(default="", description="转写全文")
    words_json: str | None = Field(default=None, description="词级时间戳 JSON: [{w,start,end}]")
    avg_logprob: float | None = Field(default=None, description="平均置信度")
    created_at: datetime = Field(default_factory=utcnow)


class HighlightCandidate(SQLModel, table=True):
    """高光候选(``highlight_candidates``):达阈值待切片的爆点。"""

    __tablename__ = "highlight_candidates"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    peak_ts: datetime = Field(description="爆点时刻")
    start_ts: datetime = Field(description="建议切片起点(含前置留白)")
    end_ts: datetime = Field(description="建议切片终点(含后置留白)")
    rule_score: float = Field(default=0.0, description="规则打分")
    llm_score: float = Field(default=0.0, description="LLM 复核打分")
    highlight_score: float = Field(default=0.0, description="综合高光评分")
    features_json: str | None = Field(default=None, description="各维度特征 JSON")
    reason: str | None = Field(default=None, description="LLM 给出的高光理由")
    status: str = Field(default=CandidateStatus.PENDING, description="候选状态")
    dedup_hash: str | None = Field(default=None, index=True, description="内容指纹,用于查重")
    created_at: datetime = Field(default_factory=utcnow)


class HighlightEvent(SQLModel, table=True):
    """高光事件(``highlight_events``):V0.1.6 P1 拆分为独立事件模型。

    代表"直播中发生了一件值得剪辑的事情",与 highlight_candidates 共存。
    新增:人工调整边界、细粒度审核决断、主题归属、审核原因、ASR 文本留存。
    """

    __tablename__ = "highlight_events"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int | None = Field(default=None, index=True, description="关联 highlight_candidates.id(可空)")
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    segment_id: int | None = Field(default=None, description="来源 raw_segments.id")

    # 时间边界(原始 + 人工调整)。
    raw_start_ts: datetime | None = Field(default=None, description="原始评分起点")
    raw_end_ts: datetime | None = Field(default=None, description="原始评分终点")
    adjusted_start_ts: datetime | None = Field(default=None, description="人工调整后起点")
    adjusted_end_ts: datetime | None = Field(default=None, description="人工调整后终点")

    # 评分。
    rule_score: float = Field(default=0.0)
    llm_score: float = Field(default=0.0)
    highlight_score: float = Field(default=0.0, description="综合高光评分")
    features_json: str | None = Field(default=None, description="各维度特征 JSON(含 danmaku_explain)")
    reason: str | None = Field(default=None, description="LLM 高光理由")
    asr_text: str | None = Field(default=None, description="ASR 转写文本(留存)")
    danmaku_explain_json: str | None = Field(default=None, description="弹幕评分解释 JSON")

    # 审核。
    review_status: str = Field(default=ReviewStatus.PENDING, description="审核决断")
    review_reason: str | None = Field(default=None, description="审核原因/备注")
    review_by: str = Field(default="auto", description="审核者:auto/manual")

    # 主题。
    topic_id: int | None = Field(default=None, index=True, description="所属 highlight_topics.id")

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ClipVariant(SQLModel, table=True):
    """成品版本(``clip_variants``):同一事件的不同渲染版本。

    一个 HighlightEvent 可产生多个 ClipVariant:
    - 单段高光版(single)
    - 完整上下文版(full_context)
    - 合集章节(collection_chapter)
    - 带字幕版(subtitled)
    - 无字幕净版(no_subtitles)
    - 投稿压制版(compressed)
    - 高码率归档版(archive)
    """

    __tablename__ = "clip_variants"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(index=True, description="关联 highlight_events.id")
    candidate_id: int | None = Field(default=None, index=True, description="关联 highlight_candidates.id(向后兼容)")

    variant_type: str = Field(default=ClipVariantType.SINGLE, description="版本类型")

    # 渲染参数。
    start_ts: datetime | None = Field(default=None, description="实际渲染起点")
    end_ts: datetime | None = Field(default=None, description="实际渲染终点")
    has_subtitles: bool = Field(default=True, description="是否包含字幕")
    resolution: str | None = Field(default=None, description="输出分辨率,如 1920×1080")
    codec_params: str | None = Field(default=None, description="编码参数")

    # 文件。
    file_path: str | None = Field(default=None, description="文件路径")
    file_hash: str | None = Field(default=None, description="文件 SHA256")
    cover_path: str | None = Field(default=None, description="封面图路径")
    duration_s: float | None = Field(default=None, description="时长(秒)")

    render_status: str = Field(default="queued", description="渲染状态:queued/rendering/done/failed")
    version_number: int = Field(default=1, description="版本号(同 variant_type 同 event 递增)")

    created_at: datetime = Field(default_factory=utcnow)


class Topic(SQLModel, table=True):
    """主题/事件簇(``topics``):同一直播中语义相关的多个高光。

    主题判定分为三级:同一主题 > 可能相关 > 不同主题。
    """

    __tablename__ = "topics"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    title: str | None = Field(default=None, description="主题标题")
    summary: str | None = Field(default=None, description="主题摘要")
    keywords_json: str | None = Field(default=None, description="关键词 JSON 数组")
    entities_json: str | None = Field(default=None, description="实体 JSON(人物/游戏/歌曲等)")
    confidence: float = Field(default=0.0, description="主题置信度")
    status: str = Field(default=TopicStatus.AUTO, description="auto/confirmed/split/blocked")
    is_collection: bool = Field(default=False, description="是否适合生成合集")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class HighlightTopic(SQLModel, table=True):
    """事件-主题关联(``highlight_topics``):多对多映射。

    V0.1.11-alpha: event_id 永远指向 HighlightEvent.id; confirmed_by_user 标记人工确认。
    """

    __tablename__ = "highlight_topics"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(index=True, description="关联 highlight_events.id")
    topic_id: int = Field(index=True, description="关联 topics.id")
    confidence: float = Field(default=0.0, description="该事件属于本主题的相似度")
    is_manual: bool = Field(default=False, description="是否人工手动归类")
    sort_order: int = Field(default=0, description="在合集中的顺序")
    chapter_title: str | None = Field(default=None, description="合集内章节标题")
    confirmed_by_user: bool = Field(default=False, description="V0.1.11-alpha:已人工确认,后续自动聚类不覆盖")
    created_at: datetime = Field(default_factory=utcnow)


class FinalClip(SQLModel, table=True):
    """成品切片(``final_clips``):后处理完成、可投稿的 MP4 及其元数据。"""

    __tablename__ = "final_clips"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(index=True, description="来源 highlight_candidates.id")
    file_path: str = Field(description="成品 MP4 路径")
    cover_path: str | None = Field(default=None, description="封面图路径")
    duration_s: float | None = Field(default=None, description="时长(秒)")
    width: int | None = Field(default=None, description="宽")
    height: int | None = Field(default=None, description="高")
    title: str | None = Field(default=None, description="标题")
    description: str | None = Field(default=None, description="简介")
    tags_json: str | None = Field(default=None, description="标签 JSON 数组")
    publish_suggestion: str | None = Field(default=None, description="发布时间/是否值得发布建议")
    content_hash: str | None = Field(default=None, index=True, description="内容指纹")
    status: str = Field(default=ClipStatus.GENERATED, description="切片状态")
    created_at: datetime = Field(default_factory=utcnow)


class UploadTask(SQLModel, table=True):
    """上传任务(``upload_tasks``):成品进入上传队列后的执行记录。"""

    __tablename__ = "upload_tasks"

    id: int | None = Field(default=None, primary_key=True)
    clip_id: int = Field(index=True, description="所属 final_clips.id")
    uploader: str = Field(default="manual", description="使用的上传器")
    status: str = Field(default=UploadStatus.QUEUED, description="任务状态")
    attempts: int = Field(default=0, description="已尝试次数")
    last_error: str | None = Field(default=None, description="最后错误")
    remote_id: str | None = Field(default=None, description="平台返回的稿件号(若有)")
    precheck_json: str | None = Field(default=None, description="预检结果 JSON")
    scheduled_at: datetime | None = Field(default=None, description="计划上传时间")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DanmakuType:
    """弹幕/互动消息类型。"""

    DANMAKU = "danmaku"      # 普通弹幕
    GIFT = "gift"            # 礼物
    SUPERCHAT = "superchat"  # 醒目留言(SC)
    INTERACT = "interact"    # 进场/关注等互动
    OTHER = "other"


class Danmaku(SQLModel, table=True):
    """弹幕/互动事件(``danmaku``)。

    用于"弹幕热度"视图与高光评分中的弹幕维度。``ts`` 使用接收到的墙钟时间(UTC),
    与原始片段的 ``start_ts``/``end_ts`` 对齐以便按窗口统计速率。
    """

    __tablename__ = "danmaku"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    room_id: int = Field(index=True, description="真实房间号")
    ts: datetime = Field(default_factory=utcnow, index=True, description="接收时间(UTC)")
    msg_type: str = Field(default=DanmakuType.DANMAKU, description="消息类型")
    user: str | None = Field(default=None, description="发送者昵称")
    content: str | None = Field(default=None, description="弹幕文本/礼物名等")
    value: float = Field(default=1.0, description="价值权重(礼物/SC 价格,普通弹幕为 1)")


class TrendItem(SQLModel, table=True):
    """网感资料库条目(``trend_items``)。

    存放从全网采集到的高热度内容(标题、摘要、标签、热度)。供高光评分的
    "网感/题材关联"维度与文案生成的风格参考使用。同一来源+标题的条目以
    ``content_hash`` 去重:重复出现时累加 ``seen_count`` 并刷新热度,从而反映
    "近期热度"的变化趋势。
    """

    __tablename__ = "trend_items"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(default="web", index=True, description="来源,如 bilibili/douyin/weibo/web")
    category: str | None = Field(default=None, index=True, description="题材分类,如 游戏/知识/生活")
    title: str = Field(description="标题/话题")
    summary: str | None = Field(default=None, description="可获取到的简介/摘要文字")
    url: str | None = Field(default=None, description="原始链接(若有)")
    tags_json: str = Field(default="[]", description="标签列表 JSON")
    keywords_json: str = Field(default="[]", description="抽取出的关键词列表 JSON")
    heat: float = Field(default=0.0, index=True, description="最近一次相对热度(0-100)")
    heat_peak: float = Field(default=0.0, description="历史峰值热度")
    seen_count: int = Field(default=1, description="被采集到的次数(近期活跃度)")
    content_hash: str = Field(index=True, description="去重指纹(source+title 的 SHA1)")
    first_seen_at: datetime = Field(default_factory=utcnow, description="首次采集时间")
    collected_at: datetime = Field(default_factory=utcnow, index=True, description="最近采集时间")
    raw_json: str | None = Field(default=None, description="原始返回数据 JSON(留档)")


class RecordingSchedule(SQLModel, table=True):
    """录制预约(``recording_schedules``):预定时间自动启动录制。"""

    __tablename__ = "recording_schedules"

    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True, description="所属 live_rooms.id")
    scheduled_at: datetime = Field(description="计划启动时间(UTC)")
    enabled: bool = Field(default=True, description="是否启用")
    recurrent: str = Field(default="", description="重复规则:空=一次性,daily=每日,weekly=每周")
    triggered: bool = Field(default=False, description="是否已触发")
    created_at: datetime = Field(default_factory=utcnow)


class ThresholdFeedback(SQLModel, table=True):
    """阈值自学习反馈记录(``threshold_feedback``):用户审批/拒绝候选时记录评分与阈值快照。"""

    __tablename__ = "threshold_feedback"

    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True, description="所属 live_rooms.id")
    candidate_id: int = Field(index=True, description="关联 highlight_candidates.id")
    action: str = Field(description="approved 或 rejected")
    old_threshold: float = Field(description="当前房间阈值快照")
    highlight_score: float = Field(description="候选的综合高光评分")
    created_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    """运行时键值配置(``app_settings``)。

    用于存放可在 Web 后台动态切换、需跨重启持久化的开关(如 biliup 启用状态),
    默认值仍来自环境变量/代码,本表仅覆盖被显式修改的项。
    """

    __tablename__ = "app_settings"

    key: str = Field(primary_key=True, description="配置键")
    value: str = Field(default="", description="配置值(字符串)")
    updated_at: datetime = Field(default_factory=utcnow)


class SystemLog(SQLModel, table=True):
    """系统日志(``system_logs``):结构化业务事件,供后台查看。"""

    __tablename__ = "system_logs"

    id: int | None = Field(default=None, primary_key=True)
    level: str = Field(default="INFO", description="日志级别")
    module: str | None = Field(default=None, description="来源模块")
    room_id: int | None = Field(default=None, index=True, description="关联直播间(可空)")
    event: str | None = Field(default=None, description="事件名")
    message: str = Field(default="", description="详情")
    context_json: str | None = Field(default=None, description="上下文 JSON")
    created_at: datetime = Field(default_factory=utcnow)


class SegmentTask(SQLModel, table=True):
    """分段处理任务(``segment_tasks``):持久化的异步任务队列 (V0.1.11-alpha 重构)。

    每个 RawSegment 录制完成后创建一条任务,按流水线阶段独立推进:
    recorded → transcribing → analyzing → rendering → approved/completed/failed。

    V0.1.11-alpha 新增:
    - failed_stage / claimed_by / claimed_at / heartbeat_at 字段
    - max_retries 默认值从 3 提升至 5
    - attempts 只在实际开始执行时增加一次
    """

    __tablename__ = "segment_tasks"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True, description="关联 raw_segments.id")
    session_id: int = Field(index=True, description="关联 recording_sessions.id")
    candidate_id: int | None = Field(default=None, index=True, description="关联 highlight_candidates.id(若有)")
    event_id: int | None = Field(default=None, index=True, description="V0.1.11-alpha:关联 highlight_events.id(若有)")
    clip_id: int | None = Field(default=None, index=True, description="关联 final_clips.id(若有)")

    stage: str = Field(default=TaskStatus.RECORDED, index=True, description="当前处理阶段")
    failed_stage: str | None = Field(default=None, description="V0.1.11-alpha:失败时的阶段,用于精确恢复")
    priority: int = Field(default=100, description="优先级(数值越小越优先)")
    idempotency_key: str | None = Field(default=None, index=True, description="幂等键:segment_id:stage,防重复")
    attempts: int = Field(default=0, description="当前阶段已尝试次数")
    max_retries: int = Field(default=5, description="V0.1.11-alpha:当前阶段最大重试次数(默认5)")
    next_retry_at: datetime | None = Field(default=None, description="下次重试时间(指数退避,含随机抖动)")
    last_error: str | None = Field(default=None, description="最近一次错误信息")
    error_is_permanent: bool = Field(default=False, description="是否为不可恢复的永久错误")

    # V0.1.11-alpha: 并发控制与崩溃恢复
    claimed_by: str | None = Field(default=None, description="领取该任务的 Worker ID(防重复领取)")
    claimed_at: datetime | None = Field(default=None, description="任务被领取的时间")
    heartbeat_at: datetime | None = Field(default=None, description="最后心跳时间(超时判定 stale)")

    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = Field(default=None, description="当前阶段开始处理时间")
    completed_at: datetime | None = Field(default=None, description="当前阶段完成时间")
    processing_time_ms: int | None = Field(default=None, description="当前阶段处理耗时(毫秒)")
    total_elapsed_ms: int | None = Field(default=None, description="任务总耗时(创建到完成,毫秒)")

    context_json: str | None = Field(default=None, description="任务上下文 JSON(如错误堆栈、配置快照等)")


class SubtitleTemplate(SQLModel, table=True):
    """ASS 字幕样式模板(``subtitle_templates``)

    V0.1.8 P0:存储自定义 ASS 字幕样式配置,支持导入 ASS 文件并提取样式,
    导出应用到字幕生成管线。
    """

    __tablename__ = "subtitle_templates"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="模板名称(用户自定义)")
    description: str | None = Field(default=None, description="模板描述")

    # ASS [V4+ Styles] 字段:Fontname Fontsize PrimaryColour SecondaryColour OutlineColour BackColour Bold Italic Underline StrikeOut ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow Alignment MarginL MarginR MarginV Encoding
    font_name: str = Field(default="Noto Sans SC", description="字体名称")
    font_size: int = Field(default=36, description="字体大小")
    primary_color: str = Field(default="&H00FFFFFF", description="主要颜色(ABGR)")
    secondary_color: str = Field(default="&H000000FF", description="次要颜色")
    outline_color: str = Field(default="&H00000000", description="轮廓颜色")
    back_color: str = Field(default="&H80000000", description="阴影颜色")
    bold: int = Field(default=0, description="粗体 0/-1")
    italic: int = Field(default=0, description="斜体 0/-1")
    underline: int = Field(default=0, description="下划线 0/-1")
    strikeout: int = Field(default=0, description="删除线 0/-1")
    scale_x: int = Field(default=100, description="横向缩放%")
    scale_y: int = Field(default=100, description="纵向缩放%")
    spacing: int = Field(default=0, description="字间距像素")
    angle: float = Field(default=0.0, description="旋转角度")
    border_style: int = Field(default=1, description="边框样式:1=轮廓+阴影,3=不透明背景")
    outline: float = Field(default=2.0, description="轮廓宽度")
    shadow: float = Field(default=2.0, description="阴影深度")
    alignment: int = Field(default=2, description="对齐:1/2/3=底部,5/6/7=顶部,9/10/11=中部")
    margin_l: int = Field(default=20, description="左边距像素")
    margin_r: int = Field(default=20, description="右边距像素")
    margin_v: int = Field(default=20, description="垂直边距像素")
    encoding: int = Field(default=1, description="编码:0=ANSI,1=Default,134=GB2312")

    # 扩展:字幕行为配置
    max_chars_per_line: int = Field(default=30, description="每行最大字数(适用于中文)")
    min_display_ms: int = Field(default=800, description="最短显示时长(毫秒)")
    max_display_ms: int = Field(default=5000, description="最长显示时长(毫秒)")
    line_gap_ms: int = Field(default=200, description="行间间隔(毫秒)")

    # ASS 分辨率
    play_res_x: int = Field(default=1920, description="播放分辨率-宽")
    play_res_y: int = Field(default=1080, description="播放分辨率-高")

    # 原始样式文本(用于完整导入/导出)
    raw_style_line: str | None = Field(default=None, description="原始 ASS [V4+ Styles] 行文本")
    raw_format_line: str | None = Field(default=None, description="原始 ASS Format 行文本")

    is_default: bool = Field(default=False, description="是否为默认模板")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class IntroTemplate(SQLModel, table=True):
    """片头/片尾模板(``intro_templates``)

    V0.1.8 P1.2:存储片头/片尾的文字与样式配置。
    支持模板变量:``{streamer_name}``,``{date}``,``{time}``,``{game_name}``,``{room_title}``。
    """

    __tablename__ = "intro_templates"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="模板名称")

    # 片头。
    intro_enabled: bool = Field(default=True, description="是否启用片头")
    intro_text: str = Field(default="{streamer_name} · {date}", description="片头文字(支持变量)")
    intro_duration_s: float = Field(default=3.0, description="片头时长(秒)")
    intro_font_name: str = Field(default="Noto Sans SC", description="片头字体")
    intro_font_size: int = Field(default=48, description="片头字号")
    intro_font_color: str = Field(default="white", description="片头文字颜色")
    intro_bg_color: str = Field(default="black@0.6", description="片头背景色(支持透明度)")

    # 片尾。
    outro_enabled: bool = Field(default=True, description="是否启用片尾")
    outro_text: str = Field(default="感谢观看", description="片尾文字(支持变量)")
    outro_duration_s: float = Field(default=2.0, description="片尾时长(秒)")
    outro_font_name: str = Field(default="Noto Sans SC", description="片尾字体")
    outro_font_size: int = Field(default=48, description="片尾字号")
    outro_font_color: str = Field(default="white", description="片尾文字颜色")
    outro_bg_color: str = Field(default="black@0.6", description="片尾背景色")

    is_default: bool = Field(default=False, description="是否为默认模板")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
