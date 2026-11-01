# ruff: noqa: D100
from __future__ import annotations

from dataclasses import dataclass

RESTART_OWNER_ENV = "_BLC_RESTART_OWNER"
CLEANUP_UNCONFIRMED_EXIT = 75
SERVICE_BIND_FAILED_EXIT = 78
# 后端允许的关闭配置上限为 3600 秒，外层额外保留所属树退出时间。
OUTER_SHUTDOWN_LIMIT_S = 3620.0
STARTUP_READY_LIMIT_S = 60.0
HTTP_UNAVAILABLE_LIMIT_S = 60.0
UNRESPONSIVE_SHUTDOWN_LIMIT_S = 30.0


@dataclass
class ServiceReadiness:
    """分别限制首次就绪等待和正常运行后的连续 HTTP 失联。"""

    started_at: float
    ready_once: bool = False
    unavailable_since: float | None = None

    def observe(self, *, ready: bool, now: float) -> str | None:
        """成功响应结束当前失联窗口，到达期限时返回明确故障原因。"""
        if ready:
            self.ready_once = True
            self.unavailable_since = None
            return None
        if not self.ready_once:
            return "服务首次 HTTP 就绪检查超时" if now - self.started_at >= STARTUP_READY_LIMIT_S else None
        if self.unavailable_since is None:
            self.unavailable_since = now
        if now - self.unavailable_since >= HTTP_UNAVAILABLE_LIMIT_S:
            return "服务持续失去 HTTP 响应"
        return None


@dataclass
class RestartBudget:
    """一个监管所有者最多追加三次启动，连续就绪一分钟后才重置故障次数。"""

    failures: int = 0
    ready_since: float | None = None

    def observe(self, *, ready: bool, now: float) -> None:
        """启动进程或首次就绪不清除预算，失去就绪会中断稳定窗口。"""
        if not ready:
            self.ready_since = None
        elif self.ready_since is None:
            self.ready_since = now
        elif now - self.ready_since >= 60:
            self.failures = 0

    def ended(self) -> None:
        """结束健康观测窗口，清理和退避耗时不计入稳定运行。"""
        self.ready_since = None

    def failed(self) -> float | None:
        """确认上一代全部退出后消费预算，返回退避秒数或明确耗尽。"""
        self.ready_since = None
        if self.failures >= 3:
            return None
        self.failures += 1
        return float(2 ** (self.failures - 1))
