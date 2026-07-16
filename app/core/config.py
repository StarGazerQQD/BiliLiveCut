"""应用配置。

使用 ``pydantic-settings`` 从环境变量与 ``.env`` 文件加载配置,实现:

* 配置集中、类型安全;
* 密钥不写死在代码里(全部来自环境变量);
* 提供合理默认值,降低 MVP 上手成本。

通过模块级单例 :data:`settings` 在全工程复用。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项。

    字段与 ``.env.example`` 一一对应。环境变量名不区分大小写。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __repr__(self) -> str:
        """安全 repr — 对敏感字段值进行脱敏处理。"""
        from app.core.sanitize import sanitize_text

        raw = super().__repr__()
        return sanitize_text(raw)

    # ---------- 通用 ----------
    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    admin_password: str = Field(default="", repr=False)  # V0.1.8.2: Web 管理后台认证密码(空则无认证)

    # ---------- 存储 ----------
    storage_root: str = "./storage"
    database_url: str = "sqlite:///./storage/blc.db"

    # ---------- FFmpeg ----------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # ---------- 录制 / 分片 ----------
    segment_duration_s: int = Field(default=60, ge=5, le=600)
    preferred_stream_protocol: Literal["hls", "flv"] = "hls"
    stream_quality: int = 10000
    reconnect_max_backoff_s: int = Field(default=30, ge=1)
    live_poll_interval_s: int = Field(default=15, ge=5)
    collect_danmaku: bool = True  # 录制期间是否同时采集弹幕

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
    # 主引擎: paraformer / whisper, 默认 paraformer-zh
    asr_primary: str = "paraformer"
    # 辅助特征提取: SenseVoice-Small (情感/笑声/音乐/事件)
    asr_sensevoice: bool = True
    # 低置信度复核: Fun-ASR-Nano
    asr_funasr_review: bool = True
    # 最终兜底: Whisper (large-v3 / turbo), 保留切换
    asr_fallback_whisper: bool = True
    # 低置信度阈值 (logprob < 此值触发复核, V0.1.12.2 改为 review_risk_threshold)
    asr_confidence_threshold: float = -0.6
    # V0.1.12.2: 统一复核风险阈值 (0-1, review_risk_score >= 此值触发复核)
    asr_review_risk_threshold: float = 0.65
    # V0.1.12.2: SenseVoice 使用开关 (独立于模型加载开关 asr_sensevoice)
    asr_sensevoice_enabled: bool = True  # False=关闭辅助特征,不参与评分

    # ---------- V0.1.12.2: 分后端设备与并发控制 ----------
    asr_primary_device: str = "cpu"
    asr_auxiliary_device: str = "cpu"
    asr_review_device: str = "cpu"
    asr_fallback_device: str = "cpu"
    asr_primary_max_concurrency: int = 1
    asr_auxiliary_max_concurrency: int = 1
    asr_review_max_concurrency: int = 1
    asr_fallback_max_concurrency: int = 1
    # 模型生命周期
    asr_primary_keep_loaded: bool = True
    asr_auxiliary_keep_loaded: bool = False
    asr_review_keep_loaded: bool = False
    asr_fallback_keep_loaded: bool = False
    asr_model_idle_unload_seconds: int = 900
    asr_preload_on_start: bool = False
    # V0.1.12.2: 固定模型 revision (不再默认 master)
    asr_model_revision: str = "v2.0.4"

    # ---------- AI:大模型(OpenAI 兼容协议,境内推荐 DeepSeek/通义/Kimi/GLM) ----------
    # provider 仅作标识;真正决定连接的是 base_url + api_key + model。
    llm_provider: str = "deepseek"
    llm_api_key: str = Field(default="", repr=False)  # Deprecated: 已迁移至 LLMProvider 系统
    # OpenAI 兼容 API 的 base_url(须含 /v1 等版本前缀,视服务商而定):
    #   DeepSeek: https://api.deepseek.com/v1
    #   通义千问 : https://dashscope.aliyuncs.com/compatible-mode/v1
    #   Kimi    : https://api.moonshot.cn/v1
    #   智谱 GLM : https://open.bigmodel.cn/api/paas/v4
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    # 联网搜索:部分服务商(通义/Kimi/GLM)支持,以 extra_body 传递开关键名。
    # 例如通义/Kimi 用 "enable_search";留空表示不尝试联网搜索参数。
    llm_web_search_param: str = "enable_search"
    # 成本护栏(每百万 token 价格,单位随你填的币种;设 0 表示不计费、不限额)。
    llm_price_input_per_m: float = 0.0
    llm_price_output_per_m: float = 0.0
    llm_daily_budget: float = 0.0

    # 兼容旧配置:若未填 llm_* 而填了 anthropic_*,仍可回退读取(已废弃,仅为兼容保留)。
    anthropic_api_key: str = Field(default="", repr=False)
    # Deprecated: 已迁移至多 LLM 供应商系统 (app/analysis/llm_providers.py)
    anthropic_model: str = ""  # Deprecated: 未使用,仅保留向后兼容
    llm_daily_budget_usd: float = 0.0

    # ---------- 网感资料库(联网采集热门内容,供评分/文案参考) ----------
    trend_enabled: bool = False  # 是否启用网感资料库(默认关闭,按需开启)
    # 趋势采集专用 API 配置(独立于通用 LLM,可指定不同的模型/服务商)。
    # 留空则回退到通用 LLM 配置(多模型列表或 .env LLM_* 单模型)。
    trend_api_key: str = Field(default="", repr=False)  # 趋势采集专用 API Key
    trend_base_url: str = ""  # 趋势采集专用 base_url(OpenAI 兼容)
    trend_model: str = ""  # 趋势采集专用模型名(留空则用 llm_model)
    trend_web_search: bool = True  # 是否启用联网搜索工具采集(关闭则仅靠模型知识)
    trend_max_searches: int = Field(default=5, ge=1, le=20)  # 单次采集最多联网搜索次数
    trend_max_items: int = Field(default=40, ge=1, le=200)  # 单次采集解析条目上限
    trend_retention_days: int = Field(default=14, ge=1)  # 资料库保留天数
    trend_match_days: int = Field(default=7, ge=1)  # 高光/文案参考的"近期"窗口(天)

    # ---------- 高光阈值 ----------
    highlight_init_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    highlight_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    auto_publish_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # ---------- V0.1.14.8: ML 高光模型 ----------
    ml_model_enabled: bool = False            # 是否启用 ML 模型替代规则+LLM
    ml_shadow_mode: bool = True               # Shadow 模式:同时跑 ML+规则,只记录差异不替代
    ml_auto_learn: bool = True                # 审批反馈后是否自动触发重训练
    ml_auto_learn_cooldown_min: int = 30      # 自动重训练冷却时间(分钟)
    ml_min_new_samples: int = 5               # 至少新增 N 条反馈才触发自动重训练
    ml_confidence_threshold: float = 0.8      # ML 预测阈值,高于此值可自动批准

    # ---------- 切片后处理 ----------
    clip_loudnorm: bool = True  # 响度标准化(EBU R128)
    clip_remove_silence: bool = False  # 去除首尾静默
    clip_vertical: bool = False  # 竖屏重构(1080x1920,居中+黑边)
    clip_subtitle: bool = False  # 烧录字幕(从转写生成)
    clip_max_duration_s: int = Field(default=180, ge=5, le=900)
    clip_video_crf: int = Field(default=20, ge=0, le=51)  # x264 质量,越小越好
    clip_preset: str = "veryfast"  # x264 编码速度档

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

    # ---------- 上传 ----------
    uploader: str = "manual"  # 默认上传器(manual 时不触碰平台接口)
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

    # 邮件通知(SMTP)。
    smtp_host: str = ""  # SMTP 服务器
    smtp_port: int = 465  # SMTP 端口(默认 SSL 465)
    smtp_user: str = ""  # SMTP 用户名
    smtp_password: str = Field(default="", repr=False)  # SMTP 密码 (repr=False 防止日志泄露)
    smtp_from: str = ""  # 发件人地址
    smtp_to: str = ""  # 收件人地址(多个用逗号分隔)

    # 通知规则。
    notify_on_clip: bool = True  # 切片完成时通知
    notify_on_upload: bool = False  # 投稿完成时通知
    notify_on_disk_alert: bool = True  # 磁盘不足时通知
    notify_on_error: bool = True  # 任务永久失败时通知
    disk_alert_threshold_gb: int = 10  # 磁盘告警阈值(GB)

    @model_validator(mode="after")
    def validate_cross_fields(self) -> Settings:
        """跨字段校验,在模型完成字段级别验证后执行。

        检查逻辑约束(如磁盘告警阈值应小于最小保留空间)
        以及格式约束(如上传命令模板须包含 ``{file}`` 占位符)。

        :returns: 校验通过的 ``self``。
        :raises ValueError: 校验失败时抛出含描述性信息的异常。
        """
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

        # 磁盘告警阈值应 <= 最小保留空间,确保告警在磁盘不足之前触发
        if self.disk_alert_threshold_gb > self.min_free_disk_gb:
            raise ValueError(
                f"disk_alert_threshold_gb ({self.disk_alert_threshold_gb} GB)"
                f" 必须 <= min_free_disk_gb ({self.min_free_disk_gb} GB),"
                f" 确保磁盘告警在空间不足之前触发"
            )

        # biliup_upload_cmd 如果非空,必须包含 {file} 占位符
        if self.biliup_upload_cmd and "{file}" not in self.biliup_upload_cmd:
            raise ValueError(f"biliup_upload_cmd 必须包含 {{file}} 占位符,当前值: {self.biliup_upload_cmd}")

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
settings: Settings = get_settings()
