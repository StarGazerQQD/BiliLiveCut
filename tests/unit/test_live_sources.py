from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from loguru import logger
from pydantic import ValidationError
from pytest import MonkeyPatch

from app.plugins import PluginManifest
from app.plugins.live_source import (
    LiveStatus,
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
from app.plugins.manager import PluginManager, PluginStateError
from app.sources.bilibili.client import BilibiliLiveClient, BilibiliRateLimitError, HttpErrorType, RoomInfo, StreamInfo
from app.sources.bilibili.source import BilibiliSource
from app.sources.registry import SourceRegistry


class Source:
    descriptor = SourceDescriptor(platform="sample", name="示例", domains=("sample.invalid", "short.invalid"))

    def __init__(self) -> None:
        self.closed = False
        self.calls = 0
        self.error: Exception | None = None

    async def resolve_room(self, value: str) -> SourceRoom:
        return SourceRoom(platform="sample", source_id="room:字串", canonical_url="https://sample.invalid/room")

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        self.calls += 1
        if self.error:
            raise self.error
        return RoomSnapshot(status=LiveStatus.UNKNOWN)

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return [StreamSpec(url="https://cdn.invalid/live?token=secret", transport="hls", container="ts")]

    async def aclose(self) -> None:
        self.closed = True


def test_registry_rejects_blocking_optional_collector() -> None:
    class BlockingSource(Source):
        def collect_danmaku(self, *args: object) -> None:
            raise AssertionError("阻塞方法不能被执行")

    registry = SourceRegistry()
    with pytest.raises(SourceInvalidInput, match="弹幕采集也必须是异步"):
        registry.register_many("blocking", [BlockingSource()])
    assert not registry.available("sample")


@pytest.mark.parametrize(
    "headers",
    [{"Cookie": "value\r\nX: injected"}, {"X\nHeader": "x"}, {"Host": "other.invalid"}, {"X": "a", "x": "b"}],
)
def test_stream_spec_rejects_header_injection(headers: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        StreamSpec(url="https://cdn.invalid/stream", transport="hls", container="ts", headers=headers)


def test_contract_preserves_unknown_identity_and_hides_credentials() -> None:
    room = SourceRoom(platform="sample", source_id="001-ab", canonical_url="https://sample.invalid/001-ab")
    assert room.source_id == "001-ab"
    assert RoomSnapshot(status="unknown").title is None
    stream = StreamSpec(
        url="https://cdn.invalid/?token=secret", transport="flv", container="flv", headers={"Cookie": "x"}
    )
    assert "secret" not in repr(stream) and "Cookie" not in repr(stream)
    with pytest.raises(ValidationError):
        SourceRoom(platform="sample", source_id=123, canonical_url="https://sample.invalid/room")
    with pytest.raises(ValidationError):
        RoomSnapshot(status="live", observed_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        SourceRoom(platform="sample", source_id="id", canonical_url="https://sample.invalid/?token=secret")


async def test_registry_identity_atomic_conflicts_and_missing_source() -> None:
    registry = SourceRegistry()
    source = Source()
    with pytest.raises(SourceInvalidInput):
        registry.register_many("bad", [source, source])
    assert not registry.available("sample")
    with pytest.raises(SourceInvalidInput):
        registry.register_many("bad", [source, BilibiliSource()])
    assert not registry.available("sample")
    registry.register_many("demo", [source])
    assert registry.identify("123") == "bilibili"
    assert registry.identify("https://short.invalid/abc") == "sample"
    assert (await registry.resolve_room("https://short.invalid/abc")).source_id == "room:字串"
    with pytest.raises(SourceInvalidInput):
        registry.identify("https://evil.invalid/live.bilibili.com/123")
    with pytest.raises(SourceUnavailable):
        registry.identify("id", "missing")
    await registry.unregister_owner("demo")
    assert source.closed and not registry.available("sample")


async def test_registry_errors_never_become_offline_and_retries_are_bounded() -> None:
    registry = SourceRegistry(retry_delay_s=0)
    source = Source()
    registry.register_many("demo", [source])
    room = await registry.resolve_room("id", "sample")
    assert (await registry.get_room_info(room)).status is LiveStatus.UNKNOWN
    source.calls = 0
    source.error = SourceTemporaryError()
    with pytest.raises(SourceTemporaryError):
        await registry.get_room_info(room)
    assert source.calls == 2
    source.calls = 0
    source.error = SourceAuthenticationError()
    with pytest.raises(SourceAuthenticationError):
        await registry.get_room_info(room)
    assert source.calls == 1
    source.error = SourceRateLimited(retry_after=60)
    with pytest.raises(SourceRateLimited):
        await registry.get_room_info(room)
    source.calls = 0
    with pytest.raises(SourceRateLimited):
        await registry.get_room_info(room)
    assert source.calls == 0


async def test_registry_timeout_cancellation_and_drain_wait_for_use() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow(Source):
        async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    source = Slow()
    registry = SourceRegistry(timeout_s=0.01, attempts=1)
    registry.register_many("demo", [source])
    room = await registry.resolve_room("id", "sample")
    with pytest.raises(SourceTemporaryError):
        await registry.get_room_info(room)
    assert cancelled.is_set()
    cancelled.clear()
    entered.clear()
    task = asyncio.create_task(registry.get_room_info(room))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()

    released = asyncio.Event()
    using = asyncio.Event()

    async def stop() -> None:
        assert not source.closed
        assert not registry.available("sample")
        released.set()
        await consumer

    async def consume() -> None:
        async with registry.use("sample", stop):
            using.set()
            await released.wait()

    consumer = asyncio.create_task(consume())
    await using.wait()
    await registry.unregister_owner("demo")
    assert source.closed


async def test_registry_revalidates_mutated_headers_and_excludes_expired_streams(monkeypatch: MonkeyPatch) -> None:
    source = Source()
    registry = SourceRegistry()
    registry.register_many("demo", [source])
    room = await registry.resolve_room("id", "sample")
    old = StreamSpec(
        url="https://cdn.invalid/expired",
        transport="flv",
        container="flv",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    fresh = StreamSpec(url="https://cdn.invalid/fresh", transport="hls", container="ts")

    async def streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return [old, fresh]

    monkeypatch.setattr(source, "get_streams", streams)
    assert await registry.get_streams(room, StreamPreference()) == [fresh]
    fresh.headers["X"] = "injected\nheader"
    with pytest.raises(SourceUnavailable):
        await registry.get_streams(room, StreamPreference())


async def test_bilibili_adapter_preserves_qn_protocol_and_error_meaning(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.core import config

    monkeypatch.setattr(config.settings, "stream_quality", 250)

    async def streams(self: BilibiliLiveClient, room_id: int, quality: int) -> list[StreamInfo]:
        assert room_id == 123 and quality == 250
        return [
            StreamInfo("https://cdn.invalid/flv", "flv", "flv", "avc", 10000),
            StreamInfo("https://cdn.invalid/hls", "hls", "ts", "avc", 250),
        ]

    async def info(self: BilibiliLiveClient, value: str, *, include_detail: bool = True) -> RoomInfo:
        return RoomInfo(123, 1, 99, 42)

    monkeypatch.setattr(BilibiliLiveClient, "get_streams", streams)
    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", info)
    source = BilibiliSource()
    room = await source.resolve_room("1")
    assert room.source_id == "123"
    assert (await source.get_room_info(room)).status == LiveStatus.UNKNOWN
    result = await source.get_streams(room, StreamPreference())
    assert [item.quality_id for item in result] == ["250", "10000"]
    assert all("Cookie" not in item.headers for item in result)

    async def denied(self: BilibiliLiveClient, value: str, *, include_detail: bool = True) -> RoomInfo:
        raise BilibiliRateLimitError(HttpErrorType.COOKIE_EXPIRED, "secret")

    monkeypatch.setattr(BilibiliLiveClient, "get_room_info", denied)
    with pytest.raises(SourceAuthenticationError, match="登录态"):
        await source.get_room_info(room)


def write_source_plugin(root: Path, *, broken: bool = False) -> Path:
    directory = root / "source-demo"
    directory.mkdir()
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "id": "source-demo",
                "name": "来源测试",
                "version": "1",
                "api_version": "1",
                "live_source_api_version": "1",
                "capabilities": ["live_source"],
                "entrypoint": "main.py:Plugin",
            }
        ),
        encoding="utf-8",
    )
    (directory / "main.py").write_text(
        """from app.plugins import BasePlugin
from app.plugins.live_source import SourceDescriptor, SourceRoom, RoomSnapshot, StreamSpec
class Source:
    descriptor = SourceDescriptor(platform="external", name="外部", domains=("external.invalid",))
    def __init__(self, directory): self.directory = directory
    async def resolve_room(self, value):
        return SourceRoom(platform="external", source_id="id-ab", canonical_url="https://external.invalid/id-ab")
    async def get_room_info(self, room): return RoomSnapshot(status="live")
    async def get_streams(self, room, preference): return []
    async def aclose(self): (self.directory / "closed.txt").write_text("closed")
class Plugin(BasePlugin):
    def on_enable(self, context):
        self.context = context
        context.register_live_source(Source(context.plugin_dir))
"""
        + ('        raise RuntimeError("secret-token")\n' if broken else "")
        + """    def on_disable(self):
        assert (self.context.plugin_dir / "closed.txt").is_file()
        (self.context.plugin_dir / "disabled.txt").write_text("disabled")
""",
        encoding="utf-8",
    )
    return directory


async def test_real_plugin_loader_lifecycle_restart_and_missing(temp_db: None, tmp_path: Path) -> None:
    directory = write_source_plugin(tmp_path)
    manager = PluginManager(tmp_path)
    await manager.start()
    assert not manager.sources.available("external")
    await manager.set_enabled("source-demo", True)
    assert (await manager.sources.resolve_room("https://external.invalid/a")).source_id == "id-ab"
    await manager.stop()
    assert (directory / "disabled.txt").is_file()
    await manager.start()
    assert manager.sources.available("external")
    (directory / "plugin.json").unlink()
    await manager.refresh()
    assert not manager.sources.available("external")
    await manager.stop()


async def test_plugin_partial_activation_closes_unpublished_source(temp_db: None, tmp_path: Path) -> None:
    directory = write_source_plugin(tmp_path, broken=True)
    manager = PluginManager(tmp_path)
    await manager.start()
    with pytest.raises(PluginStateError) as failure:
        await manager.set_enabled("source-demo", True)
    assert "secret-token" not in str(failure.value)
    assert (directory / "closed.txt").is_file() and (directory / "disabled.txt").is_file()
    assert not manager.sources.available("external")
    await manager.stop()


def test_source_manifest_requires_explicit_contract_version() -> None:
    fields = {
        "id": "example",
        "name": "Example",
        "version": "1",
        "api_version": "1",
        "entrypoint": "main.py:Plugin",
        "capabilities": ["live_source"],
    }
    for version in (None, "2"):
        with pytest.raises(ValidationError, match="live_source_api_version"):
            PluginManifest(**fields, live_source_api_version=version)
    assert PluginManifest(**fields, live_source_api_version="1").capabilities == ("live_source",)


async def test_queued_calls_respect_longest_rate_limit() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Limited(Source):
        async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
            nonlocal calls
            calls += 1
            index = calls
            if calls == 4:
                entered.set()
            await release.wait()
            raise SourceRateLimited(retry_after=120 if index == 1 else 1)

    registry = SourceRegistry()
    registry.register_many("demo", [Limited()])
    room = await registry.resolve_room("id", "sample")
    tasks = [asyncio.create_task(registry.get_room_info(room)) for _ in range(8)]
    await entered.wait()
    release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert calls == 4
    assert all(isinstance(item, SourceRateLimited) for item in outcomes)
    with pytest.raises(SourceRateLimited) as failure:
        await registry.get_room_info(room)
    assert failure.value.retry_after is not None and failure.value.retry_after > 110


async def test_zero_retry_after_does_not_create_default_cooldown() -> None:
    registry = SourceRegistry()
    source = Source()
    source.error = SourceRateLimited(retry_after=0)
    registry.register_many("demo", [source])
    room = await registry.resolve_room("id", "sample")
    with pytest.raises(SourceRateLimited):
        await registry.get_room_info(room)
    source.error = None
    assert (await registry.get_room_info(room)).status == LiveStatus.UNKNOWN


async def test_drain_cancels_inflight_before_close_and_fixes_descriptor_ownership() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow(Source):
        async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def aclose(self) -> None:
            assert cancelled.is_set()
            await super().aclose()

    registry = SourceRegistry()
    source = Slow()
    registry.register_many("demo", [source])
    room = await registry.resolve_room("id", "sample")
    source.descriptor = BilibiliSource.descriptor
    task = asyncio.create_task(registry.get_room_info(room))
    await entered.wait()
    await registry.unregister_owner("demo")
    with pytest.raises(SourceUnavailable):
        await task
    assert source.closed and registry.available("bilibili") and not registry.available("sample")


async def test_source_close_errors_are_redacted_and_registration_retained(temp_db: None, tmp_path: Path) -> None:
    directory = write_source_plugin(tmp_path)
    entry = directory / "main.py"
    entry.write_text(
        entry.read_text(encoding="utf-8").replace(
            '(self.directory / "closed.txt").write_text("closed")', 'raise RuntimeError("Cookie=secret-token")'
        ),
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)
    await manager.start()
    await manager.set_enabled("source-demo", True)
    with pytest.raises(PluginStateError) as failure:
        await manager.set_enabled("source-demo", False)
    assert "secret-token" not in str(failure.value)
    assert manager.descriptor("source-demo")["loaded"]
    assert not manager.sources.available("external")
    # 失败不能卸载仍在排空的模块；取消本次测试实例的关闭故障后重试。
    module = sys.modules["_bililivecut_plugin_source_demo"]

    async def close(source: object) -> None:
        (directory / "closed.txt").write_text("closed")

    module.Source.aclose = close
    await manager.stop()
    assert "_bililivecut_plugin_source_demo" not in sys.modules


@pytest.mark.parametrize("broken", [False, True])
async def test_disable_and_rollback_hooks_never_log_secrets(temp_db: None, tmp_path: Path, broken: bool) -> None:
    directory = write_source_plugin(tmp_path, broken=broken)
    entry = directory / "main.py"
    entry.write_text(
        entry.read_text(encoding="utf-8") + '        raise RuntimeError("Cookie=secret-token")\n', encoding="utf-8"
    )
    captured = io.StringIO()
    sink = logger.add(captured, format="{message}")
    manager = PluginManager(tmp_path)
    try:
        await manager.start()
        if broken:
            with pytest.raises(PluginStateError):
                await manager.set_enabled("source-demo", True)
        else:
            await manager.set_enabled("source-demo", True)
            await manager.set_enabled("source-demo", False)
        assert "secret-token" not in captured.getvalue()
        assert "secret-token" not in str(manager.descriptor("source-demo"))
    finally:
        logger.remove(sink)
        await manager.stop()


async def test_refresh_moving_same_manifest_closes_original(
    temp_db: None, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    original = write_source_plugin(first)
    replacement = write_source_plugin(second)
    manager = PluginManager(first)
    await manager.start()
    await manager.set_enabled("source-demo", True)
    monkeypatch.setattr(manager, "_configured_root", second)
    await manager.refresh()
    assert (original / "closed.txt").is_file()
    assert manager.sources.available("external")
    await manager.stop()
    assert (replacement / "closed.txt").is_file()


async def test_loader_duplicate_registration_closes_all_pending_sources(temp_db: None, tmp_path: Path) -> None:
    directory = write_source_plugin(tmp_path)
    entry = directory / "main.py"
    entry.write_text(
        entry.read_text(encoding="utf-8")
        .replace(
            "context.register_live_source(Source(context.plugin_dir))",
            "context.register_live_source(Source(context.plugin_dir))\n        context.register_live_source(Source(context.plugin_dir))",
        )
        .replace(
            '(self.directory / "closed.txt").write_text("closed")',
            '(self.directory / "closed.txt").open("a").write("closed\\n")',
        ),
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)
    await manager.start()
    with pytest.raises(PluginStateError, match="已注册"):
        await manager.set_enabled("source-demo", True)
    assert (directory / "closed.txt").read_text().splitlines() == ["closed", "closed"]
    assert not manager.sources.available("external")
    await manager.stop()


async def test_cancel_enable_rolls_back_staged_source(temp_db: None, tmp_path: Path) -> None:
    directory = write_source_plugin(tmp_path)
    entry = directory / "main.py"
    entry.write_text(
        "import asyncio\n"
        + entry.read_text(encoding="utf-8")
        .replace("def on_enable(self, context):", "async def on_enable(self, context):")
        .replace(
            "context.register_live_source(Source(context.plugin_dir))",
            "context.register_live_source(Source(context.plugin_dir))\n        await asyncio.Event().wait()",
        ),
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path)
    await manager.start()
    task = asyncio.create_task(manager.set_enabled("source-demo", True))
    # 明确等到实际 on_enable 已调用，不依赖时钟等待。
    module_name = "_bililivecut_plugin_source_demo"
    while module_name not in sys.modules:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (directory / "closed.txt").is_file()
    assert (directory / "disabled.txt").is_file()
    assert not manager.sources.available("external") and module_name not in sys.modules
    await manager.stop()
