"""数据库实体 — Settings."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import utcnow


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


class SubtitleTemplate(SQLModel, table=True):
    """ASS 字幕样式模板(``subtitle_templates``)

    V0.1.8 P0:存储自定义 ASS 字幕样式配置,支持导入 ASS 文件并提取样式,
    导出应用到字幕生成管线。
    """

    __tablename__ = "subtitle_templates"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, description="模板名称(用户自定义)")
    description: str | None = Field(default=None, description="模板描述")

    # ASS [V4+ Styles] 字段:Fontname Fontsize PrimaryColour SecondaryColour OutlineColour BackColour Bold Italic Underline StrikeOut ScaleX ScaleY Spacing Angle BorderStyle Outline Shadow Alignment MarginL MarginR MarginV Encoding  # noqa: E501
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
