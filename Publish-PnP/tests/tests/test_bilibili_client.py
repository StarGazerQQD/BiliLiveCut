"""Bilibili 客户端的纯逻辑单元测试(不发起真实网络请求)。"""

from __future__ import annotations

import pytest

from app.sources.bilibili.client import (
    BilibiliError,
    BilibiliLiveClient,
    StreamInfo,
    parse_room_id,
    pick_best_stream,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456", 123456),
        ("https://live.bilibili.com/123456", 123456),
        ("https://live.bilibili.com/123456?broadcast_type=0", 123456),
        ("https://live.bilibili.com/h5/789", 789),
        ("  https://live.bilibili.com/42  ", 42),
    ],
)
def test_parse_room_id_valid(raw: str, expected: int) -> None:
    """各种合法输入都能解析出正确房间号。"""
    assert parse_room_id(raw) == expected


def test_parse_room_id_invalid() -> None:
    """非法输入应抛出 BilibiliError。"""
    with pytest.raises(BilibiliError):
        parse_room_id("https://example.com/not-a-room")


def test_parse_play_info_extracts_full_url() -> None:
    """_parse_play_info 应正确拼接 host + base_url + extra。"""
    data = {
        "live_status": 1,
        "playurl_info": {
            "playurl": {
                "stream": [
                    {
                        "protocol_name": "http_hls",
                        "format": [
                            {
                                "format_name": "ts",
                                "codec": [
                                    {
                                        "codec_name": "avc",
                                        "base_url": "/live/base.m3u8",
                                        "current_qn": 10000,
                                        "url_info": [
                                            {
                                                "host": "https://cdn.example.com",
                                                "extra": "?token=abc",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        },
    }
    streams = BilibiliLiveClient._parse_play_info(data)
    assert len(streams) == 1
    s = streams[0]
    assert s.url == "https://cdn.example.com/live/base.m3u8?token=abc"
    assert s.protocol == "hls"
    assert s.quality == 10000


def test_pick_best_stream_prefers_protocol_then_quality() -> None:
    """挑选最佳流:首选协议优先,其次清晰度最高。"""
    streams = [
        StreamInfo("flv-high", "flv", "flv", "avc", 10000),
        StreamInfo("hls-low", "hls", "ts", "avc", 150),
        StreamInfo("hls-high", "hls", "ts", "avc", 10000),
    ]
    best = pick_best_stream(streams, preferred_protocol="hls")
    assert best is not None
    assert best.url == "hls-high"


def test_pick_best_stream_empty_returns_none() -> None:
    """无候选流时返回 None。"""
    assert pick_best_stream([], "hls") is None
