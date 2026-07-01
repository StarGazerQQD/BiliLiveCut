"""网感资料库定时采集调度器。

在每天的固定时间段内,按设定间隔自动执行"采集 + 迭代入库"(重复采集会去重并
刷新热度,从而不断迭代资料库)。

关键约束(按需求):**一旦开始录制/分析,定时采集立即暂停**。实现上有两道闸:

1. 录制启动时由 :class:`~app.web.service.RecorderManager` 调用 :meth:`pause_for_recording`
   立刻置暂停标志(满足"立刻");
2. 调度循环每个 tick 还会通过注入的 ``recording_active`` 回调复查是否有录制在跑
   (兜底 CLI 等其它入口)。录制全部停止后自动恢复。

时间窗与间隔的判断是纯函数(:func:`in_window` / :func:`should_run`),便于单测。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime

from loguru import logger

from app.core import settings_store
from app.core.config import settings

# 调度循环的检查间隔(秒)。较小的值让"进入窗口/暂停"响应更及时。
_CHECK_INTERVAL_S = 30


def parse_hhmm(value: str, default_minutes: int) -> int:
    """把 ``HH:MM`` 解析为"当日分钟数"(0-1439)。

    :param value: 形如 ``03:30`` 的字符串。
    :param default_minutes: 解析失败时的回退分钟数。
    :returns: 0-1439 的分钟数。
    """
    try:
        h, m = value.strip().split(":")
        minutes = int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return default_minutes
    if 0 <= minutes < 24 * 60:
        return minutes
    return default_minutes


def in_window(now_min: int, start_min: int, end_min: int) -> bool:
    """判断当前分钟数是否落在采集窗口内(支持跨午夜)。

    :param now_min: 当前当日分钟数。
    :param start_min: 窗口起始分钟数。
    :param end_min: 窗口结束分钟数。
    :returns: 在窗口内返回 ``True``。
    """
    if start_min == end_min:
        return True  # 起止相同视为全天
    if start_min < end_min:
        return start_min <= now_min < end_min
    # 跨午夜,如 23:00 -> 02:00
    return now_min >= start_min or now_min < end_min


def should_run(now_ts: float, last_run_ts: float, interval_s: float) -> bool:
    """判断距上次采集是否已超过间隔(在窗口内时使用)。

    :param now_ts: 当前时间戳(秒)。
    :param last_run_ts: 上次采集时间戳(秒);从未采集为 0。
    :param interval_s: 采集间隔(秒)。
    :returns: 应当采集返回 ``True``。
    """
    return (now_ts - last_run_ts) >= interval_s


class TrendScheduler:
    """网感资料库的每日定时采集调度器(asyncio 后台任务)。"""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._recording_active: Callable[[], bool] = lambda: False
        self._paused_by_recording = False
        self._last_run_ts = 0.0
        self._last_run_at: datetime | None = None
        self._last_saved = 0
        self._collecting = False

    # ---------------- 生命周期 ---------------- #
    def start(self, recording_active: Callable[[], bool] | None = None) -> None:
        """启动调度后台任务(幂等)。

        :param recording_active: 返回"当前是否有录制在进行"的回调(兜底复查)。
        """
        if recording_active is not None:
            self._recording_active = recording_active
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("网感定时采集调度器已启动。")

    async def stop(self) -> None:
        """停止调度后台任务。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
            logger.info("网感定时采集调度器已停止。")

    # ---------------- 暂停/恢复(录制联动) ---------------- #
    def pause_for_recording(self) -> None:
        """录制/分析开始时调用:立即暂停定时采集。"""
        if not self._paused_by_recording:
            logger.info("检测到录制开始,网感定时采集已暂停。")
        self._paused_by_recording = True

    def resume_after_recording(self) -> None:
        """录制全部停止后调用:恢复定时采集。"""
        if self._paused_by_recording:
            logger.info("录制已全部停止,网感定时采集恢复待命。")
        self._paused_by_recording = False

    def _is_paused(self) -> bool:
        """当前是否处于暂停(显式标志或仍有录制在跑)。"""
        try:
            recording = bool(self._recording_active())
        except Exception:  # noqa: BLE001
            recording = False
        return self._paused_by_recording or recording

    # ---------------- 调度主循环 ---------------- #
    async def _loop(self) -> None:
        """周期性检查并在满足条件时触发采集。"""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — 调度异常不应使任务退出
                logger.warning("网感调度 tick 异常: {}", exc)
            await asyncio.sleep(_CHECK_INTERVAL_S)

    async def _tick(self) -> None:
        """单次调度判断:是否到点、是否暂停、是否在窗口内。"""
        if not settings_store.get_bool("trend_schedule_enabled"):
            return
        if not settings.trend_enabled:
            return  # 资料库本身未启用,无从采集
        if self._is_paused():
            return
        start_min, end_min, interval_s = self._read_cfg()
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        if not in_window(now_min, start_min, end_min):
            return
        if not should_run(time.time(), self._last_run_ts, interval_s):
            return
        await self._run_once(now)

    async def _run_once(self, now: datetime) -> None:
        """执行一次采集入库(在线程池中运行,避免阻塞事件循环)。

        :param now: 当前时间(记录用)。
        """
        if self._is_paused():
            return
        from app.trends.collector import collect_and_save

        self._collecting = True
        try:
            saved = await asyncio.to_thread(collect_and_save, "")
            self._last_run_ts = time.time()
            self._last_run_at = now
            self._last_saved = saved
            logger.info("网感定时采集完成,新增/更新 {} 条。", saved)
        except Exception as exc:  # noqa: BLE001
            logger.warning("网感定时采集失败: {}", exc)
        finally:
            self._collecting = False

    def _read_cfg(self) -> tuple[int, int, float]:
        """读取窗口与间隔配置。

        :returns: ``(start_min, end_min, interval_seconds)``。
        """
        start_min = parse_hhmm(settings_store.get_setting("trend_schedule_start"), 3 * 60)
        end_min = parse_hhmm(settings_store.get_setting("trend_schedule_end"), 5 * 60)
        try:
            interval_min = max(1, int(settings_store.get_setting("trend_schedule_interval_min")))
        except (ValueError, TypeError):
            interval_min = 30
        return start_min, end_min, interval_min * 60.0

    def status(self) -> dict:
        """返回调度器当前状态(供前端展示)。

        :returns: 状态字典。
        """
        return {
            "schedule_enabled": settings_store.get_bool("trend_schedule_enabled"),
            "trend_enabled": settings.trend_enabled,
            "window_start": settings_store.get_setting("trend_schedule_start"),
            "window_end": settings_store.get_setting("trend_schedule_end"),
            "interval_min": settings_store.get_setting("trend_schedule_interval_min"),
            "paused_by_recording": self._is_paused(),
            "collecting": self._collecting,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "last_saved": self._last_saved,
            "running": self._task is not None and not self._task.done(),
        }


# 模块级单例:整个 Web 进程共享一个调度器。
trend_scheduler = TrendScheduler()
