"""Bilibili 直播源客户端:房间解析与取流地址获取。

实现思路(全部使用网页播放器公开接口,不做逆向/破解):

1. 从用户输入(URL 或纯数字短号)提取房间号;
2. 调用 ``room_init`` 将短号归一化为真实 ``room_id`` 并获取开播状态;
3. 调用 ``getRoomPlayInfo`` 获取 HLS / FLV 播放地址;
4. 解析返回结构,按偏好(协议 / 清晰度)挑选最佳流。

合规要点:

* 仅读取页面正常暴露的播放信息;
* 取到的播放地址带时效与鉴权参数,**只在内存/会话内使用**,不长期持久化或对外分发;
* 携带 ``Referer`` 与正常 ``User-Agent``,并由上层控制访问频率。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from loguru import logger

# B 站要求合理的 Referer;否则部分接口会拒绝或返回受限数据。
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
}

_ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
_PLAY_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"

# 从形如 https://live.bilibili.com/123?xxx 的 URL 中提取房间号。
_ROOM_ID_RE = re.compile(r"live\.bilibili\.com/(?:h5/)?(\d+)")


class BilibiliError(Exception):
    """Bilibili 接口调用相关错误。"""


@dataclass(slots=True)
class RoomInfo:
    """直播间基础信息(来自 ``room_init``)。

    :param room_id: 归一化后的真实房间号。
    :param short_id: 短号(可能为 0)。
    :param uid: 主播 UID。
    :param live_status: 开播状态,1 表示直播中。
    """

    room_id: int
    short_id: int
    uid: int
    live_status: int

    @property
    def is_live(self) -> bool:
        """是否正在直播。"""
        return self.live_status == 1


@dataclass(slots=True)
class DanmakuServer:
    """弹幕长连接所需的服务器与鉴权信息(来自 ``getDanmuInfo``)。

    :param token: 鉴权 token,用于 WebSocket 认证包。
    :param hosts: 候选服务器列表 ``[{host, wss_port, ws_port}, ...]``。
    """

    token: str
    hosts: list[dict]


@dataclass(slots=True)
class StreamInfo:
    """可用的直播流地址。

    :param url: 完整可拉流的 URL(host + base_url + extra)。
    :param protocol: 协议名,``hls`` 或 ``flv``。
    :param format_name: 容器格式,如 ``ts`` / ``fmp4`` / ``flv``。
    :param codec_name: 编码,如 ``avc`` / ``hevc``。
    :param quality: 实际清晰度码(qn)。
    """

    url: str
    protocol: str
    format_name: str
    codec_name: str
    quality: int


def parse_uid_from_cookie(cookie: str) -> int:
    """从 cookie 字符串解析登录用户 UID(``DedeUserID``)。

    :param cookie: 完整 cookie 字符串。
    :returns: UID;未登录/解析失败时返回 0(匿名)。
    """
    if not cookie:
        return 0
    match = re.search(r"DedeUserID=(\d+)", cookie)
    return int(match.group(1)) if match else 0


def parse_room_id(input_url: str) -> int:
    """从用户输入中解析房间号(可能是短号)。

    支持三种输入:完整 URL、``live.bilibili.com/123`` 片段、或纯数字。

    :param input_url: 用户输入的直播间 URL 或房间号字符串。
    :returns: 解析出的房间号(整数,可能仍是短号,需 ``room_init`` 归一化)。
    :raises BilibiliError: 无法从输入中解析出房间号时。
    """
    text = input_url.strip()
    if text.isdigit():
        return int(text)
    match = _ROOM_ID_RE.search(text)
    if match:
        return int(match.group(1))
    raise BilibiliError(f"无法从输入解析房间号: {input_url!r}")


class BilibiliLiveClient:
    """Bilibili 直播取流客户端。

    封装一个 ``httpx.AsyncClient``;可作为异步上下文管理器使用以确保连接关闭。

    :param cookie: 可选登录态 cookie;留空则匿名访问公开信息。
    :param timeout: 单次请求超时(秒)。
    """

    def __init__(self, cookie: str = "", timeout: float = 10.0) -> None:
        headers = dict(_DEFAULT_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout)

    async def __aenter__(self) -> BilibiliLiveClient:
        """进入异步上下文。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """退出异步上下文时关闭底层连接。"""
        await self.aclose()

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接。"""
        await self._client.aclose()

    async def _get_json(self, url: str, params: dict[str, object]) -> dict:
        """发起 GET 请求并校验 B 站统一返回结构 ``{code, message, data}``。

        :param url: 接口地址。
        :param params: 查询参数。
        :returns: ``data`` 字段(dict)。
        :raises BilibiliError: HTTP 错误或业务 ``code != 0`` 时。
        """
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BilibiliError(f"请求失败 {url}: {exc}") from exc

        payload = resp.json()
        if payload.get("code") != 0:
            raise BilibiliError(
                f"接口返回错误 code={payload.get('code')} message={payload.get('message')!r}"
            )
        return payload.get("data", {})

    async def get_room_info(self, input_url: str) -> RoomInfo:
        """解析房间号并归一化为真实房间信息。

        :param input_url: 用户输入的 URL 或房间号。
        :returns: :class:`RoomInfo`。
        :raises BilibiliError: 解析或接口调用失败时。
        """
        raw_id = parse_room_id(input_url)
        data = await self._get_json(_ROOM_INIT_URL, {"id": raw_id})
        info = RoomInfo(
            room_id=int(data["room_id"]),
            short_id=int(data.get("short_id", 0)),
            uid=int(data.get("uid", 0)),
            live_status=int(data.get("live_status", 0)),
        )
        logger.info(
            "房间解析成功: input={} -> room_id={} live_status={}",
            input_url,
            info.room_id,
            info.live_status,
        )
        return info

    async def get_streams(
        self,
        room_id: int,
        quality: int = 10000,
    ) -> list[StreamInfo]:
        """获取直播间所有可用的流地址。

        调用 ``getRoomPlayInfo``,请求 HLS 与 FLV 两种协议、ts/fmp4/flv 三种格式。

        :param room_id: 真实房间号。
        :param quality: 期望清晰度码 qn(10000=原画)。
        :returns: 解析出的 :class:`StreamInfo` 列表(可能为空,如未开播)。
        :raises BilibiliError: 接口调用失败时。
        """
        params = {
            "room_id": room_id,
            "protocol": "0,1",   # 0=http_stream(flv), 1=http_hls
            "format": "0,1,2",   # 0=flv, 1=ts, 2=fmp4
            "codec": "0,1",      # 0=avc, 1=hevc
            "qn": quality,
            "platform": "web",
            "ptype": 8,
        }
        data = await self._get_json(_PLAY_INFO_URL, params)

        if int(data.get("live_status", 0)) != 1:
            logger.warning("房间 {} 当前未开播,无可用流。", room_id)
            return []

        streams = self._parse_play_info(data)
        logger.info("房间 {} 解析到 {} 条可用流。", room_id, len(streams))
        return streams

    async def get_danmaku_server(self, room_id: int) -> DanmakuServer:
        """获取弹幕长连接服务器与鉴权 token。

        :param room_id: 真实房间号。
        :returns: :class:`DanmakuServer`。
        :raises BilibiliError: 接口调用失败时。
        """
        data = await self._get_json(_DANMU_INFO_URL, {"id": room_id, "type": 0})
        hosts = data.get("host_list") or []
        return DanmakuServer(token=data.get("token", ""), hosts=hosts)

    @staticmethod
    def _parse_play_info(data: dict) -> list[StreamInfo]:
        """解析 ``getRoomPlayInfo`` 的 ``playurl_info`` 结构为流列表。

        结构层级: stream[] -> format[] -> codec[] -> url_info[]。
        完整 URL = ``url_info.host + codec.base_url + url_info.extra``。

        :param data: ``getRoomPlayInfo`` 返回的 ``data`` 字段。
        :returns: 解析出的 :class:`StreamInfo` 列表。
        """
        result: list[StreamInfo] = []
        playurl = (data.get("playurl_info") or {}).get("playurl") or {}
        for stream in playurl.get("stream", []):
            # protocol_name: "http_stream" -> flv, "http_hls" -> hls
            protocol = "hls" if "hls" in stream.get("protocol_name", "") else "flv"
            for fmt in stream.get("format", []):
                format_name = fmt.get("format_name", "")
                for codec in fmt.get("codec", []):
                    base_url = codec.get("base_url", "")
                    current_qn = int(codec.get("current_qn", 0))
                    codec_name = codec.get("codec_name", "")
                    for url_info in codec.get("url_info", []):
                        host = url_info.get("host", "")
                        extra = url_info.get("extra", "")
                        if not host or not base_url:
                            continue
                        result.append(
                            StreamInfo(
                                url=f"{host}{base_url}{extra}",
                                protocol=protocol,
                                format_name=format_name,
                                codec_name=codec_name,
                                quality=current_qn,
                            )
                        )
        return result


def pick_best_stream(
    streams: list[StreamInfo],
    preferred_protocol: str = "hls",
) -> StreamInfo | None:
    """从候选流中按偏好挑选最佳流。

    优先级:首选协议 + 最高清晰度;若首选协议无结果,则回退到另一协议。

    :param streams: 候选流列表。
    :param preferred_protocol: 首选协议,``hls`` 或 ``flv``。
    :returns: 选中的 :class:`StreamInfo`;无可用流时返回 ``None``。
    """
    if not streams:
        return None

    def sort_key(s: StreamInfo) -> tuple[int, int]:
        # 首选协议优先(1),再按清晰度降序。
        return (1 if s.protocol == preferred_protocol else 0, s.quality)

    return max(streams, key=sort_key)
