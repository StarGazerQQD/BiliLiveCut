"""CLI 子命令 — 日程管理。"""

from __future__ import annotations

import typer
from rich.console import Console

from app.db.models import LiveRoom
from app.db.session import get_session

console = Console()


def cmd_schedule(
    room_id: int = typer.Argument(..., help="live_rooms 主键(dbid)"),
    at_time: str = typer.Option(..., "--at", help="计划启动时间,ISO 格式(如 2026-07-03T20:00:00)或 HH:MM(默认今天)"),
    daily: bool = typer.Option(False, "--daily", help="设为每日重复"),
) -> None:
    """为直播间创建一个录制预约(V0.1.2 新增)。

    到达预定时间后 Web 控制台会自动启动录制(需在后台运行中)。

    :param room_id: ``live_rooms`` 主键。
    :param at_time: 计划时间。
    :param daily: 是否为每日重复。
    """
    from datetime import datetime

    from app.core.config import settings as s
    from app.db.models import RecordingSchedule

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        if room is None:
            console.print(f"[red]房间不存在: db_id={room_id}[/red]")
            raise typer.Exit(code=1)

    # 解析时间:ISO 格式直接解析;HH:MM 则补齐到本地今天的完整时间。
    try:
        ts = datetime.fromisoformat(at_time)
    except ValueError:
        try:
            local_now = datetime.now()
            h, m = map(int, at_time.split(":"))
            ts = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            # 如果今天的时间已过,推至明天。
            if ts <= local_now:
                from datetime import timedelta

                ts += timedelta(days=1)
        except (ValueError, AttributeError) as err:
            console.print(f"[red]时间格式无效: {at_time}(支持 ISO 或 HH:MM)[/red]")
            raise typer.Exit(code=1) from err

    recurrent = "daily" if daily else ""
    with get_session() as db:
        sched = RecordingSchedule(
            room_id=room_id,
            scheduled_at=ts,
            enabled=True,
            recurrent=recurrent,
        )
        db.add(sched)
        db.flush()
        db.refresh(sched)

    label = "每日" if daily else "单次"
    console.print(f"[green]预约已创建[/green] id={sched.id} 房间=#{room_id} {label} @ {ts.isoformat()}")
    console.print(f"[dim]提示:预约在 Web 控制台运行时生效(后台每 {s.schedule_check_interval_s}s 检查一次)。[/dim]")


# 注册列表
ROOM_COMMANDS = [
    ("schedule", cmd_schedule, None),
]
