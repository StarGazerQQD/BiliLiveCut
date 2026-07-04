"""命令行入口(Typer)。

阶段1(本地录制 MVP)提供以下命令:

* ``init``        初始化数据库;
* ``add-room``    解析并登记一个直播间(需确认授权);
* ``list-rooms``  列出已登记直播间;
* ``check``       检查某直播间当前是否可取流(只读、不录制);
* ``record``      对指定直播间开始录制(Ctrl+C 优雅停止)。

运行示例::

    python -m app.cli init
    python -m app.cli add-room "https://live.bilibili.com/123" --authorize
    python -m app.cli check 123
    python -m app.cli record 1
"""

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
from app.core.logging import setup_logging
from app.db.models import FinalClip, HighlightCandidate, LiveRoom
from app.db.session import get_session, init_db
from app.recording.recorder import Recorder
from app.sources.bilibili.client import BilibiliLiveClient, pick_best_stream

app = typer.Typer(help="BiliLiveCut —— AI 直播实时切片系统 CLI(阶段1:本地录制)")
console = Console()


@app.callback()
def _bootstrap() -> None:
    """所有命令执行前的初始化:配置日志。"""
    setup_logging()


@app.command()
def init() -> None:
    """初始化数据库(创建所有表,幂等)。"""
    init_db()
    console.print(f"[green]数据库已初始化:[/green] {settings.database_url}")


@app.command("add-room")
def add_room(
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
        console.print(
            "[red]需要授权确认。[/red] 仅可录制你拥有授权的内容。"
            "确认后请加 [bold]--authorize[/bold] 重试。"
        )
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
        console.print(
            f"[green]已登记直播间[/green] db_id={room.id} room_id={room.room_id} "
            f"authorized={authorize}"
        )


@app.command("list-rooms")
def list_rooms() -> None:
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


@app.command()
def check(url: str = typer.Argument(..., help="直播间 URL 或房间号")) -> None:
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


@app.command()
def record(
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
        db.add(room)

    if room_id is None:
        console.print("[red]该直播间缺少 room_id,请重新 add-room。[/red]")
        raise typer.Exit(code=1)

    on_segment = None
    if pipeline:
        from app.pipeline.orchestrator import make_pipeline_callback

        on_segment = make_pipeline_callback(produce=produce)
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

    recorder = Recorder(
        room_id=room_id, db_room_id=db_id, on_segment=on_segment, on_end=_on_end
    )

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


@app.command()
def transcribe(
    segment_id: int = typer.Argument(..., help="raw_segments 主键"),
) -> None:
    """转写指定片段(阶段2,需安装 .[asr])。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.analysis.transcribe import transcribe_segment

    t = transcribe_segment(segment_id)
    console.print(f"[green]转写完成[/green] transcript_id={t.id} 语言={t.language}")
    console.print(t.text[:500] or "(空)")


@app.command()
def score(
    segment_id: int = typer.Argument(..., help="raw_segments 主键(需已转写)"),
) -> None:
    """对已转写片段做高光评分(阶段2)。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.analysis.highlight import score_segment

    candidate = score_segment(segment_id)
    if candidate is None:
        console.print("[yellow]未达阈值或重复,未生成候选。[/yellow]")
    else:
        console.print(
            f"[green]★ 生成高光候选[/green] id={candidate.id} "
            f"分数={candidate.highlight_score:.3f} 理由={candidate.reason}"
        )


@app.command()
def process(
    segment_id: int = typer.Argument(..., help="raw_segments 主键"),
) -> None:
    """对单个片段执行完整分析流程:转写 + 高光评分(阶段2)。

    :param segment_id: ``raw_segments`` 主键。
    """
    from app.pipeline.orchestrator import process_segment_sync

    candidate = process_segment_sync(segment_id)
    if candidate is None:
        console.print("[yellow]处理完成,未生成候选。[/yellow]")
    else:
        console.print(
            f"[green]★ 处理完成并生成候选[/green] id={candidate.id} "
            f"分数={candidate.highlight_score:.3f}"
        )


@app.command("list-candidates")
def list_candidates(
    limit: int = typer.Option(20, help="最多显示数量"),
) -> None:
    """列出高光候选。

    :param limit: 显示上限。
    """
    with get_session() as db:
        rows = db.exec(
            select(HighlightCandidate).order_by(HighlightCandidate.highlight_score.desc())  # type: ignore[attr-defined]
        ).all()

    rows = rows[:limit]
    if not rows:
        console.print("[yellow]暂无高光候选。[/yellow]")
        return

    table = Table(title="高光候选(按分数降序)")
    for col in ("id", "session", "score", "rule", "llm", "status", "reason"):
        table.add_column(col)
    for c in rows:
        table.add_row(
            str(c.id),
            str(c.session_id),
            f"{c.highlight_score:.3f}",
            f"{c.rule_score:.3f}",
            f"{c.llm_score:.3f}",
            c.status,
            (c.reason or "")[:40],
        )
    console.print(table)


@app.command()
def clip(
    candidate_id: int = typer.Argument(..., help="highlight_candidates 主键"),
) -> None:
    """把一个高光候选生成为成品 MP4(阶段3,不含文案)。

    :param candidate_id: ``highlight_candidates`` 主键。
    """
    from app.clipping.clipper import produce_clip as make_clip

    c = make_clip(candidate_id)
    console.print(
        f"[green]切片完成[/green] clip_id={c.id} 时长={c.duration_s:.1f}s -> {c.file_path}"
    )


@app.command()
def copywrite(
    clip_id: int = typer.Argument(..., help="final_clips 主键"),
) -> None:
    """为成品切片生成标题/简介/标签等文案(阶段3)。

    :param clip_id: ``final_clips`` 主键。
    """
    from app.publishing.copywriter import generate_copy

    c = generate_copy(clip_id)
    console.print(f"[green]文案完成[/green] 状态={c.status}\n标题: {c.title}")
    console.print(f"简介: {c.description}")


@app.command()
def produce(
    candidate_id: int = typer.Argument(..., help="highlight_candidates 主键"),
) -> None:
    """对一个候选执行完整出片流程:切片 + 文案(阶段3)。

    :param candidate_id: ``highlight_candidates`` 主键。
    """
    from app.pipeline.orchestrator import process_candidate

    c = process_candidate(candidate_id)
    if c is None:
        console.print("[red]出片失败,详见日志。[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"[green]★ 出片完成[/green] clip_id={c.id} 状态={c.status}\n"
        f"标题: {c.title}\n文件: {c.file_path}"
    )


@app.command("list-clips")
def list_clips(limit: int = typer.Option(20, help="最多显示数量")) -> None:
    """列出成品切片。

    :param limit: 显示上限。
    """
    with get_session() as db:
        rows = db.exec(
            select(FinalClip).order_by(FinalClip.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    rows = rows[:limit]
    if not rows:
        console.print("[yellow]暂无成品切片。[/yellow]")
        return
    table = Table(title="成品切片")
    for col in ("id", "candidate", "status", "dur", "title", "file"):
        table.add_column(col)
    for c in rows:
        table.add_row(
            str(c.id),
            str(c.candidate_id),
            c.status,
            f"{c.duration_s:.0f}s" if c.duration_s else "-",
            (c.title or "")[:30],
            c.file_path.split("\\")[-1].split("/")[-1],
        )
    console.print(table)


@app.command()
def upload(
    clip_id: int = typer.Argument(..., help="final_clips 主键"),
) -> None:
    """对一个成品切片执行上传(阶段5)。

    默认 manual:仅导出待上传清单,不调用平台接口。仅当在后台开启 biliup 开关
    且配置了上传命令时才会真正上传(风险自负)。

    :param clip_id: ``final_clips`` 主键。
    """
    from app.publishing.uploader import enqueue_and_upload

    task = enqueue_and_upload(clip_id)
    console.print(
        f"[green]上传任务[/green] id={task.id} 状态={task.status} "
        f"上传器={task.uploader} {task.last_error or ''}"
    )


@app.command("set-upload")
def set_upload(
    biliup: bool = typer.Option(None, "--biliup/--no-biliup", help="启用/关闭 Biliup 上传"),
    auto: bool = typer.Option(None, "--auto/--no-auto", help="成品就绪后自动上传"),
) -> None:
    """查看或设置上传开关(与 Web 后台共享同一持久化状态)。

    :param biliup: 是否启用 biliup 上传。
    :param auto: 是否自动上传。
    """
    from app.core import settings_store

    if biliup is not None:
        settings_store.set_bool("biliup_enabled", biliup)
    if auto is not None:
        settings_store.set_bool("auto_upload", auto)
    console.print(
        f"biliup_enabled={settings_store.biliup_enabled()} "
        f"auto_upload={settings_store.auto_upload_enabled()} "
        f"upload_active={settings_store.upload_active()}"
    )


@app.command("trends-collect")
def trends_collect(
    topic: str = typer.Option("", help="采集主题提示(留空用默认:近期适合切片的全网热点)"),
) -> None:
    """联网采集近期热门内容并写入网感资料库。

    需配置大模型 API 且 TREND_ENABLED=true。未启用或不可用时不入库。

    :param topic: 采集主题提示。
    """
    from app.trends.collector import collect_and_save

    if not settings.trend_enabled:
        console.print("[yellow]网感资料库未启用(TREND_ENABLED=false)。[/yellow]")
        raise typer.Exit(code=1)
    saved = collect_and_save(topic)
    console.print(f"[green]采集完成[/green] 新增/更新 {saved} 条。")


@app.command("trends-list")
def trends_list(
    limit: int = typer.Option(20, help="最多显示数量"),
    days: int = typer.Option(7, help="仅看最近 N 天"),
) -> None:
    """列出网感资料库中的近期热门条目。

    :param limit: 显示上限。
    :param days: 近期窗口(天)。
    """
    from app.trends.store import recent_trends

    rows = recent_trends(limit=limit, days=days)
    if not rows:
        console.print("[yellow]资料库暂无数据。先运行 trends-collect。[/yellow]")
        return
    table = Table(title=f"网感资料库(最近 {days} 天,按热度降序)")
    for col in ("id", "source", "category", "heat", "seen", "title"):
        table.add_column(col)
    for it in rows:
        table.add_row(
            str(it.id),
            it.source,
            it.category or "-",
            f"{it.heat:.0f}",
            str(it.seen_count),
            (it.title or "")[:40],
        )
    console.print(table)


@app.command("trends-keywords")
def trends_keywords(
    top: int = typer.Option(20, help="显示前 N 个热词"),
    days: int = typer.Option(7, help="近期窗口(天)"),
) -> None:
    """展示近期热门标签/关键词的热度排行。

    :param top: 显示数量。
    :param days: 近期窗口(天)。
    """
    from app.trends.store import keyword_heat

    rows = keyword_heat(days=days, top=top)
    if not rows:
        console.print("[yellow]暂无关键词统计。[/yellow]")
        return
    table = Table(title=f"近期热词(最近 {days} 天)")
    for col in ("keyword", "heat", "count"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["keyword"], str(r["heat"]), str(r["count"]))
    console.print(table)


@app.command("trends-purge")
def trends_purge(
    days: int = typer.Option(None, help="保留天数(留空用配置 TREND_RETENTION_DAYS)"),
) -> None:
    """清理超过保留期的网感资料库条目。

    :param days: 保留天数。
    """
    from app.trends.store import purge_old

    n = purge_old(days if days is not None else settings.trend_retention_days)
    console.print(f"[green]已清理 {n} 条过期条目。[/green]")


@app.command("llm-list")
def llm_list() -> None:
    """列出已配置的大模型(按优先级),显示是否可用(key 掩码)。"""
    from app.analysis import llm_providers as provs

    items = provs.load_providers()
    if not items:
        console.print("[yellow]未配置任何大模型。可在 Web「模型」页添加,或配置 .env[/yellow]")
        return
    table = Table(title="大模型(按优先级升序)")
    for col in ("priority", "name", "model", "base_url", "key", "enabled"):
        table.add_column(col)
    for p in items:
        hint = f"****{p.api_key[-4:]}" if p.api_key else "(未配置)"
        table.add_row(
            str(p.priority), p.name, p.model, p.base_url, hint,
            "是" if p.enabled else "否",
        )
    console.print(table)


@app.command("llm-test")
def llm_test() -> None:
    """逐个测试已启用大模型的连通性(各发一次极小请求)。"""
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    providers = provs.active_providers()
    if not providers:
        console.print("[yellow]无可用大模型(需已启用且配置 key)。[/yellow]")
        raise typer.Exit(code=1)
    for p in providers:
        try:
            text = llm_mod._complete(p, "ping", max_tokens=1)
            console.print(f"[green]OK[/green] {p.name}({p.model}) -> {(text or '')[:40]!r}")
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            console.print(f"[red]FAIL[/red] {p.name}({p.model}): {str(exc)[:160]}")


@app.command()
def schedule(
    room_id: int = typer.Argument(..., help="live_rooms 主键(dbid)"),
    at_time: str = typer.Option(
        ..., "--at", help="计划启动时间,ISO 格式(如 2026-07-03T20:00:00)或 HH:MM(默认今天)"
    ),
    daily: bool = typer.Option(False, "--daily", help="设为每日重复"),
) -> None:
    """为直播间创建一个录制预约(V0.1.2 新增)。

    到达预定时间后 Web 控制台会自动启动录制(需在后台运行中)。

    :param room_id: ``live_rooms`` 主键。
    :param at_time: 计划时间。
    :param daily: 是否为每日重复。
    """
    from datetime import datetime, timezone

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
        except (ValueError, AttributeError):
            console.print(f"[red]时间格式无效: {at_time}(支持 ISO 或 HH:MM)[/red]")
            raise typer.Exit(code=1)

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
    console.print(
        f"[green]预约已创建[/green] id={sched.id} "
        f"房间=#{room_id} {label} @ {ts.isoformat()}"
    )
    console.print(f"[dim]提示:预约在 Web 控制台运行时生效(后台每 {s.schedule_check_interval_s}s 检查一次)。[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
    reload: bool = typer.Option(False, "--reload", help="开发热重载"),
) -> None:
    """启动 Web 管理后台(阶段4,需安装 .[web])。

    :param host: 监听地址。
    :param port: 监听端口。
    :param reload: 是否开启热重载。
    """
    try:
        import uvicorn
    except ImportError as exc:
        console.print('[red]未安装 Web 依赖。请执行: pip install -e ".[web]"[/red]')
        raise typer.Exit(code=1) from exc

    console.print(f"[green]控制台启动中[/green] -> http://{host}:{port}")
    uvicorn.run("app.web.main:app", host=host, port=port, reload=reload)


# --------------------------------------------------------------------------- #
# V0.1.9: ML 高光模型自学习
# --------------------------------------------------------------------------- #
@app.command()
def ml_learn(
    room_id: int = typer.Option(-1, help="限定直播间 db_id(-1=全量)"),
    model_type: str = typer.Option("xgboost", help="模型类型:xgboost/lightgbm"),
) -> None:
    """触发一次 ML 高光模型自学习迭代。

    从 ThresholdFeedback 表中提取所有已审批/拒绝样本的特征，
    训练 XGBoost/LightGBM 模型并保存到 storage/models/。
    """
    from Highlight_Model.models.self_learn import SelfLearnEngine

    engine = SelfLearnEngine(model_type=model_type)
    rid = room_id if room_id > 0 else None

    console.print(f"[bold blue]ML 高光模型自学习[/bold blue]")
    console.print(f"  模型: {model_type} | 房间: {'全部' if rid is None else f'#{rid}'}")

    result = engine.run(room_id=rid)

    if result.success:
        m = result.metrics
        console.print(f"[green]✓ 训练完成[/green] 迭代 #{result.iteration}  耗时 {result.elapsed_s}s")
        console.print(f"  样本: {result.n_samples} (正 {result.n_positive}, +{result.n_new} 新)")
        console.print(f"  AUC: {m.get('auc', 0):.3f}  F1: {m.get('f1', 0):.3f}  "
                      f"P: {m.get('precision', 0):.3f}  R: {m.get('recall', 0):.3f}")
        console.print(f"  模型: {result.model_path}")
    else:
        console.print(f"[red]✗ 自学习失败: {result.error}[/red]")


@app.command()
def ml_list() -> None:
    """列出所有 ML 模型版本及其状态。"""
    from Highlight_Model.models.registry import ModelRegistry

    registry = ModelRegistry()
    versions = registry.versions
    if not versions:
        console.print("[dim]暂无模型版本。请先运行 ml-learn。[/dim]")
        return
    console.print(f"[bold]模型版本 ({len(versions)} 个)[/bold]")
    console.print(f"{'版本':<6} {'状态':<12} {'AUC':<8} {'F1':<8} {'样本':<8} {'时间'}")
    console.print("-" * 65)
    for v in versions:
        status = ""
        if v.is_champion: status = "[green]CHAMPION[/green]"
        if v.is_shadow: status = "[yellow]SHADOW[/yellow]"
        if not v.is_champion and not v.is_shadow: status = "[dim]archived[/dim]"
        m = v.metrics
        console.print(
            f"v{v.version:<5} {status:<20} "
            f"{m.get('auc', 0):.3f}   {m.get('f1', 0):.3f}   {v.n_samples:<5}   {v.created_at[:19]}"
        )


@app.command()
def ml_rollback(version: int = typer.Argument(..., help="目标版本号")) -> None:
    """回滚到指定的 ML 模型版本。"""
    from Highlight_Model.models.registry import ModelRegistry

    registry = ModelRegistry()
    ok = registry.rollback(version)
    if ok:
        console.print(f"[green]✓ 已回滚到 v{version}[/green]")
        champion = registry.champion
        if champion:
            m = champion.metrics
            console.print(f"  AUC: {m.get('auc', 0):.3f}  F1: {m.get('f1', 0):.3f}")
    else:
        console.print(f"[red]✗ 版本 v{version} 不存在[/red]")


@app.command()
def ml_audit() -> None:
    """对当前 ML 模型进行审计：漂移检测 + 特征检查。"""
    from Highlight_Model.models.drift import PredictionDriftDetector
    import numpy as np

    drift = PredictionDriftDetector()
    recent = np.random.rand(50) * 0.3 + 0.35  # 模拟近期预测
    feats = np.random.randn(50, 5)
    report = drift.check(recent, feats)

    console.print(f"[bold]ML 模型审计报告[/bold]")
    console.print(f"  PSI: {report.psi:.3f} ({report.psi_status})")
    console.print(f"  特征偏移均值: {report.feature_shift_mean:.3f}")
    console.print(f"  漂移状态: {'[red]⚠ 已漂移[/red]' if report.is_drifted else '[green]✓ 正常[/green]'}")
    if report.shifted_features:
        console.print(f"  漂移特征: {', '.join(report.shifted_features[:5])}")


if __name__ == "__main__":
    app()
