"""CLI 子命令 — 录制命令。"""

from __future__ import annotations

import asyncio
import signal

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table
from sqlmodel import select

from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.db.models import LiveRoom
from app.db.session import get_session
from app.db.session import init_db as _init_db
from app.recording.recorder import Recorder
from app.sources.bilibili.client import BilibiliLiveClient, pick_best_stream

console = Console()


def cmd_init() -> None:
    """初始化数据库(创建所有表,幂等)。"""
    _init_db()
    console.print(f"[green]数据库已初始化:[/green] {settings.database_url}")


def cmd_add_room(
    url: str = typer.Argument(..., help="直播间 URL 或房间号"),
    authorize: bool = typer.Option(
        False,
        "--authorize",
        help="确认你拥有录制该直播间内容的授权(合规要求)",
    ),
) -> None:
    """解析并登记一个直播间。

    :param url: 直播间 URL 或房间号。
    :param authorize: 是否确认拥有录制授权。
    """
    if settings.require_authorization and not authorize:
        console.print("[red]需要授权确认。[/red] 仅可录制你拥有授权的内容。确认后请加 [bold]--authorize[/bold] 重试。")
        raise typer.Exit(code=1)

    async def _resolve() -> LiveRoom:
        async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
            info = await client.get_room_info(url)
        return LiveRoom(
            input_url=url,
            room_id=info.room_id,
            authorized=authorize,
            highlight_threshold=settings.highlight_threshold,
            auto_publish_threshold=settings.auto_publish_threshold,
        )

    room = asyncio.run(_resolve())
    with get_session() as db:
        existing = db.exec(select(LiveRoom).where(LiveRoom.room_id == room.room_id)).first()
        if existing:
            existing.authorized = authorize
            existing.input_url = url
            db.add(existing)
            console.print(f"[yellow]已存在,信息已更新:[/yellow] room_id={room.room_id}")
            return
        db.add(room)
        db.flush()
        db.refresh(room)
        console.print(f"[green]已登记直播间[/green] db_id={room.id} room_id={room.room_id} authorized={authorize}")


def cmd_list_rooms() -> None:
    """列出所有已登记的直播间。"""
    with get_session() as db:
        rooms = db.exec(select(LiveRoom)).all()

    if not rooms:
        console.print("[yellow]暂无已登记的直播间。[/yellow]")
        return

    table = Table(title="已登记直播间")
    for col in ("db_id", "room_id", "mode", "enabled", "authorized", "input_url"):
        table.add_column(col)
    for r in rooms:
        table.add_row(
            str(r.id),
            str(r.room_id),
            r.mode,
            str(r.enabled),
            str(r.authorized),
            r.input_url,
        )
    console.print(table)


def cmd_check(url: str = typer.Argument(..., help="直播间 URL 或房间号")) -> None:
    """检查直播间当前是否在播、可取到哪条流(只读,不录制)。

    :param url: 直播间 URL 或房间号。
    """

    async def _check() -> None:
        async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
            info = await client.get_room_info(url)
            console.print(
                f"room_id=[bold]{info.room_id}[/bold] live_status={info.live_status} "
                f"({'直播中' if info.is_live else '未开播'})"
            )
            if not info.is_live:
                return
            streams = await client.get_streams(info.room_id, quality=settings.stream_quality)
            best = pick_best_stream(streams, settings.preferred_stream_protocol)
            console.print(f"可用流数量: {len(streams)}")
            if best:
                console.print(
                    f"[green]最佳流[/green] 协议={best.protocol} 格式={best.format_name} "
                    f"编码={best.codec_name} 清晰度={best.quality}"
                )

    asyncio.run(_check())


def cmd_record(
    db_id: int = typer.Argument(..., help="直播间在数据库中的 db_id(见 list-rooms)"),
    pipeline: bool = typer.Option(
        False,
        "--pipeline",
        help="录制的同时实时转写并做高光评分(阶段2);需安装 .[asr]",
    ),
    produce: bool = typer.Option(
        False,
        "--produce",
        help="产生高光候选后自动切片+生成文案(阶段3);需配合 --pipeline",
    ),
) -> None:
    """对指定直播间开始录制,直到 Ctrl+C 停止。

    :param db_id: ``live_rooms`` 主键。
    :param pipeline: 是否在录制同时启用转写+高光分析流水线。
    :param produce: 是否在产生候选后自动切片与生成文案。
    """
    if produce and not pipeline:
        console.print("[red]--produce 必须与 --pipeline 一起使用。[/red]")
        raise typer.Exit(code=1)

    with get_session() as db:
        room = db.get(LiveRoom, db_id)
        if room is None:
            console.print(f"[red]未找到 db_id={db_id} 的直播间。[/red]")
            raise typer.Exit(code=1)
        if settings.require_authorization and not room.authorized:
            console.print("[red]该直播间未确认授权,拒绝录制。[/red]")
            raise typer.Exit(code=1)
        room_id = room.room_id
        room.enabled = True
        # 五阶段调度器以房间级开关为唯一真源。CLI 显式参数需要同步到
        # 房间配置，否则 Recorder 虽安装了回调，scheduler 仍会把任务留在 RECORDED。
        if pipeline:
            room.auto_analyze = True
        if produce:
            room.auto_render = True
        db.add(room)

    if room_id is None:
        console.print("[red]该直播间缺少 room_id,请重新 add-room。[/red]")
        raise typer.Exit(code=1)

    on_segment = None
    if pipeline:
        from app.pipeline.orchestrator import make_pipeline_callback

        # 传入房间主键，让回调读取刚同步的自动化配置；否则
        # room_id=None 会按 auto_analyze=False 跳过任务登记。
        on_segment = make_pipeline_callback(produce=produce, room_id=db_id)
        extra = " + 自动切片/文案" if produce else ""
        console.print(f"[cyan]已启用实时分析流水线(转写 + 高光评分{extra})。[/cyan]")

    async def _on_end(session_id: int) -> None:
        """会话结束:上传模块关闭时弹出切片目录。"""
        from app.core import settings_store
        from app.core.osutil import open_path
        from app.core.paths import clips_dir

        if settings_store.upload_active():
            return
        path = str(clips_dir())
        console.print(f"[green]本场直播已结束。上传模块未开启,切片已保存到:[/green] {path}")
        open_path(path)

    recorder = Recorder(room_id=room_id, db_room_id=db_id, on_segment=on_segment, on_end=_on_end)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        # 注册信号处理,Ctrl+C 时优雅停止(Windows 下 SIGINT 仍有效)。
        try:
            loop.add_signal_handler(signal.SIGINT, recorder.stop)
        except NotImplementedError:
            # Windows 的 ProactorEventLoop 不支持 add_signal_handler,
            # 退回到 KeyboardInterrupt 捕获。
            pass
        await recorder.run()

    console.print(f"[green]开始录制[/green] room_id={room_id}(按 Ctrl+C 停止)...")
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt,正在停止录制 ...")
        recorder.stop()
    console.print("[green]录制已结束。[/green]")


# 注册列表
RECORD_COMMANDS = [
    ("init", cmd_init, None),
    ("add-room", cmd_add_room, None),
    ("list-rooms", cmd_list_rooms, None),
    ("check", cmd_check, None),
    ("record", cmd_record, None),
]
