from __future__ import annotations

import json
import zlib
from typing import TYPE_CHECKING

import brotli
import pytest

from app.sources.bilibili import danmaku

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("version", [2, 3])
def test_compressed_messages_keep_their_payload(version: int) -> None:
    message = {"cmd": "DANMU_MSG", "info": [[], "正常弹幕"]}
    packet = danmaku.encode_packet(danmaku.OP_MESSAGE, json.dumps(message).encode())
    compressed = zlib.compress(packet) if version == 2 else brotli.compress(packet)
    assert danmaku.decode(danmaku.encode_packet(danmaku.OP_MESSAGE, compressed, version)) == [
        (danmaku.OP_MESSAGE, message)
    ]


@pytest.mark.parametrize("version", [2, 3])
def test_compressed_expansion_is_bounded(version: int, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(danmaku, "_MAX_DECODE_BYTES", 1024)
    expanded = b"a" * 100_000
    compressed = zlib.compress(expanded) if version == 2 else brotli.compress(expanded)
    packet = danmaku.encode_packet(danmaku.OP_MESSAGE, compressed, version)
    with pytest.raises(danmaku.DanmakuProtocolError, match="累计大小"):
        danmaku.decode(packet)


def test_sibling_compressed_packets_share_one_budget(monkeypatch: MonkeyPatch) -> None:
    message = danmaku.encode_packet(danmaku.OP_MESSAGE, b'{"text":"' + b"a" * 500 + b'"}')
    packet = danmaku.encode_packet(danmaku.OP_MESSAGE, zlib.compress(message), 2)
    monkeypatch.setattr(danmaku, "_MAX_DECODE_BYTES", len(packet) * 2 + len(message))
    with pytest.raises(danmaku.DanmakuProtocolError, match="累计大小"):
        danmaku.decode(packet * 2)


def test_too_many_small_messages_are_rejected(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(danmaku, "_MAX_DECODE_PACKETS", 3)
    packet = danmaku.encode_packet(danmaku.OP_HEARTBEAT_REPLY, b"\x00\x00\x00\x01")
    assert len(danmaku.decode(packet * 3)) == 3
    with pytest.raises(danmaku.DanmakuProtocolError, match="过多"):
        danmaku.decode(packet * 4)


def test_oversized_frame_is_rejected_before_decoding(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(danmaku, "_MAX_FRAME_BYTES", 32)
    with pytest.raises(danmaku.DanmakuProtocolError, match="帧超过"):
        danmaku.decode(b"a" * 33)


@pytest.mark.parametrize("version", [2, 3])
def test_nested_compression_stops_at_depth_limit(version: int) -> None:
    message = {"cmd": "DANMU_MSG", "info": [[], "正常嵌套弹幕"]}
    packet = danmaku.encode_packet(danmaku.OP_MESSAGE, json.dumps(message).encode())
    compress = zlib.compress if version == 2 else brotli.compress
    for _ in range(danmaku._MAX_DECODE_DEPTH):
        packet = danmaku.encode_packet(danmaku.OP_MESSAGE, compress(packet), version)
    assert danmaku.decode(packet) == [(danmaku.OP_MESSAGE, message)]
    too_deep = danmaku.encode_packet(danmaku.OP_MESSAGE, compress(packet * 2), version)
    neighbor = danmaku.encode_packet(danmaku.OP_MESSAGE, json.dumps(message).encode())
    assert danmaku.decode(too_deep + neighbor) == [(danmaku.OP_MESSAGE, message)]


@pytest.mark.parametrize("version", [2, 3])
def test_deep_compressed_siblings_are_skipped_before_expansion(version: int, monkeypatch: MonkeyPatch) -> None:
    compress = zlib.compress if version == 2 else brotli.compress
    bomb = danmaku.encode_packet(danmaku.OP_MESSAGE, compress(b"a" * 1_000_000), version)
    neighbor = danmaku.encode_packet(danmaku.OP_MESSAGE, b'{"text":"valid"}')
    monkeypatch.setattr(danmaku, "_MAX_DECODE_BYTES", len(bomb) * 2 + len(neighbor))
    assert danmaku.decode(bomb * 2 + neighbor, _depth=danmaku._MAX_DECODE_DEPTH) == [
        (danmaku.OP_MESSAGE, {"text": "valid"})
    ]
