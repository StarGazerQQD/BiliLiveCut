"""FFmpeg 录制 + 分片器(异步)。

职责:

* 拉取直播流并用 FFmpeg 持续录制;
* 按固定时长(默认 60s)切分为独立片段,保留原始文件以便追溯;
* 断流时以指数退避自动重连(重连前重新获取播放地址,因地址有时效);
* 每当一个片段写完即登记到数据库,并回调下游(转写等)。

分片实现:使用 FFmpeg ``-f segment`` 复用器,并配合
``-segment_list ... -segment_list_type csv`` 让 FFmpeg 在**每个片段完成时**
向一个 CSV 清单追加一行 ``filename,start_time,end_time``。本模块通过 tail 该
清单精确感知"片段已完成",避免读到正在写入的半截文件。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from loguru import logger

from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error
from app.core.paths import session_raw_dir
from app.db.models import (
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SessionStatus,
    utcnow,
)
from app.db.session import get_session
from app.sources.bilibili.client import (
    BilibiliLiveClient,
    StreamInfo,
    pick_best_stream,
)

# 下游回调签名:接收刚登记入库的 RawSegment(已含 id)。
SegmentCallback = Callable[[RawSegment], Awaitable[None]]
# 会话结束回调签名:接收结束的 session_id。
SessionEndCallback = Callable[[int], Awaitable[None]]
StateCallback = Callable[[str, int | None], None]

_SEGMENT_LIST_NAME = "segments.csv"


class Recorder:
    """单个直播间的录制控制器。

    一个实例负责一个直播间的完整录制生命周期(包含多次断流重连)。

    :param room_id: 真实房间号。
    :param db_room_id: ``live_rooms`` 主键,用于关联会话。
    :param on_segment: 可选回调,在每个片段入库后触发(用于驱动下游流水线)。
    :param on_end: 可选回调,在录制会话结束时触发(接收 session_id)。
    """

    def __init__(
        self,
        room_id: int,
        db_room_id: int,
        on_segment: SegmentCallback | None = None,
        on_end: SessionEndCallback | None = None,
        on_state: StateCallback | None = None,
    ) -> None:
        self.room_id = room_id
        self.db_room_id = db_room_id
        self.on_segment = on_segment
        self.on_end = on_end
        self.on_state = on_state
        self._stop = asyncio.Event()
        self._session_id: int | None = None
        self._seq = 0  # 跨重连累加的全局片段序号
        self._paths: set[str] = set()  # 已登记片段路径缓存(避免每次查全表)
        self._danmaku = None  # type: ignore[var-annotated]  # DanmakuClient(可选)
        self._danmaku_task: asyncio.Task[None] | None = None
        self._active_process: asyncio.subprocess.Process | None = None

    @property
    def session_id(self) -> int | None:
        """返回当前录制会话 id;会话尚未创建时为 ``None``。"""
        return self._session_id

    def stop(self) -> None:
        """请求停止录制(优雅退出当前循环)。"""
        self._update_session(status=SessionStatus.STOPPING)
        self._stop.set()

    def force_stop(self) -> None:
        """立即终止当前 FFmpeg,随后仍由主循环执行数据库和回调收尾。"""
        self.stop()
        if self._active_process is not None and self._active_process.returncode is None:
            logger.warning("强制终止 FFmpeg room={} session={}", self.room_id, self._session_id)
            self._active_process.kill()

    def fail(self, message: str) -> None:
        """记录无法由主循环自行收尾的录制异常。"""
        self._update_session(status=SessionStatus.ERROR, error_message=message, ended=True)

    # ------------------------------------------------------------------ #
    # 弹幕采集(与录制并行,贯穿整个会话)
    # ------------------------------------------------------------------ #
    def _start_danmaku(self) -> None:
        """若开启且配置了登录 cookie 则启动弹幕采集后台任务(失败不影响录制)。"""
        if not settings.collect_danmaku or self._session_id is None:
            return
        if not get_bilibili_cookie():
            logger.info("未配置 Bilibili Cookie,跳过弹幕采集(接口需要登录态)。")
            return
        try:
            from app.sources.bilibili.danmaku import DanmakuClient

            self._danmaku = DanmakuClient(
                room_id=self.room_id,
                session_id=self._session_id,
                cookie=get_bilibili_cookie(),
            )
            self._danmaku_task = asyncio.create_task(self._danmaku.run())
            logger.info("弹幕采集已启动 room={} session={}", self.room_id, self._session_id)
        except Exception as exc:  # noqa: BLE001 — 弹幕采集失败不应影响录制
            logger.warning("弹幕采集启动失败 room={}: {}", self.room_id, exc)
            self._danmaku = None
            self._danmaku_task = None

    async def _stop_danmaku(self) -> None:
        """停止弹幕采集任务并等待其退出。"""
        if self._danmaku is not None:
            self._danmaku.stop()
        if self._danmaku_task is not None:
            try:
                await asyncio.wait_for(self._danmaku_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._danmaku_task.cancel()
            except Exception as exc:  # noqa: BLE001
                logger.debug("弹幕任务收尾异常: {}", exc)
        self._danmaku = None
        self._danmaku_task = None

    async def run(self) -> None:
        """启动录制主循环:取流 -> 录制 -> 断流重连,直到被请求停止。

        关键设计:
        - 每次断流都重新调用 ``_fetch_stream`` 获取新播放地址(地址有时效);
        - 超管断流/主播主动下播/网络闪断 均被统一处理为 FFmpeg 退出;
        - 重连成功后首个片段写入即重置退避计数器(backoff→1),
          避免稳定录制后再次断流时无谓等待 30s。
        """
        self._session_id = self._create_session()
        self._emit_state(SessionStatus.STARTING)
        self._seq = 0  # 每次 run() 重新开始片段计数
        self._paths = set()  # 重置路径缓存
        out_dir = session_raw_dir(self._session_id)
        backoff = 1
        reconnect_episode = False  # 当前录制是否为重连后的一次尝试

        # 会话期间并行采集弹幕(用于弹幕热度与高光评分的弹幕维度)。
        self._start_danmaku()

        async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
            while not self._stop.is_set():
                # V0.1.13: Disk protection — safely stop recording if disk critical
                from app.pipeline.storage_lifecycle import should_stop_recording

                if should_stop_recording():
                    logger.warning("磁盘 CRITICAL, 安全停止录制")
                    self.stop()
                    break

                stream = await self._fetch_stream(client)
                if stream is None:
                    # 未开播或暂无流,按轮询间隔等待后重试(不计入重连退避)。
                    self._update_session(status=SessionStatus.RECONNECTING)
                    await self._sleep_or_stop(settings.live_poll_interval_s)
                    continue

                self._update_session(
                    status=SessionStatus.RECORDING,
                    stream_url=stream.url,
                    stream_format=stream.protocol,
                    quality=stream.quality,
                )
                logger.info(
                    "开始录制 room={} 协议={} 清晰度={} reconnect_episode={}",
                    self.room_id,
                    stream.protocol,
                    stream.quality,
                    reconnect_episode,
                )

                # 记录录制前的 seq 用于判断是否产生过片段。
                seq_before = self._seq
                exit_code = await self._record_once(stream, out_dir)
                self._classify_recording_exit(exit_code, getattr(self, "_stderr_tail", None))

                if self._stop.is_set():
                    break

                # ---- 重连成功后重置退避 ----
                # 如果本次录制实际上是重连且成功产出了至少 1 个片段,
                # 说明重连成功、流已稳定,把 backoff 重置为 1。
                # 避免"稳定录制 30 分钟后再次被断流,却要白等 30s"。
                if reconnect_episode and self._seq > seq_before:
                    logger.info(
                        "重连成功并产出片段 room={} seq={}→{}, backoff 重置 30→1。",
                        self.room_id,
                        seq_before,
                        self._seq,
                    )
                    self._update_session(
                        status=SessionStatus.RECONNECTED,
                        reconnected=True,
                    )
                    self._update_session(status=SessionStatus.RECORDING)
                    reconnect_episode = False
                    backoff = 1

                # ---- 断流处理 ----
                # FFmpeg 退出可能是:
                #   a) 超管断流(超管中断推流,主播重新推流后地址可能变)
                #   b) 主播主动下播(无新流,后续 _fetch_stream 返回 None)
                #   c) 网络闪断(主播仍在推,短暂丢包后恢复)
                # 这三种情况都走"重新取流→指数退避→重连"流程。
                reconnect_episode = True
                self._increment_reconnect()
                # -1 = 被我们主动 kill(正常停止),不计为重连。
                if exit_code != -1:
                    self._update_session(status=SessionStatus.RECONNECTING)
                logger.warning(
                    "录制中断 room={} exit_code={},{}s 后重连。",
                    self.room_id,
                    exit_code,
                    backoff,
                )
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, settings.reconnect_max_backoff_s)

        self._update_session(status=SessionStatus.FINALIZING)
        await self._stop_danmaku()
        self._update_session(status=SessionStatus.STOPPED, ended=True)
        logger.info("录制已停止 room={} session={}", self.room_id, self._session_id)

        if self.on_end is not None and self._session_id is not None:
            try:
                await self.on_end(self._session_id)
            except Exception as exc:  # noqa: BLE001 — 结束回调异常不应影响停止流程
                logger.error("会话结束回调失败 session={}: {}", self._session_id, exc)

    # ------------------------------------------------------------------ #
    # 取流
    # ------------------------------------------------------------------ #
    async def _fetch_stream(self, client: BilibiliLiveClient) -> StreamInfo | None:
        """获取并挑选最佳可用流;失败返回 ``None``。

        :param client: 已建立的 Bilibili 客户端。
        :returns: 选中的 :class:`StreamInfo`,或 ``None``(未开播/出错)。
        """
        try:
            streams = await client.get_streams(self.room_id, quality=settings.stream_quality)
        except Exception as exc:  # noqa: BLE001 — 取流失败不应中断主循环
            logger.error("取流失败 room={}: {}", self.room_id, exc)
            self._update_session(error_message=str(exc))
            return None
        return pick_best_stream(streams, settings.preferred_stream_protocol)

    # ------------------------------------------------------------------ #
    # 录制单次(直到 ffmpeg 退出)
    # ------------------------------------------------------------------ #
    async def _record_once(self, stream: StreamInfo, out_dir: Path) -> int:
        """启动一次 FFmpeg 录制,并并发监听片段清单,直到进程退出。

        :param stream: 选中的流。
        :param out_dir: 片段输出目录。
        :returns: FFmpeg 进程退出码。
        """
        segment_list = out_dir / _SEGMENT_LIST_NAME
        # 为本次录制使用唯一的文件名前缀,避免重连后覆盖既有片段。
        prefix = f"part{self._seq:03d}_"
        cmd = self._build_ffmpeg_cmd(stream, out_dir, prefix, segment_list)
        logger.debug("FFmpeg 命令: {}", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        self._active_process = proc
        # 并发:监听片段清单 + 转储 ffmpeg stderr 到日志。
        watcher = asyncio.create_task(self._watch_segments(segment_list, out_dir))
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        # 监听停止信号,主动终止 ffmpeg。
        stopper = asyncio.create_task(self._terminate_on_stop(proc))

        try:
            return await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            for task in (watcher, stderr_task, stopper):
                task.cancel()
            await asyncio.gather(watcher, stderr_task, stopper, return_exceptions=True)
            # 兜底:登记可能尚未从清单读到的最后片段。
            await self._scan_orphan_segments(segment_list, out_dir)
            self._active_process = None

    def _build_ffmpeg_cmd(
        self,
        stream: StreamInfo,
        out_dir: Path,
        prefix: str,
        segment_list: Path,
    ) -> list[str]:
        """构造 FFmpeg 命令行。

        参数含义逐项说明:

        * ``-hide_banner``:不打印版本/构建横幅,日志更干净。
        * ``-loglevel warning``:只输出警告及以上,减少噪音。
        * ``-rw_timeout 15000000``:网络读超时 15s(微秒),断流可被尽快感知。
        * ``-headers``:为拉流请求附加 HTTP 头(B 站 CDN 需要 Referer)。
        * ``-i <url>``:输入直播流地址。
        * ``-c copy``:直接复制音视频流,不转码,**CPU 占用低且不损画质**。
        * ``-f segment``:使用分段复用器,把输出切成多个文件。
        * ``-segment_time N``:每段目标时长(秒)。
        * ``-reset_timestamps 1``:每段时间戳从 0 开始,便于后续独立处理。
        * ``-segment_format mpegts``:分段容器用 MPEG-TS(对流式录制更鲁棒)。
        * ``-segment_list ...`` / ``-segment_list_type csv``:每段完成即向 CSV
          清单追加 ``文件名,起始秒,结束秒``,供本模块精确感知片段完成。
        * 末尾 ``%05d.ts``:输出文件名模板(5 位补零序号)。

        :param stream: 选中的流。
        :param out_dir: 输出目录。
        :param prefix: 本次录制的文件名前缀(避免重连覆盖)。
        :param segment_list: 片段清单 CSV 路径。
        :returns: 可直接传给子进程的参数列表。
        """
        headers = (
            "Referer: https://live.bilibili.com/\r\n"
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36\r\n"
        )
        output_template = str(out_dir / f"{prefix}%05d.ts")
        return [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rw_timeout",
            "15000000",
            "-headers",
            headers,
            "-i",
            stream.url,
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(settings.segment_duration_s),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mpegts",
            "-segment_list",
            str(segment_list),
            "-segment_list_type",
            "csv",
            "-y",
            output_template,
        ]

    # ------------------------------------------------------------------ #
    # 片段清单监听
    # ------------------------------------------------------------------ #
    async def _watch_segments(self, segment_list: Path, out_dir: Path) -> None:
        """tail 片段清单 CSV,逐行将已完成片段登记入库。

        CSV 行格式: ``filename,start_seconds,end_seconds``。
        FFmpeg 在每段写完后追加一行,因此读到一行即代表该段已完整可用。

        :param segment_list: 清单文件路径。
        :param out_dir: 片段所在目录。
        """
        last_pos = 0
        try:
            while True:
                if segment_list.exists():
                    text = segment_list.read_text(encoding="utf-8", errors="ignore")
                    new_text = text[last_pos:]
                    last_pos = len(text)
                    for line in new_text.splitlines():
                        line = line.strip()
                        if line:
                            await self._register_segment(line, out_dir)
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _scan_orphan_segments(self, segment_list: Path, out_dir: Path) -> None:
        """进程退出后兜底扫描:登记清单中尚未处理的行。

        :param segment_list: 清单文件路径。
        :param out_dir: 片段所在目录。
        """
        if not segment_list.exists():
            return
        registered = self._registered_paths()
        for line in segment_list.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            filename = line.split(",")[0]
            if str((out_dir / filename).resolve()) not in registered:
                await self._register_segment(line, out_dir)

    async def _register_segment(self, csv_line: str, out_dir: Path) -> None:
        """解析一行清单并把片段写入数据库,然后触发下游回调。

        :param csv_line: 形如 ``part000_00000.ts,0.000000,60.000000`` 的一行。
        :param out_dir: 片段所在目录。
        """
        parts = csv_line.split(",")
        filename = parts[0]
        file_path = (out_dir / filename).resolve()
        if not file_path.exists():
            logger.debug("清单引用的文件暂不存在,跳过: {}", file_path)
            return

        # 去重:同一文件不重复登记。
        if str(file_path) in self._registered_paths():
            return

        try:
            start_off = float(parts[1])
            end_off = float(parts[2])
            duration = max(0.0, end_off - start_off)
        except (IndexError, ValueError):
            duration = float(settings.segment_duration_s)

        now = utcnow()
        # 用"片段完成时刻"反推起止直播时间(近似,足够下游使用)。
        seg_start = now - timedelta(seconds=duration)
        size = file_path.stat().st_size

        segment = RawSegment(
            session_id=self._session_id or 0,
            seq=self._seq,
            file_path=str(file_path),
            start_ts=seg_start,
            end_ts=now,
            duration_s=duration,
            size_bytes=size,
            status=SegmentStatus.RECORDED,
        )
        with get_session() as db:
            db.add(segment)
            db.flush()  # 取得自增 id
            db.refresh(segment)

        self._seq += 1
        self._paths.add(str(file_path))  # 更新内存缓存,避免后续反复查表
        logger.info(
            "片段已登记 seq={} size={}KB dur={:.1f}s -> {}",
            segment.seq,
            size // 1024,
            duration,
            file_path.name,
        )

        if self.on_segment is not None:
            try:
                await self.on_segment(segment)
            except Exception as exc:  # noqa: BLE001 — 下游异常不应中断录制
                logger.error("下游回调失败 seg={}: {}", segment.id, exc)

    def _registered_paths(self) -> set[str]:
        """返回当前会话已登记片段的路径集合(首次查询后缓存于内存,避免每段查全表)。

        :returns: 已登记文件路径字符串集合。
        """
        if self._paths:
            return self._paths
        from sqlmodel import select

        with get_session() as db:
            rows = db.exec(select(RawSegment.file_path).where(RawSegment.session_id == self._session_id)).all()
        self._paths = set(rows)
        return self._paths

    # ------------------------------------------------------------------ #
    # 进程与会话辅助
    # ------------------------------------------------------------------ #
    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """持续读取并记录 FFmpeg 的 stderr, 缓存最近 N 行用于错误分类。

        :param proc: FFmpeg 子进程。
        """
        if proc.stderr is None:
            return
        self._stderr_tail: list[str] = []
        try:
            async for raw in proc.stderr:
                msg = raw.decode("utf-8", errors="ignore").strip()
                if msg:
                    logger.debug("[ffmpeg] {}", msg)
                    self._stderr_tail.append(msg)
                    if len(self._stderr_tail) > 50:
                        self._stderr_tail.pop(0)
        except asyncio.CancelledError:
            pass

    async def _terminate_on_stop(self, proc: asyncio.subprocess.Process) -> None:
        """等待停止信号,触发后优雅终止 FFmpeg 进程。

        :param proc: FFmpeg 子进程。
        """
        try:
            await self._stop.wait()
            if proc.returncode is None:
                logger.info("收到停止信号,正在终止 FFmpeg ...")
                proc.terminate()
        except asyncio.CancelledError:
            pass

    async def _sleep_or_stop(self, seconds: float) -> None:
        """休眠指定秒数,若期间收到停止信号则提前返回。

        :param seconds: 休眠时长(秒)。
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _create_session(self) -> int:
        """创建一条录制会话记录并返回其 id。

        :returns: 新建 ``recording_sessions`` 的主键。
        """
        session = RecordingSession(
            room_id=self.db_room_id,
            status=SessionStatus.STARTING,
        )
        with get_session() as db:
            db.add(session)
            db.flush()
            db.refresh(session)
            sid = session.id
        logger.info("创建录制会话 session_id={} room={}", sid, self.room_id)
        return int(sid)

    def _update_session(
        self,
        *,
        status: str | None = None,
        stream_url: str | None = None,
        stream_format: str | None = None,
        quality: int | None = None,
        error_message: str | None = None,
        ended: bool = False,
        reconnected: bool = False,
    ) -> None:
        """更新当前会话的字段(仅更新传入的非 ``None`` 项)。

        :param status: 新状态。
        :param stream_url: 当前拉流地址。
        :param stream_format: 流协议。
        :param quality: 清晰度码。
        :param error_message: 错误信息。
        :param ended: 是否标记结束时间。
        :param reconnected: 是否标记最近重连成功时间(V0.1.2 新增)。
        """
        if self._session_id is None:
            return
        with get_session() as db:
            session = db.get(RecordingSession, self._session_id)
            if session is None:
                return
            if status is not None:
                session.status = status
            if stream_url is not None:
                session.stream_url = stream_url
            if stream_format is not None:
                session.stream_format = stream_format
            if quality is not None:
                session.quality = quality
            if error_message is not None:
                session.error_message = error_message
            if ended:
                session.ended_at = utcnow()
            if reconnected:
                session.last_reconnected_at = utcnow()
            db.add(session)
        if status is not None:
            self._emit_state(status)

    def _emit_state(self, status: str) -> None:
        """向管理器同步录制运行状态。"""
        if self.on_state is not None:
            self.on_state(status, self._session_id)

    def _increment_reconnect(self) -> None:
        """重连计数 +1。"""
        if self._session_id is None:
            return
        with get_session() as db:
            session = db.get(RecordingSession, self._session_id)
            if session is not None:
                session.reconnect_count += 1
                db.add(session)

    def _classify_recording_exit(self, exit_code: int, stderr_lines: list[str] | None = None) -> FfmpegErrorType:
        """对录制退出进行分类 (V0.1.13)。

        根据退出码和 stderr 内容判断 FFmpeg 退出原因，
        以便区分永久错误(不再重试)和临时错误(指数退避重连)。

        :param exit_code: FFmpeg 进程退出码。
        :param stderr_lines: 缓存的 stderr 行 (可选)。
        :returns: 分类后的错误类型。
        """
        stderr_text = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
        if exit_code == -1:
            return FfmpegErrorType.CANCELLED  # 主动停止
        error_type = classify_ffmpeg_error(exit_code, stderr_text)
        if error_type in (FfmpegErrorType.DISK_FULL,):
            logger.critical("录制磁盘满, 触发 CRITICAL 保护")
            try:
                from app.notify.webhook import notify_disk_alert

                notify_disk_alert(f"录制磁盘满: 房间 {self.room_id}")
            except Exception:
                pass
        if error_type in (
            FfmpegErrorType.PERMISSION_DENIED,
            FfmpegErrorType.MISSING_BINARY,
            FfmpegErrorType.INVALID_ARGUMENT,
            FfmpegErrorType.UNSUPPORTED_CODEC,
        ):
            logger.error("录制永久错误: {} (exit={})", error_type.name, exit_code)
        return error_type
