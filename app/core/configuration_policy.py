# ruff: noqa: D100
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

BOOTSTRAP = {
    "frontend_trusted_proxy_ips": "前端外层代理信任来自部署环境，修改后重启独立前端。",
    "database_url": "数据库连接必须在数据库打开前确定；通过部署环境配置。",
    "storage_root": "存储路径在启动时确定；变更需迁移现有数据并重启。",
    "plugin_dir": "插件目录由启动时的插件发现确定；修改部署配置后重启。",
    "app_env": "运行环境由部署配置确定，重启后生效。",
    "log_level": "日志输出在启动时配置；修改部署配置后重启。",
    "admin_password": "管理员凭据影响服务绑定与认证；在部署配置中修改后重启。",
    "docker_auth_enabled": "Docker 登录模式由容器入口启用，不可在业务配置中关闭。",
    "docker_auth_idle_seconds": "会话空闲期限属于部署配置，重建容器后生效。",
    "docker_auth_session_seconds": "会话最长有效期属于部署配置，重建容器后生效。",
    "reviewer_accounts_json": "审核员凭据在启动时加载；在部署配置中修改后重启。",
}
RESTART_KEYS = frozenset({"asr_preload_on_start", "auto_recover_max_age_hours"})
IMMEDIATE_KEYS = frozenset({"notify_enabled", "review_blind_mode"})
POLL_KEYS = frozenset(
    {
        "live_poll_interval_s",
        "live_offline_confirm_count",
        "live_session_end_delay_s",
        "recording_max_duration_s",
        "schedule_check_interval_s",
        "max_analyzing",
        "max_rendering",
        "max_publishing",
        "critical_disk_threshold_gb",
        "low_disk_threshold_gb",
        "disk_alert_threshold_gb",
        "asr_model_idle_unload_seconds",
        "trend_schedule_enabled",
        "trend_schedule_start",
        "trend_schedule_end",
        "trend_schedule_interval_min",
    }
)
ROOM_DEFAULT_KEYS = frozenset(
    {
        "highlight_threshold",
        "highlight_review_threshold",
        "highlight_auto_approve_threshold",
        "auto_publish_threshold",
    }
)


@dataclass(frozen=True)
class ConfigurationPolicy:
    """配置保存与运行读取共用的生效契约。"""

    mode: Literal["immediate", "safe_boundary", "restart"]
    effect: str
    description: str
    current_read: bool = False


@lru_cache(maxsize=512)
def configuration_policy(key: str) -> ConfigurationPolicy:
    """返回真实读取边界；任务参数默认保留在本次运行快照中。"""
    if key in BOOTSTRAP:
        return ConfigurationPolicy("restart", "deployment", BOOTSTRAP[key])
    if key in RESTART_KEYS:
        return ConfigurationPolicy("restart", "restart", "重启应用进程后生效；当前进程继续使用启动时的值。")
    if key in IMMEDIATE_KEYS:
        return ConfigurationPolicy("immediate", "immediate", "保存后下一次读取生效；已发出的外部请求不会被撤销。", True)
    if key in POLL_KEYS or key.endswith(("_max_concurrency", "_keep_loaded")):
        return ConfigurationPolicy(
            "safe_boundary", "next_poll", "下一次调度、资源申请或空闲检查时使用新值；已有资源不会被强制回收。", True
        )
    if key in ROOM_DEFAULT_KEYS:
        return ConfigurationPolicy(
            "safe_boundary", "new_room", "新建房间使用新默认值；已有房间的独立设置和运行中任务的默认值快照保持不变。"
        )
    if key == "worker_shutdown_timeout_seconds":
        return ConfigurationPolicy(
            "safe_boundary", "next_shutdown", "下一次发起停止时确定等待期限，不改变已开始的关闭。", True
        )
    if key == "review_claim_ttl_s":
        return ConfigurationPolicy(
            "safe_boundary", "next_claim", "下一次领取审核任务时使用新期限，不延长已经发出的租约。", True
        )
    if key.startswith(
        ("recording_", "segment_", "stream_", "preferred_stream", "reconnect_", "room_metadata_")
    ) or key in {
        "collect_danmaku",
        "danmaku_login_retry_max_attempts",
        "danmaku_login_retry_interval_s",
        "bilibili_cookie",
    }:
        return ConfigurationPolicy(
            "safe_boundary", "next_recording", "新录制或新的独立操作使用新值；已有录制保留启动快照。"
        )
    return ConfigurationPolicy("safe_boundary", "next_task", "新任务或下一次独立操作使用新值；运行中单元保留原有快照。")
