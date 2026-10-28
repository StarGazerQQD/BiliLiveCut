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
import errno
import math
import os
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from app.core.async_cleanup import complete_cleanup
from app.core.config import settings
from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error
from app.core.paths import session_raw_dir
from app.core.sanitize import sanitize_diagnostic
from app.db.entities import (
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SessionStatus,
    utcnow,
)
from app.db.session import get_session
from app.plugins.live_source import (
    LiveSource,
    SourceError,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
)
from app.recording.danmaku import DanmakuCapture
from app.recording.stream_failover import StreamFailover
from app.recording.stream_race import StreamAttempt, StreamRace
from app.sources.registry import source_registry

# 下游回调签名:接收刚登记入库的 RawSegment(已含 id)。
SegmentCallback = Callable[[RawSegment], Awaitable[None]]
# 会话结束回调签名:接收结束的 session_id。
SessionEndCallback = Callable[[int], Awaitable[None]]
StateCallback = Callable[[str, int | None], None]

_SEGMENT_LIST_NAME = "segments.csv"
_STDERR_LINE_LIMIT = 8192
_STDERR_TAIL_LINES = 20


@dataclass(slots=True)
class _ReconnectBudget:
    """跟踪一次连续断流期间的失败次数和持续时间。"""

    failures: int = 0
    started_at: float | None = None

    def begin(self, now: float) -> None:
        """在尚未计时时记录本次连续断流的起点。"""
        if self.started_at is None:
            self.started_at = now

    def record_failure(self, now: float) -> None:
        """登记一次未能恢复稳定录制的重试。"""
        self.begin(now)
        self.failures += 1

    def reset(self) -> None:
        """稳定产出目标时长的媒体后清空连续失败预算。"""
        self.failures = 0
        self.started_at = None

    def exhaustion_reason(
        self,
        now: float,
        *,
        max_attempts: int,
        max_elapsed_s: int,
    ) -> str | None:
        """返回预算耗尽原因；两个上限为 ``0`` 时分别禁用。"""
        if max_attempts > 0 and self.failures >= max_attempts:
            return f"连续取流重试达到次数上限({self.failures}/{max_attempts} 次)"
        if max_elapsed_s > 0 and self.started_at is not None:
            elapsed_s = max(0.0, now - self.started_at)
            if elapsed_s >= max_elapsed_s:
                return f"连续取流重试达到时长上限({elapsed_s:.1f}/{max_elapsed_s} 秒)"
        return None


class Recorder:
    """单个直播间的录制控制器。

    一个实例负责一个直播间的完整录制生命周期(包含多次断流重连)。

    :param source_room: 稳定平台身份，与数据库房间主键分开。
    :param db_room_id: ``live_rooms`` 主键,用于关联会话。
    :param on_segment: 可选回调,在每个片段入库后触发(用于驱动下游流水线)。
    :param on_end: 可选回调,在录制会话结束时触发(接收 session_id)。
    """

    def __init__(
        self,
        source_room: SourceRoom,
        db_room_id: int,
        on_segment: SegmentCallback | None = None,
        on_end: SessionEndCallback | None = None,
        on_state: StateCallback | None = None,
        metadata_prepared: bool = False,
    ) -> None:
        self.source_room = source_room
        self._source: LiveSource | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._source_retry_delay = 0.0
        self._source_error: str | None = None
        self._source_action_required = False
        self.db_room_id = db_room_id
        self.on_segment = on_segment
        self.on_end = on_end
        self.on_state = on_state
        self._metadata_prepared = metadata_prepared
        self._stop = asyncio.Event()
        self._session_id: int | None = None
        self._seq = 0  # 跨重连累加的全局片段序号
        self._paths: set[str] = set()  # 已登记片段路径缓存(避免每次查全表)
        self._danmaku: DanmakuCapture | None = None
        self._active_process: asyncio.subprocess.Process | None = None
        self._recording_error: str | None = None
        self._recording_action_required = False
        self._stderr_tail: list[str] = []
        self._stderr_categories: set[FfmpegErrorType] = set()
        self._diagnostic_secrets: tuple[str, ...] = ()
        self._attempt_media_seconds = 0.0
        self._upstream_status: int | None = None
        self._last_exit_abnormal = False
        self._stream_failover = StreamFailover()
        self._stream_candidates: list[StreamSpec] = []
        self._selected_stream: StreamSpec | None = None
        self._race_budget_s = 180.0
        self._failed_selection_started_at: float | None = None
        self._race_input_failure = False
        self._stream_race_supported = True

    @property
    def session_id(self) -> int | None:
        """返回当前录制会话 id;会话尚未创建时为 ``None``。"""
        return self._session_id

    def stop(self) -> None:
        """请求停止录制(优雅退出当前循环)。"""
        self._stop.set()
        self._update_session(status=SessionStatus.STOPPING)

    def _stopping(self) -> bool:
        """应用关闭与房间停止共用停止闸门，关闭期间不能重新取流。"""
        from app.pipeline.lifecycle import shutdown_event

        if shutdown_event.is_set():
            self._stop.set()
        return self._stop.is_set()

    def force_stop(self) -> None:
        """立即终止当前 FFmpeg,随后仍由主循环执行数据库和回调收尾。"""
        self.stop()
        if self._active_process is not None and self._active_process.returncode is None:
            logger.warning("强制终止 FFmpeg room={} session={}", self.db_room_id, self._session_id)
            self._active_process.kill()

    def fail(self, message: str) -> None:
        """记录无法由主循环自行收尾的录制异常。"""
        self._update_session(status=SessionStatus.ERROR, error_message=message, ended=True)

    # ------------------------------------------------------------------ #
    # 弹幕采集(与录制并行,贯穿整个会话)
    # ------------------------------------------------------------------ #
    async def _start_danmaku(self) -> None:
        """独立启动可选弹幕，按场次保存能力与实际连接证据。"""
        if self._session_id is not None and self._source is not None:
            self._danmaku = DanmakuCapture(self._source, self.source_room, self.db_room_id, self._session_id)
            await self._danmaku.start()

    async def _stop_danmaku(self) -> None:
        """采集连接完成收尾后才允许释放直播源。"""
        if self._danmaku is not None:
            await self._danmaku.stop()
            self._danmaku = None

    async def _drain_source(self) -> None:
        """来源停用不争抢房间控制锁，也不改写用户的暂停标记。"""
        task = self._run_task
        self.stop()
        if task is None or task.done():
            return
        for timeout, action in ((30, None), (5, self.force_stop), (5, task.cancel)):
            if action is not None:
                action()
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if done:
                await asyncio.gather(task, return_exceptions=True)
                return
        raise SourceUnavailable("录制或弹幕未完成取消，来源保留在停用中，请检查插件资源释放")

    async def run(self) -> None:
        """从资料刷新到末片入库始终持有来源，所有入口共用相同生命周期。"""
        from app.core.runtime_settings import async_settings_scope

        self._run_task = asyncio.current_task()
        try:
            async with source_registry.use(self.source_room.platform, self._drain_source) as source:
                self._source = source
                async with async_settings_scope(fresh=True, boundary="recording"):
                    await self._run_with_settings()
        finally:
            self._source = None
            self._run_task = None

    async def _run_with_settings(self) -> None:
        """统一各录制入口的资料刷新，并保证所有退出路径释放刷新任务。"""
        from app.recording.metadata import begin_session_metadata, end_session_metadata, refresh_room_metadata

        if not self._metadata_prepared:
            await refresh_room_metadata(self.db_room_id)
        if self._stopping():
            return
        self._session_id = self._create_session()
        begin_session_metadata(self._session_id)
        metadata_task = asyncio.create_task(self._refresh_metadata_loop())
        failed: str | None = None
        try:
            await self._run_session()
        except asyncio.CancelledError:
            failed = "录制任务被取消"
            raise
        except OSError as exc:
            from app.core.sanitize import safe_exception_summary

            failed = self._recording_error or f"录制文件或进程失败：{safe_exception_summary(exc)}"
            self._recording_error = failed
            self._recording_action_required = True
            raise
        except Exception as exc:
            # 插件与子进程边界；不持久化可能包含 URL/请求头的异常正文。
            from app.core.sanitize import safe_exception_summary

            failed = self._recording_error or f"录制失败：{safe_exception_summary(exc)}"
            raise
        finally:

            async def cleanup() -> None:
                metadata_task.cancel()
                await asyncio.gather(metadata_task, return_exceptions=True)
                try:
                    await self._stop_danmaku()
                    await self._finalize_session()
                finally:
                    end_session_metadata(self._session_id)
                    if failed is not None:
                        self.fail(failed)

            await complete_cleanup(cleanup())

    async def _refresh_metadata_loop(self) -> None:
        """录制及重连期间独立刷新标题，停止时由录制生命周期取消。"""
        from sqlalchemy.exc import SQLAlchemyError

        from app.recording.metadata import refresh_room_metadata

        while not self._stopping():
            await self._sleep_or_stop(settings.room_metadata_refresh_interval_s)
            if not self._stopping():
                try:
                    await refresh_room_metadata(self.db_room_id)
                except SQLAlchemyError:
                    logger.exception("标题持久化失败 room={} session={}，下一轮重试", self.db_room_id, self._session_id)

    async def _run_session(self) -> None:
        """启动录制主循环:取流 -> 录制 -> 断流重连,直到被请求停止。

        关键设计:
        - 每次断流都重新调用 ``_fetch_stream`` 获取新播放地址(地址有时效);
        - 人工停止和干净 EOF 不记作异常；永久错误阻止自动重启;
        - 累计产出至少一个目标分段时长才重置预算，短末片不代表恢复。
        """
        self._emit_state(SessionStatus.STARTING)
        self._seq = 0  # 每次 run() 重新开始片段计数
        self._paths = set()  # 重置路径缓存
        self._stream_failover.reset()
        out_dir = session_raw_dir(self._session_id)
        backoff = 1
        reconnect_episode = False  # 当前录制是否为重连后的一次尝试
        reconnect_budget = _ReconnectBudget()

        # 会话期间并行采集弹幕(用于弹幕热度与高光评分的弹幕维度)。
        await self._start_danmaku()

        while not self._stopping():
            # V0.1.13: Disk protection — safely stop recording if disk critical
            from app.pipeline.storage_lifecycle import should_stop_recording

            if await asyncio.to_thread(should_stop_recording):
                self._recording_action_required = True
                self._recording_error = "磁盘空间不足，请清理存储后手动恢复录制"
                logger.error("{} room={} session={}", self._recording_error, self.db_room_id, self._session_id)
                self.stop()
                break

            if self._retry_exhausted(reconnect_budget):
                break

            if self._stopping():
                break
            stream = await self._fetch_stream()
            if self._stopping():
                break
            if stream is None:
                # 未开播、已下播或暂时无法取流：计入连续失败预算。
                reconnect_budget.record_failure(time.monotonic())
                if self._retry_exhausted(reconnect_budget):
                    break
                self._update_session(status=SessionStatus.RECONNECTING)
                delay = self._retry_delay(
                    max(settings.live_poll_interval_s, self._source_retry_delay), reconnect_budget
                )
                await self._sleep_or_stop(delay)
                continue

            self._update_session(
                status=SessionStatus.RECORDING,
                stream_format=stream.transport,
            )
            self._save_stream_selection(stream)
            logger.info(
                "开始录制 room={} 协议={} 清晰度={} reconnect_episode={}",
                self.db_room_id,
                stream.transport,
                stream.quality_id,
                reconnect_episode,
            )

            # 记录录制前的 seq 用于判断是否产生过片段。
            seq_before = self._seq
            self._attempt_media_seconds = 0.0
            self._stderr_tail = []
            self._stderr_categories = set()
            self._upstream_status = None
            self._selected_stream = None
            self._failed_selection_started_at = None
            self._race_input_failure = False
            # 竞争和重连共用时间预算，不能因为候选较多而无限延长一轮。
            _, max_elapsed = self._reconnect_limits()
            self._race_budget_s = (
                max(0.0, max_elapsed - (time.monotonic() - reconnect_budget.started_at))
                if max_elapsed and reconnect_budget.started_at is not None
                else float(max_elapsed or 180)
            )
            exit_code = await self._record_once(stream, out_dir)
            stream = self._selected_stream or stream
            if self._stopping():
                break
            self._last_exit_abnormal = exit_code != 0
            error_type = (
                self._classify_recording_exit(exit_code, self._stderr_tail)
                if self._last_exit_abnormal
                else FfmpegErrorType.UNKNOWN
            )
            stable_recording = self._seq > seq_before and self._attempt_media_seconds >= settings.segment_duration_s
            if stable_recording:
                reconnect_budget.reset()
                self._stream_failover.reset()

            # ---- 重连成功后重置退避 ----
            # 短末片不能证明恢复；实际媒体达到目标分段长度才重置退避。
            # 避免"稳定录制 30 分钟后再次被断流,却要白等 30s"。
            if reconnect_episode and stable_recording:
                logger.info(
                    "重连成功并产出片段 room={} seq={}→{}, backoff 重置 30→1。",
                    self.db_room_id,
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

            # HTTP 403 等可能是临时播放地址失效；有限预算内重新取流，
            # 不把它直接解释为 Cookie 过期。未知错误同样不能无限重试。
            permanent = error_type in {
                FfmpegErrorType.DISK_FULL,
                FfmpegErrorType.PERMISSION_DENIED,
                FfmpegErrorType.MISSING_BINARY,
                FfmpegErrorType.INVALID_ARGUMENT,
                FfmpegErrorType.UNSUPPORTED_CODEC,
            }
            retry_started_at = time.monotonic()
            if self._failed_selection_started_at is not None:
                # 首轮全失败的竞争也必须计时，不能从竞争结束才开始预算。
                reconnect_budget.begin(self._failed_selection_started_at)
            if not permanent:
                # 无论是断流还是提前 EOF，都尝试其他线路；人工停止在上面已退出。
                self._stream_failover.failed(stream, now=retry_started_at)
            if stable_recording:
                reconnect_budget.begin(retry_started_at)
            else:
                reconnect_budget.record_failure(retry_started_at)
            if permanent:
                self._recording_action_required = True
                self._recording_error = f"FFmpeg {error_type.name}，请检查磁盘、权限或媒体配置后手动恢复录制"
            exhausted = False if permanent else self._retry_exhausted(reconnect_budget)
            retry = not permanent and not exhausted
            delay = min(backoff * random.uniform(0.8, 1.2), settings.reconnect_max_backoff_s) if retry else 0.0
            try_alternative = self._stream_failover.has_untried_alternative
            if try_alternative:
                delay = min(delay, 1.0)
            delay = self._retry_delay(delay, reconnect_budget)
            if exit_code != 0:
                if exhausted:
                    self._recording_error = "FFmpeg 连续异常退出，重试预算耗尽，等待下一次开播或手动启动"
                self._log_recording_exit(exit_code, error_type, reconnect_budget, retry, delay)
            else:
                logger.info(
                    "录制流正常结束 room={} session={} exit_code=0 retry={} next_wait_s={:.2f}",
                    self.db_room_id,
                    self._session_id,
                    retry,
                    delay,
                )
            if not retry:
                break
            reconnect_episode = True
            self._increment_reconnect()
            self._update_session(status=SessionStatus.RECONNECTING)
            await self._sleep_or_stop(delay)
            backoff = 1 if try_alternative else min(backoff * 2, settings.reconnect_max_backoff_s)

    async def _finalize_session(self) -> None:
        """正常停止、取消和异常都执行一次会话收尾。"""
        self._update_session(status=SessionStatus.FINALIZING)
        await self._stop_danmaku()
        self._update_session(
            status=SessionStatus.ERROR if self._recording_error else SessionStatus.STOPPED,
            error_message=self._recording_error,
            ended=True,
        )
        logger.info("录制已停止 room={} session={}", self.db_room_id, self._session_id)

        if self.on_end is not None and self._session_id is not None:
            try:
                await self.on_end(self._session_id)
            except Exception as exc:  # noqa: BLE001 — 结束回调异常不应影响停止流程
                logger.error("会话结束回调失败 session={}: {}", self._session_id, exc)

    def _retry_exhausted(self, budget: _ReconnectBudget) -> bool:
        """检查连续取流预算，并在耗尽时记录自动停止原因。"""
        max_attempts, max_elapsed = self._reconnect_limits()
        reason = budget.exhaustion_reason(
            time.monotonic(),
            max_attempts=max_attempts,
            max_elapsed_s=max_elapsed,
        )
        if reason is None:
            return False
        self._retry_budget_exhausted = True
        message = f"{reason}，自动结束本场录制"
        if self._last_exit_abnormal or self._source_error is not None:
            self._recording_error = f"{message}；{self._source_error}" if self._source_error is not None else message
        self._update_session(error_message=message)
        logger.info("{} room={} session={}", message, self.db_room_id, self._session_id)
        return True

    @staticmethod
    def _reconnect_limits() -> tuple[int, int]:
        """保留单项禁用语义；两项均为零时仍以 20 次防止无界重启。"""
        attempts = settings.recording_reconnect_max_attempts
        elapsed = settings.recording_reconnect_max_elapsed_s
        return (attempts or (20 if elapsed == 0 else 0)), elapsed

    def _retry_delay(self, proposed: float, budget: _ReconnectBudget) -> float:
        """所有等待均受剩余时间预算约束，包括来源要求的 retry_after。"""
        _, elapsed = self._reconnect_limits()
        if elapsed > 0 and budget.started_at is not None:
            return min(proposed, max(0.0, elapsed - (time.monotonic() - budget.started_at)))
        return proposed

    @property
    def recording_action_required(self) -> bool:
        """录制端永久故障需显式恢复，不能被开播监控重新拉起。"""
        return self._recording_action_required

    @property
    def recording_error(self) -> str | None:
        """返回最终录制错误，供任务管理器展示相同的处理原因。"""
        return self._recording_error

    def _log_recording_exit(
        self, exit_code: int, error_type: FfmpegErrorType, budget: _ReconnectBudget, retry: bool, delay: float
    ) -> None:
        """每次异常只写一条后台可见摘要，详细受限 stderr 留在文件日志。"""
        attempts, elapsed = self._reconnect_limits()
        tail = "\n".join(self._stderr_tail)
        fatal = [line for line in self._stderr_tail if classify_ffmpeg_error(1, line) == FfmpegErrorType.PROCESS_CRASH]
        diagnostic = "\n".join(fatal) if fatal else tail
        summary = (
            sanitize_diagnostic(diagnostic, secrets=self._diagnostic_secrets, limit=max(len(diagnostic), 600))[-600:]
            or "无 stderr，请检查 FFmpeg 安装及上游日志"
        )
        logger.log(
            "WARNING" if retry else "ERROR",
            "录制异常 room={} session={} exit_code={} category={} http_status={} retry={} "
            "attempt={}/{} elapsed_budget_s={} next_wait_s={:.2f} reason={} stderr={}",
            self.db_room_id,
            self._session_id,
            exit_code,
            error_type.name,
            self._upstream_status,
            retry,
            budget.failures,
            attempts,
            elapsed,
            delay,
            self._recording_error or "重新获取播放地址",
            summary,
        )
        logger.debug("FFmpeg stderr tail room={} session={}\n{}", self.db_room_id, self._session_id, tail)

    @property
    def retry_budget_exhausted(self) -> bool:
        """返回本场录制是否因连续取流失败耗尽重试预算。"""
        return bool(getattr(self, "_retry_budget_exhausted", False))

    @property
    def source_action_required(self) -> bool:
        """返回永久取流错误是否需要用户处理后显式恢复自动录制。"""
        return self._source_action_required

    # ------------------------------------------------------------------ #
    # 取流
    # ------------------------------------------------------------------ #
    async def _fetch_stream(self) -> StreamSpec | None:
        """每次重连重新取流；永久来源错误结束当前录制，临时失败使用原预算。"""
        self._source_retry_delay = 0.0
        self._source_error = None
        self._stream_candidates = []
        try:
            streams = await source_registry.get_streams(
                self.source_room, StreamPreference(preferred_transport=settings.preferred_stream_protocol)
            )
        except SourceTemporaryError as exc:
            self._source_retry_delay = exc.retry_after or 0.0
            self._source_error = f"取流暂时失败：{exc.code}"
            self._update_session(error_message=f"取流暂时失败：{exc.code}")
            logger.opt(exception=exc).warning(
                "取流失败 db_room={} platform={} session={} code={} retry_after={}",
                self.db_room_id,
                self.source_room.platform,
                self.session_id,
                exc.code,
                exc.retry_after,
            )
            return None
        except SourceError as exc:
            self._source_action_required = True
            logger.opt(exception=exc).error(
                "来源需要处理 room={} platform={} session={} code={}",
                self.db_room_id,
                self.source_room.platform,
                self.session_id,
                exc.code,
            )
            self._update_session(error_message=f"来源需要处理：{exc.code}")
            self.stop()
            return None
        self._stream_candidates = self._stream_failover.ranked(streams, now=time.monotonic())
        stream = self._stream_candidates[0] if self._stream_candidates else None
        if stream is not None:
            logger.info(
                "录制线路选择 room={} session={} candidate={}/{} endpoint={} transport={} container={}",
                self.db_room_id,
                self._session_id,
                next(index for index, candidate in enumerate(streams, 1) if candidate is stream),
                len(streams),
                sanitize_diagnostic(stream.url),
                stream.transport,
                stream.container,
            )
        return stream

    def _save_stream_selection(self, stream: StreamSpec) -> None:
        """只保存非敏感播放描述，临时 URL 和请求头不进入普通持久字段。"""
        import json

        from app.db.entities import AppSetting

        with get_session() as db:
            key = f"session_stream:{self._session_id}"
            row = db.get(AppSetting, key) or AppSetting(key=key)
            row.value = json.dumps(
                {
                    "version": 1,
                    "platform": self.source_room.platform,
                    "source_id": self.source_room.source_id,
                    "transport": stream.transport,
                    "container": stream.container,
                    "quality_id": stream.quality_id,
                    "quality_label": stream.quality_label,
                    "codec": stream.codec,
                },
                ensure_ascii=False,
            )
            db.add(row)

    # ------------------------------------------------------------------ #
    # 录制单次(直到 ffmpeg 退出)
    # ------------------------------------------------------------------ #
    async def _record_once(self, stream: StreamSpec, out_dir: Path) -> int:
        """启动一次 FFmpeg 录制,并并发监听片段清单,直到进程退出。

        :param stream: 选中的流。
        :param out_dir: 片段输出目录。
        :returns: FFmpeg 进程退出码。
        """
        if (
            len(self._stream_candidates) > 1
            and settings.recording_stream_race_parallel > 1
            and self._stream_race_supported
        ):
            return await self._record_race(out_dir)
        return await self._record_single(stream, out_dir)

    async def _record_single(self, stream: StreamSpec, out_dir: Path) -> int:
        """沿用上游单进程录制，供串行模式及能力不受支持时回退。"""
        segment_list = out_dir / _SEGMENT_LIST_NAME
        # 为本次录制使用唯一的文件名前缀,避免重连后覆盖既有片段。
        prefix = f"part{self._seq:03d}_{uuid.uuid4().hex[:12]}_"
        cmd = self._build_ffmpeg_cmd(stream, out_dir, prefix, segment_list)
        logger.debug(
            "启动 FFmpeg db_room={} session={} transport={}", self.db_room_id, self._session_id, stream.transport
        )

        self._diagnostic_secrets = tuple(stream.headers.values())
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **(
                    {"creationflags": 0x00000200}  # CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else {"start_new_session": True}
                ),
            )
        except OSError as exc:
            category = {
                errno.ENOENT: FfmpegErrorType.MISSING_BINARY,
                errno.EACCES: FfmpegErrorType.PERMISSION_DENIED,
                errno.EPERM: FfmpegErrorType.PERMISSION_DENIED,
                errno.ENOSPC: FfmpegErrorType.DISK_FULL,
            }.get(exc.errno, FfmpegErrorType.UNKNOWN)
            self._stderr_categories.add(category)
            self._stderr_tail = [sanitize_diagnostic(f"{type(exc).__name__}: {exc}", secrets=self._diagnostic_secrets)]
            self._diagnostic_secrets = ()
            return 1

        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        return await self._supervise_process(proc, out_dir, prefix, segment_list, stderr_task)

    def _adopt_attempt_diagnostics(self, attempt: StreamAttempt) -> None:
        """共享胜出者的有界诊断缓冲，后续读取仍能补充错误分类。"""
        self._stderr_tail = attempt.tail
        self._stderr_categories = attempt.categories
        self._upstream_status = attempt.http_status

    async def _record_race(self, out_dir: Path) -> int:
        """竞争期间没有入库回调，胜出后复用原进程及已经写入的视频。"""
        race = StreamRace(
            self._stream_candidates,
            out_dir,
            self._seq,
            self._build_ffmpeg_cmd,
            self._stop,
            lambda stream: self._stream_failover.failed(stream, now=time.monotonic()),
            parallel=settings.recording_stream_race_parallel,
            timeout_s=settings.recording_stream_race_timeout_s,
            budget_s=self._race_budget_s,
        )
        logger.info(
            "并发录制选线 room={} session={} candidates={} parallel={}",
            self.db_room_id,
            self._session_id,
            len(race.attempts),
            settings.recording_stream_race_parallel,
        )
        selection_started_at = time.monotonic()
        selection = asyncio.create_task(race.run())
        disk_guard = asyncio.create_task(self._monitor_disk())
        attempt = None
        supervised = False
        try:
            done, _ = await asyncio.wait({selection, disk_guard}, return_when=asyncio.FIRST_COMPLETED)
            if disk_guard in done:
                try:
                    await disk_guard
                except Exception:
                    self._recording_action_required = True
                    self._recording_error = "录制辅助任务失败 stage=disk"
                    raise
            attempt = await selection
            if self._stopping():
                return -1
            if attempt is None:
                resource_errno = next(
                    (failed.resource_errno for failed in race.failures if failed.resource_errno is not None), None
                )
                if resource_errno is not None:
                    raise OSError(resource_errno, f"本机录制资源不足：{errno.errorcode[resource_errno]}")
                if any(failed.progress_unsupported for failed in race.failures) and not any(
                    failed.global_error and not failed.progress_unsupported for failed in race.failures
                ):
                    # 缺少观测能力不意味着无法录制；本场次回到上游已有单进程路径。
                    self._stream_race_supported = False
                    logger.info(
                        "FFmpeg 不支持 progress，回退串行录制 room={} session={}", self.db_room_id, self._session_id
                    )
                    disk_guard.cancel()
                    await asyncio.gather(disk_guard, return_exceptions=True)
                    if self._stopping():
                        return -1
                    self._selected_stream = self._stream_candidates[0]
                    return await self._record_single(self._selected_stream, out_dir)
                self._failed_selection_started_at = selection_started_at
                self._race_input_failure = bool(race.failures) and not any(
                    failed.global_error for failed in race.failures
                )
                for failed in race.failures:
                    self._stderr_categories.update(failed.categories)
                    self._stderr_tail.extend(failed.tail)
                    self._upstream_status = failed.http_status or self._upstream_status
                del self._stderr_tail[:-_STDERR_TAIL_LINES]
                return 1
            assert attempt.process is not None and attempt.stderr_task is not None and attempt.progress_task is not None
            self._selected_stream = attempt.stream
            self._adopt_attempt_diagnostics(attempt)
            self._diagnostic_secrets = tuple(attempt.stream.headers.values())
            self._update_session(stream_format=attempt.stream.transport)
            self._save_stream_selection(attempt.stream)
            logger.info(
                "录制线路胜出 room={} session={} endpoint={} transport={} container={} pid={} reuse=True",
                self.db_room_id,
                self._session_id,
                sanitize_diagnostic(attempt.stream.url),
                attempt.stream.transport,
                attempt.stream.container,
                attempt.process.pid,
            )
            disk_guard.cancel()
            # 直接进入带 finally 的监控，避免交接期间出现无人负责的取消窗口。
            supervised = True
            return await self._supervise_process(
                attempt.process,
                out_dir,
                attempt.prefix,
                attempt.segment_list,
                attempt.stderr_task,
                extra_tasks=(attempt.progress_task,),
                race_attempt=attempt,
            )
        finally:

            async def cleanup() -> None:
                selection.cancel()
                disk_guard.cancel()
                await asyncio.gather(selection, disk_guard, return_exceptions=True)
                try:
                    if not supervised:
                        # 交接前停止或保存线路信息失败：没有候选进入登记流程。
                        race.winner = None
                    await race.close()
                finally:
                    if attempt is not None:
                        self._upstream_status = attempt.http_status
                    self._active_process = None
                    self._diagnostic_secrets = ()

            await complete_cleanup(cleanup())

    async def _supervise_process(
        self,
        proc: asyncio.subprocess.Process,
        out_dir: Path,
        prefix: str,
        segment_list: Path,
        stderr_task: asyncio.Task[None],
        *,
        extra_tasks: tuple[asyncio.Task[None], ...] = (),
        race_attempt: StreamAttempt | None = None,
    ) -> int:
        """复用已启动的进程，统一监控分片、管道、磁盘与停止信号。"""
        self._active_process = proc
        # 并发:监听片段清单 + 转储 ffmpeg stderr 到日志。
        watcher = asyncio.create_task(self._watch_segments(segment_list, out_dir))
        # 监听停止信号,主动终止 ffmpeg。
        stopper = asyncio.create_task(self._terminate_on_stop(proc))
        disk_guard = asyncio.create_task(self._monitor_disk())
        waiter = asyncio.create_task(proc.wait())

        try:
            supervisors = {watcher: "segments", stderr_task: "stderr", stopper: "stop", disk_guard: "disk"}
            supervisors.update({task: "progress" for task in extra_tasks})
            pending = {waiter, *supervisors}
            while pending:
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done - {waiter}:
                    try:
                        await task
                    except Exception as exc:
                        from app.core.sanitize import safe_exception_summary

                        self._recording_action_required = True
                        self._recording_error = (
                            f"录制辅助任务失败 stage={supervisors[task]}: {safe_exception_summary(exc)}"
                        )
                        logger.opt(exception=exc).error(
                            "录制辅助任务失败 room={} session={} stage={}",
                            self.db_room_id,
                            self.session_id,
                            supervisors[task],
                        )
                        raise
                pending.difference_update(done)
                if waiter in done:
                    break
            return await waiter
        finally:

            async def cleanup() -> None:
                try:
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass  # 检查 returncode 后进程自行退出；继续回收。
                        await proc.wait()
                    # 进程已退出后排空管道，避免取消读取导致最后一条根异常丢失。
                    await stderr_task
                finally:
                    for task in (watcher, stderr_task, stopper, disk_guard, waiter, *extra_tasks):
                        task.cancel()
                    await asyncio.gather(
                        watcher, stderr_task, stopper, disk_guard, waiter, *extra_tasks, return_exceptions=True
                    )
                    try:
                        # 兜底:登记可能尚未从清单读到的最后片段。
                        await self._scan_orphan_segments(segment_list, out_dir, prefix=prefix)
                        if race_attempt is not None:
                            # watcher 和进程均已退出，末片登记成功后才删除清单。
                            # 登记异常时保留 CSV 与录像，避免破坏恢复依据。
                            await race_attempt.cleanup_files(discard=False)
                    finally:
                        self._active_process = None
                        self._diagnostic_secrets = ()

            await complete_cleanup(cleanup())

    async def _monitor_disk(self) -> None:
        """在 FFmpeg 运行期间持续检查空间，通过现有停止信号优雅收尾。"""
        from app.pipeline.storage_lifecycle import should_stop_recording

        while not self._stopping():
            if await asyncio.to_thread(should_stop_recording):
                self._recording_action_required = True
                self._recording_error = "磁盘空间不足，请清理存储后手动恢复录制"
                logger.error(
                    "录制期间磁盘空间进入紧急状态，停止录制 room={} session={}", self.db_room_id, self.session_id
                )
                self.stop()
                return
            await self._sleep_or_stop(1.0)

    def _build_ffmpeg_cmd(
        self,
        stream: StreamSpec,
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
        stream = StreamSpec.model_validate(stream.model_dump())
        headers = "".join(f"{key}: {value}\r\n" for key, value in stream.headers.items())
        output_template = str(out_dir / f"{prefix}%05d.ts")
        return [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rw_timeout",
            "15000000",
            "-protocol_whitelist",
            # HTTPS through an inherited HTTP proxy uses a nested CONNECT protocol.
            "http,https,tcp,tls,crypto,httpproxy",
            *(["-headers", headers] if headers else []),
            # HLS 初始化段失败后复用 HTTP 连接可能触发 FFmpeg 的 v->input 断言。
            *(["-http_persistent", "0"] if stream.transport == "hls" else []),
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

    async def _scan_orphan_segments(self, segment_list: Path, out_dir: Path, *, prefix: str = "part") -> None:
        """进程退出后补读清单，再探测本次录制中没有清单记录的末片。"""
        from app.clipping.core import probe_media

        if segment_list.exists():
            for line in segment_list.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip():
                    await self._register_segment(line.strip(), out_dir)
        for path in sorted(out_dir.glob(f"{prefix}*.ts")):
            if str(path.resolve()) in self._registered_paths() or not path.is_file():
                continue
            end_ts = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if path.stat().st_size == 0:
                continue
            duration, width, height = await asyncio.to_thread(probe_media, str(path))
            if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
                logger.warning("末片不可播放，保留文件等待检查: {}", path.name)
                continue
            await self._register_segment(f"{path.name},0,{duration}", out_dir, end_ts=end_ts)

    async def _register_segment(self, csv_line: str, out_dir: Path, *, end_ts: datetime | None = None) -> None:
        """解析一行清单并把片段写入数据库,然后触发下游回调。

        :param csv_line: 形如 ``part000_00000.ts,0.000000,60.000000`` 的一行。
        :param out_dir: 片段所在目录。
        """
        parts = csv_line.split(",")
        filename = parts[0]
        file_path = (out_dir / filename).resolve()
        if not file_path.is_relative_to(out_dir.resolve()):
            logger.warning("片段路径超出录制目录，拒绝登记: {}", filename)
            return
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
            # 强停可能截断清单，退出后由探测恢复真实时长。
            return

        if not math.isfinite(duration) or duration <= 0:
            return
        now = end_ts or datetime.fromtimestamp(file_path.stat().st_mtime, UTC)
        # 用文件完成时刻反推起止时间，避免恢复探测耗时引起时间漂移。
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
        self._attempt_media_seconds += duration
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
            rows = db.exec(select(RawSegment).where(RawSegment.session_id == self._session_id)).all()
        self._seq = max(self._seq, max((row.seq + 1 for row in rows), default=0))
        self._paths = {row.file_path for row in rows}
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
        pending = b""
        oversized = False
        while chunk := await proc.stderr.read(4096):
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                if not oversized and len(raw) <= _STDERR_LINE_LIMIT:
                    self._remember_stderr(raw)
                else:
                    self._remember_stderr(b"[oversized stderr line omitted]")
                oversized = False
            if len(pending) > _STDERR_LINE_LIMIT:
                pending = b""
                oversized = True
        if pending or oversized:
            self._remember_stderr(b"[oversized stderr line omitted]" if oversized else pending)

    def _remember_stderr(self, raw: bytes) -> None:
        """只在有界内存中分类原文，脱敏后才保存末尾诊断。"""
        msg = raw.decode("utf-8", errors="replace").strip()
        if not msg:
            return
        self._stderr_categories.add(classify_ffmpeg_error(1, msg))
        status = re.search(r"(?:server returned|http error)\s+([45]\d\d)", msg, re.IGNORECASE)
        if status:
            self._upstream_status = int(status[1])
        self._stderr_tail.append(sanitize_diagnostic(msg, secrets=self._diagnostic_secrets, limit=512))
        del self._stderr_tail[:-_STDERR_TAIL_LINES]

    async def _terminate_on_stop(self, proc: asyncio.subprocess.Process) -> None:
        """等待停止信号,触发后优雅终止 FFmpeg 进程。

        :param proc: FFmpeg 子进程。
        """
        try:
            while not self._stopping():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.2)
                except TimeoutError:
                    pass
            if proc.returncode is None:
                logger.info("收到停止信号,正在终止 FFmpeg ...")
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(b"q\n")
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        logger.debug("FFmpeg 输入已关闭 session={}", self._session_id)
                else:
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except TimeoutError:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
        except asyncio.CancelledError:
            pass

    async def _sleep_or_stop(self, seconds: float) -> None:
        """休眠指定秒数,若期间收到停止信号则提前返回。

        :param seconds: 休眠时长(秒)。
        """
        deadline = time.monotonic() + seconds
        while not self._stopping() and (remaining := deadline - time.monotonic()) > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(remaining, 0.2))
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
        logger.info("创建录制会话 session_id={} room={}", sid, self.db_room_id)
        return int(sid)

    def _update_session(
        self,
        *,
        status: str | None = None,
        stream_format: str | None = None,
        error_message: str | None = None,
        ended: bool = False,
        reconnected: bool = False,
    ) -> None:
        """更新当前会话的字段(仅更新传入的非 ``None`` 项)。

        :param status: 新状态。
        :param stream_format: 流协议。
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
            if stream_format is not None:
                session.stream_format = stream_format
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
        categories = self._stderr_categories | {classify_ffmpeg_error(exit_code, stderr_text)}
        if self._race_input_failure:
            # 诊断保留原文，但单路输入的 Invalid argument/权限拒绝不能暂停整个房间。
            categories -= {
                FfmpegErrorType.PERMISSION_DENIED,
                FfmpegErrorType.INVALID_ARGUMENT,
                FfmpegErrorType.MISSING_BINARY,
                FfmpegErrorType.UNSUPPORTED_CODEC,
            }
            categories.add(
                FfmpegErrorType.UPSTREAM_UNAVAILABLE if self._upstream_status else FfmpegErrorType.TRANSIENT_NETWORK
            )
        # 先前的网络警告不能遮住随后发生的磁盘/配置故障。
        priority = (
            FfmpegErrorType.PROCESS_CRASH,
            FfmpegErrorType.DISK_FULL,
            FfmpegErrorType.PERMISSION_DENIED,
            FfmpegErrorType.MISSING_BINARY,
            FfmpegErrorType.UNSUPPORTED_CODEC,
            FfmpegErrorType.INVALID_ARGUMENT,
            FfmpegErrorType.CORRUPTED_INPUT,
            FfmpegErrorType.UPSTREAM_UNAVAILABLE,
            FfmpegErrorType.TRANSIENT_NETWORK,
            FfmpegErrorType.CANCELLED,
            FfmpegErrorType.UNKNOWN,
        )
        return next(item for item in priority if item in categories)
