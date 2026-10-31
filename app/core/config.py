"""应用配置。

使用 ``pydantic-settings`` 从环境变量与 ``.env`` 文件加载配置,实现:

* 配置集中、类型安全;
* 密钥来自部署环境或数据库中的显式覆盖，不写死在代码里;
* 提供合理默认值,降低 MVP 上手成本。

通过稳定代理 :data:`settings` 读取有效配置；业务任务使用开始时的完整快照。
"""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_network
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.capacity import DEFAULT_OUTPUT_TOKENS
from app.models.contracts import MAX_CONFIGURABLE_TOKENS


class Settings(BaseSettings):
    """全局配置项。

    字段与 ``.env.example`` 一一对应。环境变量名不区分大小写。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    def __repr__(self) -> str:
        """安全 repr — 对敏感字段值进行脱敏处理。"""
        from app.core.sanitize import sanitize_text

        raw = super().__repr__()
        return sanitize_text(raw)

    # ---------- 通用 ----------
    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    admin_password: str = Field(default="", repr=False)  # Web 后台显式密码；Docker 可使用网页初始化
    docker_auth_enabled: bool = False  # 由容器入口启用，源码和 Portable 默认关闭
    docker_auth_idle_seconds: int = Field(default=1800, ge=60, le=86400)
    docker_auth_session_seconds: int = Field(default=43200, ge=300, le=604800)
    reviewer_accounts_json: str = Field(
        default="",
        repr=False,
        description='审核员账号 JSON，例如 {"reviewer":"password"}',
    )
    review_claim_ttl_s: int = Field(default=900, ge=60, le=86400)
    review_blind_mode: bool = True
    frontend_trusted_proxy_ips: str = Field(default="", description="前端信任的外层代理 IP 或 CIDR，逗号分隔，重启生效")

    # ---------- 存储 ----------
    storage_root: str = "./storage"
    database_url: str = "sqlite:///./storage/blc.db"
    plugin_dir: str = "./storage/plugins"

    # ---------- FFmpeg ----------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # ---------- 录制 / 分片 ----------
    # FFmpeg 按关键帧边界完成分段，因此 300 秒是目标值，实际片长会有小幅浮动。
    segment_duration_s: int = Field(default=300, ge=5, le=600)
    preferred_stream_protocol: Literal["hls", "flv"] = "hls"
    stream_quality: int = Field(default=10000, ge=1)
    reconnect_max_backoff_s: int = Field(default=30, ge=1)
    live_poll_interval_s: int = Field(
        default=15,
        ge=5,
        description="本地守候配置刷新间隔，同时用于没有推送能力的来源状态查询；Bilibili 开播由事件驱动。",
    )
    room_metadata_refresh_interval_s: int = Field(default=30, ge=5, le=3600)
    room_metadata_refresh_timeout_s: float = Field(default=8.0, ge=0.1, le=30.0)
    # 连续无法恢复录制的最大失败次数；0=单项关闭，两项均0仍以20次保护。
    recording_reconnect_max_attempts: int = Field(default=20, ge=0, le=10000)
    # 从取流尝试开始计算的连续故障预算（秒）；健康媒体持续推进不受此寿命限制，0=单项关闭。
    recording_reconnect_max_elapsed_s: int = Field(default=180, ge=0, le=86400)
    # 多线路真实拉流竞争；1=串行，胜出进程继续录制而不重新建立连接。
    recording_stream_race_parallel: int = Field(default=4, ge=1, le=16)
    recording_stream_race_timeout_s: float = Field(
        default=30.0,
        ge=3.0,
        le=120.0,
        description="候选首次出流与录制中无媒体推进的最长等待秒数；实际输出增长会刷新无进度计时。",
    )
    # 自动监控连续离线确认、延迟收尾和单场录制时长保护。
    live_offline_confirm_count: int = Field(
        default=3,
        ge=1,
        le=100,
        description="仅供没有推送能力的来源连续确认下播；明确下播事件直接进入延迟收尾和状态复核。",
    )
    live_session_end_delay_s: int = Field(default=60, ge=0, le=3600)
    recording_max_duration_s: int = Field(default=43200, ge=300, le=604800)
    collect_danmaku: bool = Field(
        default=True,
        description="录制期间是否采集并保存弹幕；关闭后，自动录制仍使用独立连接监听开下播事件。",
    )
    # 单场录制使用登录 Cookie 获取弹幕 token 的最大失败次数（首次计入）；0=始终匿名。
    danmaku_login_retry_max_attempts: int = Field(default=5, ge=0, le=100)
    # 登录访问失败后，保持匿名采集并按此间隔后台重试登录链路。
    danmaku_login_retry_interval_s: float = Field(default=60.0, ge=1.0, le=3600.0)
    # 未显式指定 --pipeline/--no-pipeline 时，录制是否启用实时转写与高光分析。
    recording_pipeline_enabled: bool = True

    # ---------- Bilibili 合规 ----------
    require_authorization: bool = True
    bilibili_cookie: str = Field(default="", repr=False)

    # ---------- AI:语音转写(本地 Whisper,境内可用) ----------
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    # V0.1.13: ASR 资源检查策略 — "strict"=资源不足抛异常, "warn"=仅警告
    asr_resource_policy: Literal["strict", "warn"] = "warn"

    # ---------- AI:多引擎 ASR 流水线 (V0.1.12) ----------
    # 只有显式选择 openai_compatible 才上传音频到远程转写服务。
    asr_primary: Literal["funasr_nano", "paraformer", "whisper", "openai_compatible"] = "funasr_nano"
    asr_remote_route_id: str = Field(
        default="",
        max_length=128,
        pattern=r"^[^\x00-\x20\x7F]*$",
        description="远程转写业务路由；留空使用下方独立服务配置。仅选择远程主引擎时上传音频。",
    )
    asr_remote_base_url: str = Field(
        default="",
        max_length=2048,
        description="独立远程转写基础地址（含 /v1 等前缀）；未选择业务路由时使用。",
    )
    asr_remote_api_key: str = Field(
        default="",
        max_length=4096,
        pattern=r"^[!-~]*$",
        repr=False,
        description="独立远程转写 Bearer 密钥；选择业务路由后使用连接内的密钥，无认证服务可留空。",
    )
    asr_remote_model: str = Field(
        default="",
        max_length=200,
        pattern=r"^[^\x00-\x1F\x7F]*$",
        description="独立配置的转写模型 ID；选择业务路由后使用路由模型。",
    )
    asr_remote_language: str = Field(
        default="", pattern=r"^(?:[a-z]{2,3})?$", description="ISO 639 语言码，如 zh；留空由服务识别。"
    )
    asr_remote_response_format: Literal["json", "verbose_json"] = Field(
        default="json", description="默认 json；仅支持词/句时间戳的服务使用 verbose_json。"
    )
    asr_remote_timeout_s: float = Field(
        default=120.0, ge=1.0, le=600.0, description="远程请求读写超时秒数；总时限仍由任务推理预算控制。"
    )
    asr_remote_chunk_s: int = Field(
        default=300, ge=1, le=600, description="远程音频分块秒数，单块最多 600 秒且低于 20 MB。"
    )
    # 辅助特征提取: SenseVoice-Small (情感/笑声/音乐/事件)
    asr_sensevoice: bool = True
    # 低置信度复核: Fun-ASR-Nano
    asr_funasr_review: bool = True
    # 最终兜底: Whisper (large-v3 / turbo), 保留切换
    asr_fallback_whisper: bool = True
    # V0.1.12.2: 统一复核风险阈值 (0-1, review_risk_score >= 此值触发复核)
    asr_review_risk_threshold: float = 0.65
    # V0.1.12.2: SenseVoice 使用开关 (独立于模型加载开关 asr_sensevoice)
    asr_sensevoice_enabled: bool = True  # False=关闭辅助特征,不参与评分
    # FunASR 长音频先由 FSMN-VAD 切为短句，避免生成式解码器处理五分钟单句。
    asr_vad_max_segment_s: int = Field(default=30, ge=5, le=120)
    # 同时执行的分段 ASR 任务数；实际模型实例由共享池按角色并发上限分配。
    asr_task_max_concurrency: int = Field(default=1, ge=1, le=8)
    asr_inference_base_timeout_s: int = Field(default=600, ge=30, le=7200)
    asr_inference_timeout_rtf: float = Field(default=10.0, ge=1, le=100)
    asr_inference_max_timeout_s: int = Field(default=7200, ge=60, le=43200)

    # ---------- V0.1.12.2: 分后端设备与并发控制 ----------
    asr_primary_device: str = "cpu"
    asr_auxiliary_device: str = "cpu"
    asr_review_device: str = "cpu"
    asr_fallback_device: str = "cpu"
    asr_primary_max_concurrency: int = Field(default=1, ge=1, le=8)
    asr_auxiliary_max_concurrency: int = Field(default=1, ge=1, le=8)
    asr_review_max_concurrency: int = Field(default=1, ge=1, le=8)
    asr_fallback_max_concurrency: int = Field(default=1, ge=1, le=8)
    # 模型生命周期
    asr_primary_keep_loaded: bool = True
    asr_auxiliary_keep_loaded: bool = False
    asr_review_keep_loaded: bool = False
    asr_fallback_keep_loaded: bool = False
    asr_model_idle_unload_seconds: int = Field(default=900, ge=0, le=86400)
    asr_preload_on_start: bool = False

    # ---------- AI:大模型(OpenAI 兼容协议,境内推荐 DeepSeek/通义/Kimi/GLM) ----------
    llm_daily_budget: float = Field(default=0.0, ge=0.0)
    llm_request_timeout_s: float = Field(default=600.0, ge=1.0, le=3600.0)
    editorial_model_max_attempts: int = Field(default=8, ge=1, le=32)
    editorial_model_deadline_s: float = Field(default=1800.0, ge=1.0, le=14400.0)
    # 每个录制切片完成 ASR 后，是否调用 LLM 整理正文并生成摘要。
    transcript_llm_refine_enabled: bool = True
    transcript_llm_refine_max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=128, le=MAX_CONFIGURABLE_TOKENS)
    # 高光复核需要同时容纳五分钟转写、模型推理和结构化判断结果。
    highlight_llm_max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=512, le=MAX_CONFIGURABLE_TOKENS)
    # EventEnricher 解释完整证据束并返回严格结构化 JSON。
    hotspot_enrichment_llm_max_tokens: int = Field(default=DEFAULT_OUTPUT_TOKENS, ge=512, le=MAX_CONFIGURABLE_TOKENS)

    # ---------- 网感资料库(联网采集热门内容,供评分/文案参考) ----------
    trend_enabled: bool = False  # 是否启用网感资料库(默认关闭,按需开启)
    # 未保存独立业务路由时兼容原热点专用配置，留空按旧通用列表回退。
    trend_api_key: str = Field(
        default="", repr=False, description="原热点专用密钥；保存独立业务路由后使用热点路由连接。"
    )
    trend_base_url: str = Field(default="", description="原热点专用基础地址；保存独立业务路由后使用热点路由连接。")
    trend_model: str = Field(default="", description="原热点专用模型；保存独立业务路由后使用热点路由模型。")
    trend_web_search_param: str = Field(
        default="enable_search", description="原热点专用搜索参数；保存独立业务路由后使用路由参数。"
    )
    trend_web_search: bool = True  # 是否启用联网搜索工具采集(关闭则仅靠模型知识)
    trend_max_items: int = Field(default=12, ge=1, le=200)  # 单次采集解析条目上限
    trend_retention_days: int = Field(default=14, ge=1)  # 资料库保留天数
    trend_match_days: int = Field(default=7, ge=1)  # 高光/文案参考的"近期"窗口(天)

    # ---------- 高光阈值 ----------
    highlight_init_threshold: float = Field(default=0.28, ge=0.0, le=1.0)
    highlight_threshold: float = Field(default=0.38, ge=0.0, le=1.0)
    highlight_review_threshold: float = Field(default=0.32, ge=0.0, le=1.0)
    highlight_auto_approve_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    highlight_max_candidates_per_segment: int = Field(default=4, ge=1, le=12)
    highlight_peak_min_distance_s: float = Field(default=25.0, ge=5.0, le=300.0)
    highlight_min_pre_roll_s: float = Field(default=20.0, ge=0.0, le=180.0)
    highlight_min_post_roll_s: float = Field(default=30.0, ge=0.0, le=180.0)
    # B 站弹幕接收时间通常晚于画面爆点；评分查询窗口向后平移此秒数。
    danmaku_event_lag_s: float = Field(default=7.5, ge=0.0, le=60.0)
    # Event-first 热点检测器的统一时间参数；三者必须按 bucket 整数对齐。
    hotspot_bucket_s: float = Field(default=10.0, ge=5.0, le=10.0)
    hotspot_baseline_window_s: float = Field(default=90.0, ge=60.0, le=120.0)
    hotspot_detector_tick_s: float = Field(default=20.0, ge=15.0, le=30.0)
    hotspot_min_baseline_buckets: int = Field(default=3, ge=2, le=12)
    hotspot_detection_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    # 事件级协调：跨分段合并、稳定确认与录制连续性边界。
    hotspot_event_merge_gap_s: float = Field(default=30.0, ge=0.0, le=120.0)
    hotspot_event_confirm_delay_s: float = Field(default=60.0, ge=0.0, le=600.0)
    hotspot_event_semantic_overlap_threshold: float = Field(default=0.20, ge=0.0, le=1.0)
    hotspot_recording_gap_tolerance_s: float = Field(default=1.0, ge=0.0, le=10.0)
    # 热点局部 ASR 先于普通/历史完整转写；数值越小的任务越先被领取。
    hotspot_asr_enabled: bool = True
    hotspot_asr_pre_roll_s: float = Field(default=35.0, ge=0.0, le=180.0)
    hotspot_asr_post_roll_s: float = Field(default=55.0, ge=0.0, le=180.0)
    hotspot_asr_priority: int = Field(default=10, ge=0, le=1000)
    near_live_asr_priority: int = Field(default=50, ge=0, le=1000)
    background_asr_priority: int = Field(default=100, ge=0, le=1000)
    auto_publish_threshold: float = Field(default=0.80, ge=0.0, le=1.0)

    # ---------- 切片后处理 ----------
    clip_loudnorm: bool = True  # 响度标准化(EBU R128)
    clip_remove_silence: bool = False  # 去除首尾静默
    clip_vertical: bool = False  # 竖屏重构(1080x1920,居中+黑边)
    clip_subtitle: bool = False  # 烧录字幕(从转写生成)
    clip_danmaku: bool = Field(default=False, description="烧录已采集或导入的普通弹幕，与转写字幕独立。")
    clip_danmaku_mode: Literal["scroll", "top", "bottom"] = Field(
        default="scroll", description="滚动、顶部或下方固定弹幕。"
    )
    clip_danmaku_font_size: int = Field(default=36, ge=12, le=120, description="以 1080 高画面为基准的弹幕字号。")
    clip_danmaku_color: str = Field(
        default="FFFFFF", pattern=r"^[0-9A-Fa-f]{6}$", description="六位 RGB 十六进制颜色，不含 #。"
    )
    clip_danmaku_opacity: float = Field(
        default=0.85, ge=0.0, le=1.0, description="弹幕不透明度，0 为透明，1 为不透明。"
    )
    clip_danmaku_duration_s: float = Field(default=6.0, ge=2.0, le=20.0, description="单条弹幕显示秒数。")
    clip_danmaku_max_lines: int = Field(
        default=8, ge=1, le=20, description="最大弹幕行数；满行时舍弃新弹幕，底部保留字幕区。"
    )
    clip_max_duration_s: int = Field(default=180, ge=5, le=900)
    clip_video_crf: int = Field(default=20, ge=0, le=51)  # x264 质量,越小越好
    clip_video_encoder: Literal["cpu", "nvidia", "intel", "amd"] = Field(
        default="cpu", description="视频编码设备：cpu 关闭硬件编码；其余选项需要对应显卡、驱动及 FFmpeg 编码器。"
    )
    clip_hardware_quality: int = Field(
        default=23, ge=1, le=51, description="硬件编码质量值，越小通常越清晰；与 CPU CRF 不等价。"
    )
    clip_hardware_preset: Literal["speed", "balanced", "quality"] = Field(
        default="balanced", description="硬件编码速度档：speed 优先速度、balanced 均衡、quality 优先质量。"
    )
    clip_preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"
    ] = "veryfast"  # x264 编码速度档

    # ---------- V0.1.2 新增:高级功能 ----------
    # 阈值自学习:收集 N 条审批反馈后自动调整房间级阈值。
    threshold_learning_min_samples: int = Field(default=10, ge=3, description="最少反馈样本数才触发调参")
    threshold_learning_max_delta: float = Field(default=0.1, ge=0.01, le=0.3, description="单次阈值调整最大幅度")
    # 录制预约检查间隔(秒)。
    schedule_check_interval_s: int = Field(default=30, ge=10, le=300)
    # 录制自动恢复:启动时检查最近 N 小时内是否有中断的会话。
    auto_recover_max_age_hours: int = Field(default=24, ge=1, le=72)

    # ---------- V0.1.7 P3:磁盘保护与存储生命周期 ----------
    min_free_disk_gb: float = Field(default=10.0, ge=1.0, description="最低剩余磁盘空间(GB),低于此值暂停高风险任务")
    raw_retention_days: int = Field(default=7, ge=1, le=90, description="原始录像保留天数")
    clip_cleanup_delay_hours: int = Field(default=24, ge=1, le=720, description="成片成功后原始分段延迟清理(小时)")

    # 后台调度、恢复与磁盘保护；保留已有 ENV 名称。
    max_analyzing: int = Field(default=2, ge=1, le=16)
    max_rendering: int = Field(default=2, ge=1, le=16)
    max_publishing: int = Field(default=1, ge=1, le=8)
    worker_shutdown_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    stale_timeout_s: int = Field(default=120, ge=60, le=86400)
    upload_attempt_stale_s: int = Field(default=600, ge=60, le=86400)
    low_disk_threshold_gb: float = Field(default=20.0, ge=1)
    critical_disk_threshold_gb: float = Field(default=5.0, ge=0.1)

    # ---------- 上传 ----------
    upload_max_retries: int = Field(default=3, ge=0, le=10)
    upload_max_per_hour: int = Field(default=5, ge=1)  # 投稿频率上限(每小时)
    title_max_len: int = Field(default=80, ge=10, le=200)
    desc_max_len: int = Field(default=2000, ge=10)
    # biliup(社区方案,自担风险):凭据/配置路径与自定义上传命令模板。
    # 留空时 Biliup 上传器会以清晰提示失败,不会崩溃。
    biliup_config: str = ""
    # 自定义上传命令模板,支持占位符 {file} {title} {desc}。例如:
    # biliup_upload_cmd=biliup upload "{file}" --title "{title}"
    biliup_upload_cmd: str = ""

    # ---------- 通知/Webhook (V0.1.8 P2) ----------
    notify_enabled: bool = False  # 是否启用通知

    # 钉钉机器人 Webhook。
    dingtalk_webhook: str = ""  # 钉钉机器人 Webhook 地址
    dingtalk_secret: str = Field(default="", repr=False)  # 钉钉机器人加签密钥(可选)

    # 企业微信机器人 Webhook。
    wecom_webhook: str = ""  # 企业微信机器人 Webhook 地址

    lark_app_id: str = Field(default="", max_length=128, pattern=r"^[^\s\x00-\x1f]*$")
    lark_app_secret: str = Field(default="", repr=False, max_length=1024, pattern=r"^[^\s\x00-\x1f]*$")
    lark_domain: Literal["feishu", "lark"] = "feishu"
    lark_receive_id: str = Field(default="", max_length=320, pattern=r"^[^\s\x00-\x1f]*$")
    lark_receive_id_type: Literal["chat_id", "open_id", "union_id", "user_id", "email"] = "chat_id"

    # 邮件通知(SMTP)。
    smtp_host: str = Field(default="", max_length=255, pattern=r"^[^\s/@\x00-\x1f]*$")
    smtp_port: int = Field(default=465, ge=1, le=65535)  # SMTP 端口(默认 SSL 465)
    smtp_security: Literal["ssl", "starttls", "plain"] = Field(default="ssl", description="SMTP 连接加密方式。")
    smtp_user: str = Field(default="", max_length=320, pattern=r"^[^\x00-\x1f]*$")
    smtp_password: str = Field(default="", repr=False, max_length=4096)
    smtp_from: str = ""  # 发件人地址，留空使用 SMTP 用户名。
    smtp_to: str = Field(default="", description="接收提醒的单个邮箱，必须通过验证码验证。")
    notify_on_connection: bool = Field(default=True, description="Cookie 失效或 WS 连续连接失败时提醒。")
    notify_connection_cooldown_s: int = Field(default=1800, ge=60, le=86400, description="同房间同类连接提醒间隔。")

    # 通知规则。
    notify_on_clip: bool = True  # 切片完成时通知
    notify_on_upload: bool = False  # 投稿完成时通知
    notify_on_disk_alert: bool = True  # 磁盘不足时通知
    notify_on_error: bool = True  # 任务永久失败时通知
    disk_alert_threshold_gb: int = 10  # 磁盘告警阈值(GB)

    @field_validator("frontend_trusted_proxy_ips")
    @classmethod
    def validate_frontend_trusted_proxy_ips(cls, value: str) -> str:
        """显式登记可信网络；空值保持不信任任何外层转发头。"""
        if not value.strip():
            return ""
        try:
            return ",".join(str(ip_network(item.strip(), strict=False)) for item in value.split(","))
        except ValueError as exc:
            raise ValueError("frontend_trusted_proxy_ips 必须为 IP 或 CIDR 列表") from exc

    @field_validator("smtp_from", "smtp_to")
    @classmethod
    def validate_mailbox(cls, value: str) -> str:
        """配置只支持一个已验证收件人，拒绝邮件头和多地址。"""
        from app.core.email_address import mailbox

        return mailbox(value)

    @field_validator("asr_remote_base_url")
    @classmethod
    def validate_asr_remote_base_url(cls, value: str) -> str:
        """允许显式配置本机或远端服务，拒绝凭据、查询参数和控制字符。"""
        if not value:
            return value
        if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
            raise ValueError("asr_remote_base_url 含空白或非法字符")
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError as exc:
            raise ValueError("asr_remote_base_url 地址无效") from exc
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ValueError("asr_remote_base_url 必须是无凭据、查询参数和片段的 HTTP(S) 基础地址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_cross_fields(self) -> Settings:
        """跨字段校验,在模型完成字段级别验证后执行。

        检查逻辑约束(如磁盘告警应在最低空间保护之前触发)
        以及格式约束(如上传命令模板须包含 ``{file}`` 占位符)。

        :returns: 校验通过的 ``self``。
        :raises ValueError: 校验失败时抛出含描述性信息的异常。
        """
        if (
            self.asr_primary == "openai_compatible"
            and not self.asr_remote_route_id
            and (not self.asr_remote_base_url or not self.asr_remote_model.strip())
        ):
            raise ValueError(
                "openai_compatible 需要同时配置 asr_remote_base_url 和 asr_remote_model，或选择远程转写路由"
            )

        # asr_review_risk_threshold 必须在 [0, 1] 范围内
        if not (0.0 <= self.asr_review_risk_threshold <= 1.0):
            raise ValueError(
                f"asr_review_risk_threshold 必须在 0.0 ~ 1.0 之间,当前值: {self.asr_review_risk_threshold}"
            )

        # clip_max_duration_s 必须大于 5 秒
        if self.clip_max_duration_s <= 5:
            raise ValueError(f"clip_max_duration_s 必须大于 5 秒,当前值: {self.clip_max_duration_s}")

        # upload_max_retries 必须 >= 0
        if self.upload_max_retries < 0:
            raise ValueError(f"upload_max_retries 必须 >= 0,当前值: {self.upload_max_retries}")

        # upload_max_per_hour 必须 >= 1
        if self.upload_max_per_hour < 1:
            raise ValueError(f"upload_max_per_hour 必须 >= 1,当前值: {self.upload_max_per_hour}")

        if (
            self.critical_disk_threshold_gb > self.min_free_disk_gb
            or self.min_free_disk_gb > self.low_disk_threshold_gb
        ):
            raise ValueError("磁盘阈值必须满足 critical <= min_free <= low")
        if self.disk_alert_threshold_gb < self.min_free_disk_gb:
            raise ValueError("磁盘告警阈值必须 >= min_free_disk_gb")

        # biliup_upload_cmd 如果非空,必须包含 {file} 占位符
        if self.biliup_upload_cmd and "{file}" not in self.biliup_upload_cmd:
            raise ValueError("biliup_upload_cmd 必须包含 {file} 占位符")

        baseline_ratio = self.hotspot_baseline_window_s / self.hotspot_bucket_s
        tick_ratio = self.hotspot_detector_tick_s / self.hotspot_bucket_s
        if not baseline_ratio.is_integer():
            raise ValueError("hotspot_baseline_window_s 必须是 hotspot_bucket_s 的整数倍")
        if not tick_ratio.is_integer():
            raise ValueError("hotspot_detector_tick_s 必须是 hotspot_bucket_s 的整数倍")
        if self.hotspot_min_baseline_buckets >= int(baseline_ratio):
            raise ValueError("hotspot_min_baseline_buckets 必须小于滚动基线的 bucket 数")
        if not (self.hotspot_asr_priority < self.near_live_asr_priority < self.background_asr_priority):
            raise ValueError(
                "ASR 优先级必须满足 hotspot_asr_priority < near_live_asr_priority < background_asr_priority"
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级缓存的配置单例。

    使用 ``lru_cache`` 保证只解析一次环境变量,便于测试时通过
    ``get_settings.cache_clear()`` 重置。

    :returns: 已加载的 :class:`Settings` 实例。
    """
    return Settings()


# 便捷别名:大多数模块直接 ``from app.core.config import settings`` 即可。
from app.core.runtime_settings import SettingsProxy  # noqa: E402

settings = cast(Settings, SettingsProxy())
