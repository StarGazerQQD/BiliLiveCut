"""网感定时采集调度器测试:时间窗/间隔纯函数、暂停联动、单次采集与设置校验。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

from app.core import settings_store
from app.trends import collector as collector_mod
from app.trends.scheduler import (
    TrendScheduler,
    in_window,
    parse_hhmm,
    should_run,
    trend_scheduler,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_parse_hhmm() -> None:
    """合法时间解析为分钟数,非法回退默认值。"""
    assert parse_hhmm("03:30", 0) == 210
    assert parse_hhmm("00:00", 999) == 0
    assert parse_hhmm("bad", 60) == 60
    assert parse_hhmm("25:00", 60) == 60


def test_in_window_normal_and_wrap() -> None:
    """窗口判断支持普通区间与跨午夜区间。"""
    # 普通:03:00-05:00
    assert in_window(4 * 60, 3 * 60, 5 * 60)
    assert not in_window(6 * 60, 3 * 60, 5 * 60)
    # 跨午夜:23:00-02:00
    assert in_window(23 * 60 + 30, 23 * 60, 2 * 60)
    assert in_window(1 * 60, 23 * 60, 2 * 60)
    assert not in_window(12 * 60, 23 * 60, 2 * 60)
    # 起止相同视为全天
    assert in_window(10 * 60, 0, 0)


def test_should_run() -> None:
    """超过间隔才应采集;首次(last=0)立即可采。"""
    # 首次(last=0)且 now 为真实时间戳,远超间隔 -> 立即可采。
    assert should_run(1_700_000_000.0, 0.0, 1800.0)
    assert should_run(1_700_001_900.0, 1_700_000_000.0, 1800.0)
    assert not should_run(1_700_000_500.0, 1_700_000_000.0, 1800.0)


def test_pause_resume_with_recording_callback() -> None:
    """显式暂停或回调报告录制中,均视为暂停。"""
    sched = TrendScheduler()
    assert not sched._is_paused()

    sched.pause_for_recording()
    assert sched._is_paused()
    sched.resume_after_recording()
    assert not sched._is_paused()

    # 回调报告有录制 -> 即便未显式暂停也视为暂停。
    sched._recording_active = lambda: True
    assert sched._is_paused()


def test_status_structure(temp_db: None) -> None:
    """状态字典应包含关键字段。

    :param temp_db: 隔离数据库夹具。
    """
    status = trend_scheduler.status()
    for key in (
        "schedule_enabled",
        "trend_enabled",
        "window_start",
        "window_end",
        "interval_min",
        "paused_by_recording",
        "running",
    ):
        assert key in status


@pytest.mark.asyncio
async def test_run_once_collects_when_not_paused(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    """未暂停时 _run_once 应调用采集并记录结果;暂停时跳过。

    :param temp_db: 隔离数据库夹具。
    :param monkeypatch: pytest 夹具。
    """
    calls: list[str] = []

    def fake_collect(topic: str = "") -> int:
        calls.append(topic)
        return 3

    monkeypatch.setattr(collector_mod, "collect_and_save", fake_collect)

    sched = TrendScheduler()
    await sched._run_once(datetime.now())
    assert calls == [""]
    assert sched._last_saved == 3
    assert sched._last_run_at is not None

    # 暂停后不应再采集。
    sched.pause_for_recording()
    await sched._run_once(datetime.now())
    assert len(calls) == 1


def test_update_schedule_settings_validation(temp_db: None) -> None:
    """设置更新:合法值写入持久化;非法时间/间隔抛错。

    :param temp_db: 隔离数据库夹具。
    """
    from app.web import service

    service.update_settings(
        {
            "trend_schedule_enabled": True,
            "trend_schedule_start": "01:30",
            "trend_schedule_end": "02:45",
            "trend_schedule_interval_min": 15,
        }
    )
    assert settings_store.get_bool("trend_schedule_enabled") is True
    assert settings_store.get_setting("trend_schedule_start") == "01:30"
    assert settings_store.get_setting("trend_schedule_interval_min") == "15"

    with pytest.raises(ValueError):
        service.update_settings({"trend_schedule_start": "99:99"})
    with pytest.raises(ValueError):
        service.update_settings({"trend_schedule_interval_min": 0})
