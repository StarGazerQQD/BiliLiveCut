"""Bilibili 客户端的纯逻辑单元测试(不发起真实网络请求)。"""

from __future__ import annotations

import httpx
import pytest

from app.sources.bilibili.client import (
    BilibiliError,
    BilibiliLiveClient,
    BilibiliRateLimitError,
    HttpErrorType,
    StreamInfo,
    parse_room_id,
    pick_best_stream,
)
from app.sources.bilibili.wbi import WbiKeys, sign_wbi_params

_WBI_RESPONSE = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "isLogin": False,
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    },
}


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


@pytest.mark.asyncio
async def test_room_info_includes_anchor_name_and_title() -> None:
    """房间解析应连同详情接口返回主播名和直播标题。"""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/room/v1/Room/room_init":
            payload = {
                "code": 0,
                "message": "0",
                "data": {"room_id": 23771139, "short_id": 123, "uid": 456, "live_status": 1},
            }
        else:
            payload = {
                "code": 0,
                "message": "0",
                "data": {
                    "room_info": {"uid": 456, "title": "深夜游戏直播"},
                    "anchor_info": {"base_info": {"uname": "测试主播"}},
                },
            }
        return httpx.Response(200, json=payload, request=request)

    client = BilibiliLiveClient()
    headers = dict(client._client.headers)  # noqa: SLF001
    await client._client.aclose()  # noqa: SLF001
    client._client = httpx.AsyncClient(headers=headers, transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        info = await client.get_room_info("123")
    finally:
        await client.aclose()

    assert info.room_id == 23771139
    assert info.title == "深夜游戏直播"
    assert info.uploader_name == "测试主播"
    assert [request.url.path for request in requests] == [
        "/room/v1/Room/room_init",
        "/xlive/web-room/v1/index/getInfoByRoom",
    ]


@pytest.mark.asyncio
async def test_danmaku_server_parses_success_response() -> None:
    """匿名弹幕请求应接受导航 -101、携带有效 WBI 签名并解析节点。"""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=_WBI_RESPONSE, request=request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "0",
                "data": {
                    "token": "token-value",
                    "host_list": [{"host": "broadcast.example", "wss_port": 443}],
                },
            },
            request=request,
        )

    client = BilibiliLiveClient()
    await client._client.aclose()  # noqa: SLF001 - 测试注入无网络传输层
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        server = await client.get_danmaku_server(23771139)
    finally:
        await client.aclose()

    assert server.token == "token-value"
    assert server.hosts == [{"host": "broadcast.example", "wss_port": 443}]
    assert [request.url.path for request in requests] == [
        "/x/web-interface/nav",
        "/xlive/web-room/v1/index/getDanmuInfo",
    ]
    assert all("Cookie" not in request.headers for request in requests)
    signed_request = requests[1]
    query = dict(signed_request.url.params)
    timestamp = int(query["wts"])
    keys = WbiKeys.from_urls(
        _WBI_RESPONSE["data"]["wbi_img"]["img_url"],
        _WBI_RESPONSE["data"]["wbi_img"]["sub_url"],
    )
    expected = sign_wbi_params(
        {"id": 23771139, "type": 0, "web_location": "444.8"},
        keys,
        timestamp=timestamp,
    )
    assert query == expected


@pytest.mark.asyncio
async def test_danmaku_server_sends_configured_cookie() -> None:
    """登录模式应在 WBI 导航和 token 请求中携带配置的 Cookie。"""
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=_WBI_RESPONSE, request=request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "0",
                "data": {"token": "logged-token", "host_list": []},
            },
            request=request,
        )

    cookie = "SESSDATA=valid; DedeUserID=456"
    client = BilibiliLiveClient(cookie=cookie)
    headers = dict(client._client.headers)  # noqa: SLF001
    await client._client.aclose()  # noqa: SLF001 - 测试注入无网络传输层
    client._client = httpx.AsyncClient(  # noqa: SLF001
        headers=headers,
        transport=httpx.MockTransport(handler),
    )
    try:
        server = await client.get_danmaku_server(23771139)
    finally:
        await client.aclose()

    assert server.token == "logged-token"
    assert len(requests) == 2
    assert all(request.headers["Cookie"] == cookie for request in requests)


@pytest.mark.asyncio
async def test_danmaku_server_classifies_minus_352_as_risk_control() -> None:
    """弹幕 token 接口的 -352 是平台风控，不应误报为 Cookie 过期。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=_WBI_RESPONSE, request=request)
        return httpx.Response(
            200,
            json={"code": -352, "message": "-352", "data": {}},
            request=request,
        )

    client = BilibiliLiveClient()
    await client._client.aclose()  # noqa: SLF001 - 测试注入无网络传输层
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        with pytest.raises(BilibiliRateLimitError) as exc_info:
            await client.get_danmaku_server(23771139)
    finally:
        await client.aclose()

    assert exc_info.value.error_type is HttpErrorType.RISK_CONTROL
    assert "Cookie 过期" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_danmaku_server_rejects_missing_wbi_keys() -> None:
    """导航响应缺少 WBI 图片键时必须明确失败，不能退回裸请求。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": -101, "message": "账号未登录", "data": {"isLogin": False}},
            request=request,
        )

    client = BilibiliLiveClient()
    await client._client.aclose()  # noqa: SLF001 - 测试注入无网络传输层
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    try:
        with pytest.raises(BilibiliError, match="缺少 wbi_img"):
            await client.get_danmaku_server(23771139)
    finally:
        await client.aclose()
