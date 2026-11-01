"""内置 Bilibili 来源：平台 qn、Cookie 和风控语义仅在此适配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.core.sanitize import safe_exception_summary
from app.plugins.live_source import (
    DanmakuSink,
    DanmakuSource,
    DanmakuStateSink,
    LiveStateSink,
    LiveStatus,
    MetadataFailure,
    RoomSnapshot,
    SourceAuthenticationError,
    SourceDescriptor,
    SourceInvalidInput,
    SourceRateLimited,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
)
from app.sources.bilibili.client import (
    BilibiliAuthenticationError,
    BilibiliError,
    BilibiliLiveClient,
    BilibiliRateLimitError,
    BilibiliStreamUnavailable,
    BilibiliTransportError,
    HttpErrorType,
    parse_room_id,
)

if TYPE_CHECKING:
    from app.recording.danmaku_archive import DanmakuArchive


class BilibiliSource:
    """按请求创建客户端以读取当前 Cookie，并在取消或异常时关闭连接。"""

    descriptor = SourceDescriptor(platform="bilibili", name="Bilibili", domains=("live.bilibili.com",))

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[BilibiliLiveClient]:
        try:
            async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
                yield client
        except BilibiliAuthenticationError as exc:
            raise SourceAuthenticationError("Bilibili HTTP 401 鉴权被拒绝，请检查平台访问凭据") from exc
        except BilibiliRateLimitError as exc:
            if exc.error_type in {HttpErrorType.COOKIE_EXPIRED, HttpErrorType.ACCOUNT_BANNED}:
                raise SourceAuthenticationError("Bilibili 登录态失效或账号访问被拒绝，请更新平台凭据") from exc
            raise SourceRateLimited("Bilibili 风控或限流", retry_after=max(0.0, exc.retry_after_seconds)) from exc
        except BilibiliStreamUnavailable as exc:
            raise SourceTemporaryError("直播中但画质序列没有可用地址") from exc
        except BilibiliTransportError as exc:
            if not exc.retryable:
                raise SourceUnavailable(f"Bilibili 接口返回不可重试 HTTP {exc.status_code}") from exc
            raise SourceTemporaryError(str(exc), retry_after=exc.retry_after_seconds) from exc
        except BilibiliError as exc:
            raise SourceTemporaryError("Bilibili 接口暂时不可用") from exc

    async def resolve_room(self, value: str) -> SourceRoom:
        """保留数字短号及直播 URL 的解析、真实房间号归一化行为。"""
        try:
            raw_id = parse_room_id(value)
        except BilibiliError as exc:
            raise SourceInvalidInput("请输入 Bilibili 直播 URL 或数字房间号") from exc
        async with self._client() as client:
            info = await client.get_room_info(str(raw_id))
        return SourceRoom(
            platform="bilibili", source_id=str(info.room_id), canonical_url=f"https://live.bilibili.com/{info.room_id}"
        )

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        """1 为开播、0/2 为下播/轮播；未识别状态保留 UNKNOWN。"""
        async with self._client() as client:
            info = await client.get_room_info(room.source_id, include_detail=True)
        if str(info.room_id) != room.source_id:
            raise SourceInvalidInput("Bilibili 返回房间身份发生变化，请重新登记并检查原房间")
        status = {1: LiveStatus.LIVE, 0: LiveStatus.OFFLINE, 2: LiveStatus.OFFLINE}.get(
            info.live_status, LiveStatus.UNKNOWN
        )
        failure = None
        if info.detail_error is not None:
            error = info.detail_error
            code = type(error).__name__
            retry_after = None
            if isinstance(error, BilibiliRateLimitError):
                code = error.error_type.value
                retry_after = error.retry_after_seconds
            elif isinstance(error, BilibiliTransportError):
                code = f"{code}:{error.status_code}"
                retry_after = error.retry_after_seconds
            failure = MetadataFailure(code=code, message=safe_exception_summary(error)[:2000], retry_after=retry_after)
        return RoomSnapshot(status=status, title=info.title, uploader_name=info.uploader_name, metadata_failure=failure)

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        """按宿主画质上限逐级取流；只在 Bilibili 内按 qn、同档协议偏好排序。"""
        quality = settings.stream_quality
        async with self._client() as client:
            streams = await client.get_streams(int(room.source_id), quality=quality)
        streams = [item for item in streams if item.quality is None or item.quality <= quality]
        streams.sort(
            key=lambda item: (
                item.quality if item.quality is not None else -1,
                item.protocol == preference.preferred_transport,
            ),
            reverse=True,
        )
        return [
            StreamSpec(
                url=item.url,
                transport=item.protocol,
                container=item.format_name,
                codec=item.codec_name or None,
                quality_id=str(item.quality) if item.quality is not None else None,
                headers={
                    "Referer": "https://live.bilibili.com/",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            for item in streams
        ]

    async def aclose(self) -> None:
        """请求作用域已关闭所有 HTTP 连接，无共享连接需要释放。"""

    async def collect_live_events(self, room: SourceRoom, emit: LiveStateSink, state: DanmakuStateSink) -> None:
        """独立守候连接仅观察开下播，不受 COLLECT_DANMAKU 或采样率影响。"""
        from app.sources.bilibili.live_events import BilibiliLiveEvents

        client = BilibiliLiveEvents(int(room.source_id), get_bilibili_cookie(), emit, state)
        try:
            await client.run()
        finally:
            client.stop()

    def danmaku_for_session(self, session_id: int, *, archive: DanmakuArchive | None = None) -> DanmakuSource:
        """宿主内部会话适配，保留 Bilibili 既有事件类型、采样和存储语义。"""
        return _BilibiliSessionDanmaku(session_id, archive)


class _BilibiliSessionDanmaku:
    def __init__(self, session_id: int, archive: DanmakuArchive | None) -> None:
        self.session_id = session_id
        self.archive = archive

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        """现有客户端负责 Bili 事件入库，不重复发送为普通文本事件。"""
        from app.analysis.danmaku_sampling import session_sampling_policy
        from app.sources.bilibili.danmaku import DanmakuClient

        client = DanmakuClient(
            room_id=int(room.source_id),
            session_id=self.session_id,
            cookie=get_bilibili_cookie(),
            on_state=state,
            archive=self.archive,
            sampling_policy=lambda: session_sampling_policy(self.session_id),
        )
        try:
            await client.run()
        finally:
            client.stop()
