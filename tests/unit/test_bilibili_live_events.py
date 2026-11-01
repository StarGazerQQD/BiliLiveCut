"""监听通道不触碰持久化，且保留鉴权首帧中的真实状态通知。"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from app.plugins.live_source import DanmakuStatus, LiveStatus, RoomSnapshot
from app.sources.bilibili.client import DanmakuServer
from app.sources.bilibili.danmaku import OP_AUTH_REPLY, OP_MESSAGE, DanmakuAccess, encode_packet
from app.sources.bilibili.live_events import BilibiliLiveEvents


def _frame(raw: dict) -> bytes:
    return encode_packet(OP_MESSAGE, json.dumps(raw).encode())


async def test_live_control_channel_ignores_synthetic_live_and_never_saves_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[RoomSnapshot] = []

    async def emit(snapshot: RoomSnapshot) -> None:
        snapshots.append(snapshot)

    async def state(value: DanmakuStatus) -> None:
        pass

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("开下播守候不能调用数据库、采样器或弹幕归档")

    monkeypatch.setattr("app.recording.danmaku_journal.get_session", forbidden)
    monkeypatch.setattr("app.analysis.danmaku_sampling.get_sampler", forbidden)
    monkeypatch.setattr("app.recording.danmaku_archive.DanmakuArchive.append", forbidden)
    monkeypatch.setattr("app.recording.danmaku_journal.DanmakuJournal.append_messages", forbidden)
    monkeypatch.setattr("app.recording.danmaku_journal.DanmakuJournal.append_observation", forbidden)
    client = BilibiliLiveEvents(100, "", emit, state)
    await client._process_frame(
        b"".join(
            _frame(raw)
            for raw in [
                {"cmd": "LIVE"},
                {"cmd": "LIVE", "live_time": 0},
                {"cmd": "LIVE", "live_time": "invalid"},
                {"cmd": "DANMU_MSG", "info": [[], "忽略正文", [1, "观众"]]},
                {"cmd": "SEND_GIFT", "data": {"total_coin": 5000}},
                {"cmd": "LIVE", "live_time": 1700000000, "roomid": 100},
                {"cmd": "PREPARING", "roomid": "100"},
            ]
        )
    )
    assert [item.status for item in snapshots] == [LiveStatus.LIVE, LiveStatus.OFFLINE]


async def test_real_websocket_delivers_live_bundled_with_auth_reply_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    states: list[DanmakuStatus] = []
    snapshots: list[RoomSnapshot] = []
    closed = asyncio.Event()

    async def emit(snapshot: RoomSnapshot) -> None:
        snapshots.append(snapshot)
        client.stop()

    async def state(value: DanmakuStatus) -> None:
        states.append(value)

    async def serve(ws: object) -> None:
        try:
            await ws.recv()
            await ws.send(
                encode_packet(OP_AUTH_REPLY, b'{"code":0}') + _frame({"cmd": "LIVE", "live_time": 1700000000})
            )
            await ws.wait_closed()
        finally:
            closed.set()

    real_connect = websockets.connect
    async with websockets.serve(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(
            websockets, "connect", lambda _uri, **kwargs: real_connect(f"ws://127.0.0.1:{port}", **kwargs)
        )
        client = BilibiliLiveEvents(100, "", emit, state)
        await asyncio.wait_for(
            client._consume_endpoint("test.invalid", 443, DanmakuAccess(DanmakuServer("token", []), 0, False)), 2
        )
        await asyncio.wait_for(closed.wait(), 2)
    assert [snapshot.status for snapshot in snapshots] == [LiveStatus.LIVE]
    assert states == [DanmakuStatus.AVAILABLE, DanmakuStatus.CONNECTING]
