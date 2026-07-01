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

from pydantic import Field
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

    # ---------- 通用 ----------
    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

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
    collect_danmaku: bool = True   # 录制期间是否同时采集弹幕

    # ---------- Bilibili 合规 ----------
    require_authorization: bool = True
    bilibili_cookie: str = ""

    # ---------- AI:语音转写(本地 Whisper,境内可用) ----------
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # ---------- AI:大模型(OpenAI 兼容协议,境内推荐 DeepSeek/通义/Kimi/GLM) ----------
    # provider 仅作标识;真正决定连接的是 base_url + api_key + model。
    llm_provider: str = "deepseek"
    llm_api_key: str = ""
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

    # 兼容旧配置:若未填 llm_* 而填了 anthropic_*,仍可回退读取(不推荐,境内多不可用)。
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    llm_daily_budget_usd: float = 0.0

    # ---------- 网感资料库(联网采集热门内容,供评分/文案参考) ----------
    trend_enabled: bool = False           # 是否启用网感资料库(默认关闭,按需开启)
    trend_model: str = ""                 # 联网搜索用模型(留空则用 anthropic_model)
    trend_web_search: bool = True         # 是否启用联网搜索工具采集(关闭则仅靠模型知识)
    trend_max_searches: int = Field(default=5, ge=1, le=20)  # 单次采集最多联网搜索次数
    trend_max_items: int = Field(default=40, ge=1, le=200)   # 单次采集解析条目上限
    trend_retention_days: int = Field(default=14, ge=1)      # 资料库保留天数
    trend_match_days: int = Field(default=7, ge=1)           # 高光/文案参考的"近期"窗口(天)

    # ---------- 高光阈值 ----------
    highlight_init_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    highlight_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    auto_publish_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # ---------- 切片后处理 ----------
    clip_loudnorm: bool = True            # 响度标准化(EBU R128)
    clip_remove_silence: bool = False     # 去除首尾静默
    clip_vertical: bool = False           # 竖屏重构(1080x1920,居中+黑边)
    clip_subtitle: bool = False           # 烧录字幕(从转写生成)
    clip_max_duration_s: int = Field(default=180, ge=5, le=900)
    clip_video_crf: int = Field(default=20, ge=0, le=51)   # x264 质量,越小越好
    clip_preset: str = "veryfast"         # x264 编码速度档

    # ---------- 上传 ----------
    uploader: str = "manual"                  # 默认上传器(manual 时不触碰平台接口)
    upload_max_retries: int = Field(default=3, ge=0, le=10)
    upload_max_per_hour: int = Field(default=5, ge=1)   # 投稿频率上限(每小时)
    title_max_len: int = Field(default=80, ge=10, le=200)
    desc_max_len: int = Field(default=2000, ge=10)
    # biliup(社区方案,自担风险):凭据/配置路径与自定义上传命令模板。
    # 留空时 Biliup 上传器会以清晰提示失败,不会崩溃。
    biliup_config: str = ""
    # 自定义上传命令模板,支持占位符 {file} {title} {desc}。例如:
    # biliup_upload_cmd=biliup upload "{file}" --title "{title}"
    biliup_upload_cmd: str = ""


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
