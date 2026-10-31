"""CLI 子命令 — 录制命令。"""

from __future__ import annotations

import asyncio
import signal
from functools import partial

import click
from loguru import logger
from rich.console import Console
from rich.table import Table

from app.core.config import settings
from app.core.execution import run_sync
from app.db.entities import LiveRoom
from app.db.session import get_session
from app.db.session import init_db as _init_db
from app.recording.recorder import Recorder

console = Console()


def cmd_init() -> None:
    """初始化数据库(创建所有表,幂等)。"""
    _init_db()
    console.print(f"[green]数据库已初始化:[/green] {settings.database_url}")


@click.argument("url", type=str)
@click.option("--authorize", default=False, is_flag=True, help="确认拥有录制授权", show_default=True)
@click.option("--platform", default=None, type=str, help="显式来源平台；纯数字默认 Bilibili", show_default=True)
def cmd_add_room(
    url: str,
    authorize: bool = False,
    platform: str | None = None,
) -> None:
    """使用已启用的直播源插件解析并登记房间。"""
    from app.plugins.live_source import SourceError
    from app.plugins.manager import PluginError
    from app.plugins.runtime import plugin_runtime
    from app.sources.rooms import register_room, room_source

    async def resolve() -> LiveRoom:
        async with plugin_runtime():
            return await register_room(url, authorize, platform)

    try:
        room = asyncio.run(resolve())
        source = room_source(room)
    except (SourceError, PluginError, ValueError) as exc:
        console.print(str(exc), markup=False)
        raise click.exceptions.Exit(code=1) from exc
    console.print(
        f"已登记直播间 db_id={room.id} platform={source.platform} source_id={source.source_id} authorized={authorize}",
        markup=False,
    )


def cmd_list_rooms() -> None:
    """列出所有已登记的直播间。"""
    from app.sources.rooms import registered_rooms

    with get_session() as db:
        rooms = registered_rooms(db)

    if not rooms:
        console.print("[yellow]暂无已登记的直播间。[/yellow]")
        return

    table = Table(title="已登记直播间")
    for col in ("db_id", "platform", "source_id", "enabled", "authorized", "input_url"):
        table.add_column(col)
    from app.plugins.live_source import SourceError
    from app.sources.rooms import room_source

    for r in rooms:
        try:
            identity = room_source(r).source_id if r.platform != "local" else None
        except SourceError:
            identity = None
        table.add_row(
            str(r.id),
            r.platform,
            identity or "未知",
            str(r.enabled),
            str(r.authorized),
            r.input_url,
        )
    console.print(table)


@click.argument("url", type=str)
@click.option("--platform", default=None, type=str, help="显式来源平台", show_default=True)
def cmd_check(
    url: str,
    platform: str | None = None,
) -> None:
    """通过同一来源契约检查直播状态和播放规格，不输出临时播放凭据。"""
    from app.plugins.live_source import LiveStatus, SourceError, StreamPreference
    from app.plugins.manager import PluginError
    from app.plugins.runtime import plugin_runtime
    from app.sources.registry import source_registry

    async def check() -> None:
        async with plugin_runtime():
            room = await source_registry.resolve_room(url, platform)
            info = await source_registry.get_room_info(room)
            console.print(
                f"platform={room.platform} source_id={room.source_id} live_status={info.status}", markup=False
            )
            if info.status != LiveStatus.LIVE:
                return
            streams = await source_registry.get_streams(
                room, StreamPreference(preferred_transport=settings.preferred_stream_protocol)
            )
            console.print(f"可用流数量: {len(streams)}")
            if streams:
                best = streams[0]
                console.print(
                    f"最佳流 协议={best.transport} 格式={best.container} 编码={best.codec} 清晰度={best.quality_id}",
                    markup=False,
                )

    try:
        asyncio.run(check())
    except (SourceError, PluginError, ValueError) as exc:
        console.print(str(exc), markup=False)
        raise click.exceptions.Exit(code=1) from exc


@click.argument("db_id", type=int)
@click.option(
    "--pipeline/--no-pipeline",
    default=None,
    help="覆盖录制实时转写/高光分析默认值；未指定时读取运行时开关或 RECORDING_PIPELINE_ENABLED",
    show_default=True,
)
@click.option(
    "--produce",
    default=False,
    is_flag=True,
    help="产生高光候选后自动切片+生成文案(阶段3);需配合 --pipeline",
    show_default=True,
)
def cmd_record(
    db_id: int,
    pipeline: bool | None = None,
    produce: bool = False,
) -> None:
    """对指定直播间开始录制,直到 Ctrl+C 停止。

    :param db_id: ``live_rooms`` 主键。
    :param pipeline: 是否在录制同时启用转写+高光分析流水线；``None`` 使用全局默认值。
    :param produce: 是否在产生候选后自动切片与生成文案。
    """
    from app.plugins.live_source import SourceError, SourceUnavailable
    from app.plugins.runtime import plugin_runtime
    from app.sources.registry import source_registry
    from app.sources.rooms import room_is_removed, room_source

    async def execute() -> None:
        async with plugin_runtime():
            from app.core import settings_store

            pipeline_enabled = settings_store.recording_pipeline_enabled() if pipeline is None else pipeline
            if produce and not pipeline_enabled:
                console.print("[red]--produce 必须与 --pipeline 一起使用。[/red]")
                raise click.exceptions.Exit(code=1)

            with get_session() as db:
                room = db.get(LiveRoom, db_id)
                if room is None or room_is_removed(db, db_id):
                    console.print(f"[red]未找到 db_id={db_id} 的直播间。[/red]")
                    raise click.exceptions.Exit(code=1)
                if settings.require_authorization and not room.authorized:
                    console.print("[red]该直播间未确认授权,拒绝录制。[/red]")
                    raise click.exceptions.Exit(code=1)
                identity = room_source(room, db)
                if not source_registry.available(identity.platform):
                    raise SourceUnavailable("来源不可用，请安装并启用对应直播源插件")
                room.enabled = True
                # 五阶段调度器以房间级开关为唯一真源。CLI 的有效 Pipeline 值需要同步到
                # 房间配置，否则 Recorder 虽安装了回调，scheduler 仍会把任务留在 RECORDED。
                if pipeline_enabled:
                    room.auto_analyze = True
                if produce:
                    room.auto_render = True
                db.add(room)

            on_segment = None
            if pipeline_enabled:
                from app.pipeline.orchestrator import make_pipeline_callback

                # 传入房间主键，让回调读取刚同步的自动化配置；否则
                # room_id=None 会按 auto_analyze=False 跳过任务登记。
                on_segment = make_pipeline_callback(produce=produce, room_id=db_id)
                extra = " + 自动切片/文案" if produce else ""
                console.print(f"[cyan]已启用实时分析流水线(转写 + 高光评分{extra})。[/cyan]")

            async def _on_end(session_id: int) -> None:
                """会话结束:上传模块关闭时弹出切片目录。"""
                if pipeline_enabled:
                    from app.analysis.reanalysis import request_session_reanalysis
                    from app.analysis.session_summary import request_session_timeline_summary

                    reanalysis_requested = await run_sync(
                        partial(request_session_reanalysis, session_id, reason="session_finalized")
                    )
                    if not reanalysis_requested:
                        request_session_timeline_summary(
                            session_id,
                            reason="session_finalized_without_reanalysis",
                            force=True,
                        )
                from app.core import settings_store
                from app.core.osutil import open_path
                from app.core.paths import clips_dir

                if settings_store.upload_active():
                    return
                path = str(clips_dir())
                console.print(f"[green]本场直播已结束。上传模块未开启,切片已保存到:[/green] {path}")
                open_path(path)

            recorder = Recorder(source_room=identity, db_room_id=db_id, on_segment=on_segment, on_end=_on_end)
            loop = asyncio.get_running_loop()
            try:
                loop.add_signal_handler(signal.SIGINT, recorder.stop)
            except NotImplementedError:
                pass  # Windows 下 asyncio.run 的取消进入 Recorder finally 完成收尾。
            console.print(f"开始录制 {identity.platform}:{identity.source_id}（按 Ctrl+C 停止）", markup=False)
            try:
                await recorder.run()
            finally:
                with get_session() as db:
                    room = db.get(LiveRoom, db_id)
                    if room is not None:
                        room.enabled = False
                        db.add(room)

    try:
        asyncio.run(execute())
    except KeyboardInterrupt:
        logger.info("已取消录制并完成收尾")
    except click.exceptions.Exit:
        raise
    except Exception as exc:
        # CLI 最外层录制边界：禁止 traceback locals 回显 FFmpeg 播放凭据。
        code = exc.code if isinstance(exc, SourceError) else type(exc).__name__
        console.print(f"录制失败：{code}，请检查来源设置与 FFmpeg 配置", markup=False)
        raise click.exceptions.Exit(code=1) from None
    console.print("[green]录制已结束。[/green]")


# 注册列表
RECORD_COMMANDS = [
    ("init", cmd_init, None),
    ("add-room", cmd_add_room, None),
    ("list-rooms", cmd_list_rooms, None),
    ("check", cmd_check, None),
    ("record", cmd_record, None),
]
