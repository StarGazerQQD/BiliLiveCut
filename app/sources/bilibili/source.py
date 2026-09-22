"""内置 Bilibili 来源：平台 qn、Cookie 和风控语义仅在此适配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.plugins.live_source import (
    DanmakuSink,
    DanmakuSource,
    DanmakuStateSink,
    LiveStatus,
    RoomSnapshot,
    SourceAuthenticationError,
    SourceDescriptor,
    SourceInvalidInput,
    SourceRateLimited,
    SourceRoom,
    SourceTemporaryError,
    StreamPreference,
    StreamSpec,
)
from app.sources.bilibili.client import (
    BilibiliError,
    BilibiliLiveClient,
    BilibiliRateLimitError,
    HttpErrorType,
    parse_room_id,
)


class BilibiliSource:
    """按请求创建客户端以读取当前 Cookie，并在取消或异常时关闭连接。"""

    descriptor = SourceDescriptor(platform="bilibili", name="Bilibili", domains=("live.bilibili.com",))

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[BilibiliLiveClient]:
        try:
            async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
                yield client
        except BilibiliRateLimitError as exc:
            if exc.error_type in {HttpErrorType.COOKIE_EXPIRED, HttpErrorType.ACCOUNT_BANNED}:
                raise SourceAuthenticationError("Bilibili 登录态失效或账号访问被拒绝，请更新平台凭据") from exc
            raise SourceRateLimited("Bilibili 风控或限流", retry_after=max(0.0, exc.retry_after_seconds)) from exc
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
        return RoomSnapshot(status=status, title=info.title, uploader_name=info.uploader_name)

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        """Bilibili qn 仍取宿主既有设置；其数字只在此平台内排序。"""
        async with self._client() as client:
            streams = await client.get_streams(int(room.source_id), quality=settings.stream_quality)
        streams.sort(key=lambda item: (item.protocol == preference.preferred_transport, item.quality), reverse=True)
        return [
            StreamSpec(
                url=item.url,
                transport=item.protocol,
                container=item.format_name,
                codec=item.codec_name or None,
                quality_id=str(item.quality),
                headers={
                    "Referer": "https://live.bilibili.com/",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            for item in streams
        ]

    async def aclose(self) -> None:
        """请求作用域已关闭所有 HTTP 连接，无共享连接需要释放。"""

    def danmaku_for_session(self, session_id: int) -> DanmakuSource:
        """宿主内部会话适配，保留 Bilibili 既有事件类型、采样和存储语义。"""
        return _BilibiliSessionDanmaku(session_id)


class _BilibiliSessionDanmaku:
    def __init__(self, session_id: int) -> None:
        self.session_id = session_id

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        """现有客户端负责 Bili 事件入库，不重复发送为普通文本事件。"""
        from app.sources.bilibili.danmaku import DanmakuClient

        client = DanmakuClient(
            room_id=int(room.source_id),
            session_id=self.session_id,
            cookie=get_bilibili_cookie(),
            on_state=state,
        )
        try:
            await client.run()
        finally:
            client.stop()
