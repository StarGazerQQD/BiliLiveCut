"""宿主管理直播源的注册归属、调用预算和使用者排空。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.plugins.live_source import (
    DanmakuSource,
    LiveSource,
    RoomSnapshot,
    SourceDescriptor,
    SourceError,
    SourceInvalidInput,
    SourceRateLimited,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
    http_url,
)

T = TypeVar("T")
DrainCallback = Callable[[], Awaitable[None]]


@dataclass
class _Entry:
    """注册时固定描述符，避免插件重绑定属性改变归属。"""

    owner: str
    source: LiveSource
    descriptor: SourceDescriptor = field(init=False)
    draining: bool = False
    cooldown_until: float = 0.0
    operations: set[asyncio.Task[object]] = field(default_factory=set)
    users: dict[object, DrainCallback] = field(default_factory=dict)
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))

    def __post_init__(self) -> None:
        self.descriptor = self.source.descriptor


class SourceRegistry:
    """单事件循环的来源注册表；注册和开始排空之间没有 await 竞态窗口。"""

    def __init__(self, *, timeout_s: float = 10.0, attempts: int = 2, retry_delay_s: float = 0.25) -> None:
        from app.sources.bilibili.source import BilibiliSource

        if not 0 < timeout_s <= 60 or not 1 <= attempts <= 3 or not 0 <= retry_delay_s <= 5:
            raise ValueError("来源调用预算超出允许范围")
        self.timeout_s = timeout_s
        self.attempts = attempts
        self.retry_delay_s = retry_delay_s
        self._entries: dict[str, _Entry] = {"bilibili": _Entry("builtin", BilibiliSource())}

    def descriptors(self) -> list[SourceDescriptor]:
        """返回目前接受新任务的来源，供 CLI/Web 显示平台能力。"""
        # FastAPI 的同步只读端点可能位于工作线程；使用原子发布的注册快照。
        entries = self._entries
        return [entry.descriptor for entry in entries.values() if not entry.draining]

    def available(self, platform: str) -> bool:
        """判断来源是否允许新调用或新录制；不代表平台网络状态。"""
        entry = self._entries.get(platform)
        return entry is not None and not entry.draining

    def register_many(self, owner: str, sources: Sequence[LiveSource]) -> None:
        """原子注册一组来源；失败不留下部分注册，资源仍由调用方关闭。"""
        if owner == "builtin" or not sources:
            raise SourceInvalidInput("直播源插件必须注册至少一个来源")
        platforms = set(self._entries)
        domains = {domain for item in self._entries.values() for domain in item.descriptor.domains}
        for source in sources:
            if not isinstance(source, LiveSource) or not isinstance(source.descriptor, SourceDescriptor):
                raise SourceInvalidInput("直播源对象不满足 LiveSource 契约")
            if not all(
                inspect.iscoroutinefunction(getattr(source, name))
                for name in ("resolve_room", "get_room_info", "get_streams", "aclose")
            ):
                raise SourceInvalidInput("直播源必须实现异步方法；阻塞 SDK 请在插件内隔离")
            if isinstance(source, DanmakuSource) and not inspect.iscoroutinefunction(source.collect_danmaku):
                raise SourceInvalidInput("可选弹幕采集也必须是异步方法；阻塞 SDK 请在插件内隔离")
            descriptor = source.descriptor
            if descriptor.platform in platforms or domains.intersection(descriptor.domains):
                raise SourceInvalidInput(f"平台或域名已注册: {descriptor.platform}")
            platforms.add(descriptor.platform)
            domains.update(descriptor.domains)
        additions = {source.descriptor.platform: _Entry(owner, source) for source in sources}
        self._entries = {**self._entries, **additions}

    def _entry(self, platform: str) -> _Entry:
        entry = self._entries.get(platform)
        if entry is None or entry.draining:
            raise SourceUnavailable(f"来源 {platform} 不可用，请安装并启用对应插件")
        return entry

    @asynccontextmanager
    async def use(self, platform: str, stop: DrainCallback) -> AsyncIterator[LiveSource]:
        """持有长任务租约；停用时宿主先调用 stop 并等待片段收尾。"""
        entry = self._entry(platform)
        token = object()
        entry.users[token] = stop
        try:
            yield entry.source
        finally:
            entry.users.pop(token, None)

    async def unregister_owner(self, owner: str) -> None:
        """阻止新任务、排空旧任务及调用，再释放来源；收尾失败时保留禁用中的注册。"""
        if owner == "builtin":
            raise SourceInvalidInput("不能注销内置来源")
        entries = [entry for entry in self._entries.values() if entry.owner == owner]
        for entry in entries:
            entry.draining = True
        for entry in entries:
            for stop in tuple(entry.users.values()):
                # 不取消片段收尾；使用者自身必须具有有界停止/强停流程。
                try:
                    await stop()
                except Exception as exc:
                    raise SourceUnavailable("直播源使用者未能完成收尾，请重试停用") from exc
            if entry.users:
                raise SourceUnavailable("来源仍有活动任务，暂不卸载插件")
            operations = tuple(entry.operations)
            for operation in operations:
                operation.cancel()
            if operations:
                await asyncio.gather(*operations, return_exceptions=True)
            try:
                await asyncio.wait_for(entry.source.aclose(), timeout=self.timeout_s)
            except Exception as exc:
                raise SourceUnavailable("直播源关闭失败，请重试停用") from exc
        self._entries = {platform: entry for platform, entry in self._entries.items() if entry.owner != owner}

    async def _call(self, entry: _Entry, operation: Callable[[], Awaitable[T]]) -> T:
        if entry.draining:
            raise SourceUnavailable("来源正在停用")
        remaining = entry.cooldown_until - time.monotonic()
        if remaining > 0:
            raise SourceRateLimited("来源处于限流等待期", retry_after=remaining)

        async def execute() -> T:
            async with entry.slots:
                for attempt in range(self.attempts):
                    if entry.draining:
                        raise SourceUnavailable("来源正在停用")
                    remaining = entry.cooldown_until - time.monotonic()
                    if remaining > 0:
                        raise SourceRateLimited("来源处于限流等待期", retry_after=remaining)
                    try:
                        return await asyncio.wait_for(operation(), timeout=self.timeout_s)
                    except SourceRateLimited as exc:
                        delay = exc.retry_after if exc.retry_after is not None else 60.0
                        entry.cooldown_until = max(entry.cooldown_until, time.monotonic() + delay)
                        raise
                    except (SourceTemporaryError, TimeoutError) as exc:
                        if attempt + 1 == self.attempts:
                            raise SourceTemporaryError("来源请求超时或暂时失败，请稍后重试") from exc
                        delay = max(self.retry_delay_s, getattr(exc, "retry_after", None) or 0.0)
                        if delay > 5:
                            entry.cooldown_until = max(entry.cooldown_until, time.monotonic() + delay)
                            raise SourceTemporaryError("来源请求需要延后重试", retry_after=delay) from exc
                        await asyncio.sleep(delay)
                    except SourceError:
                        raise
                    except Exception as exc:
                        # 插件边界：保留异常链供调试，公开错误不回显第三方 URL/凭据。
                        raise SourceUnavailable("来源实现发生错误，请检查插件配置及兼容性") from exc
            raise AssertionError("unreachable")

        task = asyncio.create_task(execute())
        entry.operations.add(task)
        try:
            async with asyncio.timeout(self.timeout_s * self.attempts + 5):
                return await task
        except TimeoutError as exc:
            raise SourceTemporaryError("来源请求排队或调用超时") from exc
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if entry.draining and current is not None and not current.cancelling():
                raise SourceUnavailable("来源已停用") from None
            raise
        finally:
            entry.operations.discard(task)

    def identify(self, value: str, platform: str | None = None) -> str:
        """纯数字保留 Bilibili 语义；URL 只按注册域名识别，不做跨源探测。"""
        if platform is not None:
            entry = self._entry(platform)
            if "://" in value:
                try:
                    http_url(value)
                except ValueError as exc:
                    raise SourceInvalidInput("来源 URL 无效") from exc
                if urlsplit(value).hostname not in entry.descriptor.domains:
                    raise SourceInvalidInput("URL 不属于指定平台")
            return platform
        value = value.strip()
        if value.isdecimal():
            return "bilibili"
        url = value if "://" in value else f"https://{value}"
        try:
            http_url(url)
        except ValueError as exc:
            raise SourceInvalidInput("请输入直播间 URL，或使用显式 platform 指定来源") from exc
        host = urlsplit(url).hostname
        for entry in self._entries.values():
            if host in entry.descriptor.domains:
                self._entry(entry.descriptor.platform)
                return entry.descriptor.platform
        raise SourceInvalidInput("不支持该地址，请先安装并启用对应直播源插件")

    async def resolve_room(self, value: str, platform: str | None = None) -> SourceRoom:
        """解析并检查规范身份仍属于同一注册来源。"""
        selected = self.identify(value, platform)
        entry = self._entry(selected)
        room = await self._call(entry, lambda: entry.source.resolve_room(value.strip()))
        if (
            not isinstance(room, SourceRoom)
            or room.platform != selected
            or urlsplit(room.canonical_url).hostname not in entry.descriptor.domains
        ):
            raise SourceInvalidInput("来源返回了不属于自身的规范身份")
        return room

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        """使用统一预算读取状态，不把查询错误转换成下播。"""
        entry = self._entry(room.platform)
        result = await self._call(entry, lambda: entry.source.get_room_info(room))
        if not isinstance(result, RoomSnapshot):
            raise SourceUnavailable("来源返回了无效状态")
        return result

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        """每次重新取流并重新校验 headers，排除已过期候选；保持来源给定顺序。"""
        entry = self._entry(room.platform)
        result = await self._call(entry, lambda: entry.source.get_streams(room, preference))
        try:
            if not isinstance(result, list) or not all(isinstance(item, StreamSpec) for item in result):
                raise ValueError("invalid streams")
            streams = [StreamSpec.model_validate(item.model_dump()) for item in result]
        except (ValueError, ValidationError) as exc:
            raise SourceUnavailable("来源返回了无效播放规格") from exc
        now = datetime.now(UTC)
        return [item for item in streams if item.expires_at is None or item.expires_at > now]


source_registry = SourceRegistry()
