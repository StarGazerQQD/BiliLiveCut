# ruff: noqa: D100
from __future__ import annotations

from dataclasses import dataclass

RESTART_OWNER_ENV = "_BLC_RESTART_OWNER"
CLEANUP_UNCONFIRMED_EXIT = 75
SERVICE_BIND_FAILED_EXIT = 78
# 后端允许的关闭配置上限为 3600 秒，外层额外保留所属树退出时间。
OUTER_SHUTDOWN_LIMIT_S = 3620.0


@dataclass
class RestartBudget:
    """一个监管所有者最多追加三次启动，连续就绪一分钟后才重置故障次数。"""

    failures: int = 0
    ready_since: float | None = None
    ended_at: float | None = None

    def observe(self, *, ready: bool, now: float) -> None:
        """启动进程或首次就绪不清除预算，失去就绪会中断稳定窗口。"""
        self.ended_at = None
        if not ready:
            self.ready_since = None
        elif self.ready_since is None:
            self.ready_since = now

    def ended(self, now: float) -> None:
        """冻结实际运行结束时间，清理耗时不计入稳定运行窗口。"""
        self.ended_at = now

    def failed(self, now: float) -> float | None:
        """确认上一代全部退出后消费预算，返回退避秒数或明确耗尽。"""
        elapsed_until = self.ended_at if self.ended_at is not None else now
        if self.ready_since is not None and elapsed_until - self.ready_since >= 60:
            self.failures = 0
        self.ready_since = None
        self.ended_at = None
        if self.failures >= 3:
            return None
        self.failures += 1
        return float(2 ** (self.failures - 1))
