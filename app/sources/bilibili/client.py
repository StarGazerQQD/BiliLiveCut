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

import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx
from loguru import logger

from app.sources.bilibili.wbi import WbiKeys, sign_wbi_params

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
_ROOM_DETAIL_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"
_PLAY_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
_DANMU_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
_WBI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_LIVE_WEB_LOCATION = "444.8"
_QUALITY_FALLBACKS = (10000, 400, 250, 150, 80)
_MAX_QUALITY_ATTEMPTS = 8

# 从形如 https://live.bilibili.com/123?xxx 的 URL 中提取房间号。
_ROOM_ID_RE = re.compile(r"live\.bilibili\.com/(?:h5/)?(\d+)")


class BilibiliError(Exception):
    """Bilibili 接口调用相关错误。"""


class BilibiliStreamUnavailable(BilibiliError):
    """接口确认直播中，但画质协商未取得可用播放地址。"""


class BilibiliTransportError(BilibiliError):
    """网络或 HTTP 传输失败，与平台明确的登录态拒绝分别处理。"""

    def __init__(
        self, message: str, *, retryable: bool = True, status_code: int | None = None, retry_after_seconds: float = 0.0
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class BilibiliAuthenticationError(BilibiliError):
    """HTTP 明确拒绝鉴权；仅实际携带 Cookie 的弹幕登录请求计入登录预算。"""

    def __init__(self, message: str, *, cookie_login_request: bool) -> None:
        super().__init__(message)
        self.cookie_login_request = cookie_login_request


class HttpErrorType(Enum):
    """Bilibili 接口 HTTP 及业务错误分类。"""

    RATE_LIMITED = "rate_limited"
    PRECONDITION_FAILED = "precondition_failed"
    RISK_CONTROL = "risk_control"
    COOKIE_EXPIRED = "cookie_expired"
    ACCOUNT_BANNED = "account_banned"


class BilibiliRateLimitError(BilibiliError):
    """Bilibili 风控/限流异常,携带错误类型与重试等待建议。

    :param error_type: 错误类型枚举。
    :param message: 人类可读的错误描述。
    :param retry_after_seconds: 建议的等待秒数,默认 60。
    """

    def __init__(
        self,
        error_type: HttpErrorType,
        message: str,
        retry_after_seconds: float = 60.0,
    ) -> None:
        self.error_type = error_type
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"[{error_type.name}] {message}")


class CircuitBreaker:
    """简单的熔断器,基于连续失败次数做指数退避。

    每次调用 :meth:`record_failure` 增加失败计数,并将恢复时间窗口
    设为 ``base_delay_s * 2^fail_count``。在窗口内 :meth:`is_open`
    返回 ``True``,表示应暂停对该房间的访问。

    :param base_delay_s: 基础退避秒数。
    """

    def __init__(self, base_delay_s: float = 2.0) -> None:
        self.fail_count: int = 0
        self.last_fail_time: float = 0.0
        self.backoff_until: float = 0.0
        self.base_delay_s = base_delay_s

    def record_failure(self) -> None:
        """记录一次失败并更新退避时间窗。"""
        self.fail_count += 1
        self.last_fail_time = time.monotonic()
        self.backoff_until = self.last_fail_time + self.base_delay_s * (2**self.fail_count)

    def is_open(self) -> bool:
        """熔断器是否打开(当前处于退避中)。"""
        return time.monotonic() < self.backoff_until

    def reset(self) -> None:
        """清除失败状态,关闭熔断器。"""
        self.fail_count = 0
        self.last_fail_time = 0.0
        self.backoff_until = 0.0


# 按房间维度的熔断器字典。
_ROOM_BREAKERS: dict[int, CircuitBreaker] = {}


def get_room_breaker(room_id: int) -> CircuitBreaker:
    """获取或创建指定房间的熔断器实例。

    :param room_id: 目标房间号。
    :returns: 该房间对应的 :class:`CircuitBreaker`。
    """
    if room_id not in _ROOM_BREAKERS:
        _ROOM_BREAKERS[room_id] = CircuitBreaker()
    return _ROOM_BREAKERS[room_id]


@dataclass(slots=True)
class RoomInfo:
    """直播间基础信息(来自 ``room_init``)。

    :param room_id: 归一化后的真实房间号。
    :param short_id: 短号(可能为 0)。
    :param uid: 主播 UID。
    :param live_status: 开播状态,1 表示直播中。
    :param title: 直播间标题。
    :param uploader_name: 主播名称。
    """

    room_id: int
    short_id: int
    uid: int
    live_status: int | None
    title: str | None = None
    uploader_name: str | None = None
    detail_status: str = "not_requested"
    detail_error: BilibiliError | None = field(default=None, repr=False)

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
    :param quality: 实际清晰度码(qn)，接口未提供时为 None。
    """

    url: str
    protocol: str
    format_name: str
    codec_name: str
    quality: int | None


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

    async def _request_payload(self, url: str, params: dict[str, object] | None = None) -> dict:
        """发起 GET 请求并返回 B 站统一响应体。

        :param url: 接口地址。
        :param params: 可选查询参数。
        :returns: 完整响应体字典。
        :raises BilibiliError: HTTP 或 JSON 响应无效时。
        :raises BilibiliRateLimitError: HTTP 层触发风控/限流时。
        """
        try:
            resp = await self._client.get(url, params=params)

            # 解析 Retry-After 头(若存在)。
            retry_after_val = resp.headers.get("Retry-After", "")
            try:
                retry_after = float(retry_after_val) if retry_after_val else 60.0
                if not math.isfinite(retry_after) or retry_after < 0:
                    retry_after = 60.0
            except ValueError:
                retry_after = 60.0

            # HTTP 状态码级别的风控处理。
            if resp.status_code in {403, 429}:
                raise BilibiliRateLimitError(
                    HttpErrorType.RATE_LIMITED,
                    f"HTTP {resp.status_code} 请求被拒绝 {url}",
                    retry_after,
                )
            if resp.status_code == 412:
                raise BilibiliRateLimitError(
                    HttpErrorType.PRECONDITION_FAILED,
                    f"HTTP 412 前置条件失败 {url}",
                    retry_after,
                )

            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                raise BilibiliAuthenticationError(
                    f"HTTP 401 接口鉴权被拒绝 {url}",
                    cookie_login_request=url == _DANMU_INFO_URL and bool(exc.request.headers.get("Cookie")),
                ) from exc
            raise BilibiliTransportError(
                f"HTTP {status} 接口请求失败 {url}",
                retryable=status in {408, 425, 500, 502, 503, 504},
                status_code=status,
                retry_after_seconds=retry_after if retry_after_val else 0.0,
            ) from exc
        except httpx.RequestError as exc:
            raise BilibiliTransportError(f"{type(exc).__name__} 接口传输失败 {url}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise BilibiliError(f"接口返回了无效 JSON {url}") from exc
        if not isinstance(payload, dict):
            raise BilibiliError(f"接口返回结构无效 {url}")
        return payload

    async def _get_json(self, url: str, params: dict[str, object]) -> dict:
        """发起 GET 请求并校验 B 站统一返回结构 ``{code, message, data}``。

        :param url: 接口地址。
        :param params: 查询参数。
        :returns: ``data`` 字段(dict)。
        :raises BilibiliError: HTTP 错误或业务 ``code != 0`` 时。
        :raises BilibiliRateLimitError: 触发风控/限流/登录态失效时。
        """
        payload = await self._request_payload(url, params)
        self._validate_api_code(payload)
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _validate_api_code(payload: dict[str, object], *, anonymous_nav: bool = False) -> None:
        """统一业务错误分类，匿名导航的 -101 仍允许读取公开 WBI 图片键。"""
        code = payload.get("code")
        if code == 0 or anonymous_nav and code == -101:
            return

        # 业务级错误码分类处理。
        if code == -101:
            raise BilibiliRateLimitError(
                HttpErrorType.COOKIE_EXPIRED,
                f"未登录或 cookie 已过期, code=-101 message={payload.get('message')!r}",
            )
        if code == -352:
            raise BilibiliRateLimitError(
                HttpErrorType.RISK_CONTROL,
                f"接口触发平台风控, code=-352 message={payload.get('message')!r}",
            )
        if code == -412:
            raise BilibiliRateLimitError(
                HttpErrorType.ACCOUNT_BANNED,
                f"请求被拒绝, code=-412 message={payload.get('message')!r}",
            )
        raise BilibiliError(f"接口返回错误 code={code} message={payload.get('message')!r}")

    async def _get_wbi_keys(self) -> WbiKeys:
        """匿名读取网页当前使用的 WBI 图片键。

        导航接口在未登录时会返回 ``code=-101``，但仍会在 ``data.wbi_img``
        提供公开图片键；这里仅消费该字段，不把未登录视为错误。

        :returns: 当前 WBI 图片键。
        :raises BilibiliError: 响应缺少或包含无效键时。
        """
        payload = await self._request_payload(_WBI_NAV_URL)
        self._validate_api_code(payload, anonymous_nav=True)
        data = payload.get("data")
        wbi_img = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi_img, dict):
            raise BilibiliError("WBI 导航响应缺少 wbi_img")
        try:
            return WbiKeys.from_urls(str(wbi_img.get("img_url", "")), str(wbi_img.get("sub_url", "")))
        except ValueError as exc:
            raise BilibiliError("WBI 导航响应包含无效图片键") from exc

    async def get_room_info(self, input_url: str, *, include_detail: bool = True) -> RoomInfo:
        """解析房间号并归一化为真实房间信息。

        :param input_url: 用户输入的 URL 或房间号。
        :param include_detail: 是否额外请求主播名与直播标题。
        :returns: :class:`RoomInfo`。
        :raises BilibiliError: 解析或接口调用失败时。
        """
        raw_id = parse_room_id(input_url)
        data = await self._get_json(_ROOM_INIT_URL, {"id": raw_id})
        room_id = int(data["room_id"])
        title: str | None = None
        uploader_name: str | None = None
        uid = int(data.get("uid", 0))
        detail_status = "not_requested"
        detail_error: BilibiliError | None = None
        if include_detail:
            try:
                detail = await self._get_json(_ROOM_DETAIL_URL, {"room_id": room_id})
                room_detail = detail.get("room_info")
                anchor_detail = detail.get("anchor_info")
                if isinstance(room_detail, dict):
                    raw_title = str(room_detail.get("title") or "").strip()
                    title = raw_title or None
                    uid = int(room_detail.get("uid") or uid)
                if isinstance(anchor_detail, dict):
                    base_info = anchor_detail.get("base_info")
                    if isinstance(base_info, dict):
                        raw_name = str(base_info.get("uname") or "").strip()
                        uploader_name = raw_name or None
                detail_status = "fresh" if title else "unavailable"
            except BilibiliError as exc:
                detail_status = "failed"
                detail_error = exc
                # 基础 room_init 已成功时，详情失败不应阻止录制；仅退化为房间号显示。
                logger.debug("房间 {} 主播资料读取失败,继续使用基础信息: {}", room_id, exc)

        info = RoomInfo(
            room_id=room_id,
            short_id=int(data.get("short_id", 0)),
            uid=uid,
            live_status=data.get("live_status") if type(data.get("live_status")) is int else None,
            title=title,
            uploader_name=uploader_name,
            detail_status=detail_status,
            detail_error=detail_error,
        )
        logger.info(
            "房间解析成功: input={} -> room_id={} uploader={} live_status={}",
            input_url,
            info.room_id,
            info.uploader_name or "-",
            info.live_status,
        )
        return info

    async def get_streams(
        self,
        room_id: int,
        quality: int = 10000,
    ) -> list[StreamInfo]:
        """从画质上限向下逐级获取首个有可用地址的画质。

        调用 ``getRoomPlayInfo``,请求 HLS 与 FLV 两种协议、ts/fmp4/flv 三种格式。

        :param room_id: 真实房间号。
        :param quality: 清晰度上限 qn(10000=原画)，不足时依次尝试更低档。
        :returns: 解析出的 :class:`StreamInfo` 列表(可能为空,如未开播)。
        :raises BilibiliError: 接口调用失败时。
        """
        if quality <= 0:
            raise ValueError("Bilibili 清晰度编号必须大于 0")
        params: dict[str, object] = {
            "room_id": room_id,
            "protocol": "0,1",  # 0=http_stream(flv), 1=http_hls
            "format": "0,1,2",  # 0=flv, 1=ts, 2=fmp4
            "codec": "0,1",  # 0=avc, 1=hevc
            "qn": quality,
            "platform": "web",
            "ptype": 8,
        }
        pending = sorted({quality, *(qn for qn in _QUALITY_FALLBACKS if qn < quality)}, reverse=True)
        for attempt in range(_MAX_QUALITY_ATTEMPTS):
            if not pending:
                break
            requested = pending.pop(0)
            params["qn"] = requested
            # 登录态、风控、网络及业务错误仍抛给来源预算，不伪装为画质不可用。
            data = await self._get_json(_PLAY_INFO_URL, params)
            live_status = data.get("live_status")
            if not isinstance(live_status, bool) and live_status in (0, 2, "0", "2"):
                logger.info("房间 {} 当前未开播，无可用流。", room_id)
                return []
            if live_status not in (1, "1") or isinstance(live_status, bool):
                raise BilibiliError("播放接口直播状态缺失或无效")
            try:
                streams = [
                    item for item in self._parse_play_info(data) if item.quality is None or item.quality <= quality
                ]
            except (AttributeError, TypeError, ValueError) as exc:
                raise BilibiliError("播放接口流结构无效") from exc
            if streams:
                logger.info("房间 {} 取流成功 requested_qn={} candidates={}", room_id, requested, len(streams))
                return streams
            lower_defaults = {qn for qn in _QUALITY_FALLBACKS if qn < requested}
            additional = sorted(
                {qn for qn in (*pending, *self._available_qualities(data)) if qn < requested} - lower_defaults,
                reverse=True,
            )
            remaining = _MAX_QUALITY_ATTEMPTS - attempt - 1
            # 保留标准低档，避免异常元数据挤占全部预算后永远试不到低画质。
            pending = sorted(lower_defaults | set(additional[: max(0, remaining - len(lower_defaults))]), reverse=True)
            logger.info("房间 {} 画质无可用地址 qn={}，继续尝试更低画质", room_id, requested)
        logger.warning("房间 {} 画质序列无可用地址，交给录制重连预算处理", room_id)
        raise BilibiliStreamUnavailable("直播中，但画质序列没有可用播放地址")

    @staticmethod
    def _available_qualities(data: dict[str, object]) -> list[int]:
        """提取接口声明的画质编号，异常元数据不能扩大请求次数或改变顺序。"""
        info = data.get("playurl_info")
        playurl = info.get("playurl") if isinstance(info, dict) else None
        if not isinstance(playurl, dict):
            return []
        values: list[object] = []
        descriptions = playurl.get("g_qn_desc", [])
        if isinstance(descriptions, list):
            values.extend(item.get("qn") for item in descriptions if isinstance(item, dict))
        for stream in playurl.get("stream", []):
            if not isinstance(stream, dict):
                continue
            for fmt in stream.get("format", []):
                if not isinstance(fmt, dict):
                    continue
                for codec in fmt.get("codec", []):
                    if isinstance(codec, dict) and isinstance(codec.get("accept_qn"), list):
                        values.extend(codec["accept_qn"])
        return sorted({item for item in values if isinstance(item, int) and not isinstance(item, bool) and item > 0})

    async def get_danmaku_server(self, room_id: int) -> DanmakuServer:
        """按直播网页请求格式获取弹幕服务器与鉴权 token。

        是否携带登录 Cookie 由构造当前客户端时的 ``cookie`` 参数决定；
        WBI 签名流程在登录与匿名模式下保持一致。

        :param room_id: 真实房间号。
        :returns: :class:`DanmakuServer`。
        :raises BilibiliError: 接口调用失败时。
        """
        keys = await self._get_wbi_keys()
        params = sign_wbi_params(
            {"id": room_id, "type": 0, "web_location": _LIVE_WEB_LOCATION},
            keys,
        )
        data = await self._get_json(_DANMU_INFO_URL, params)
        hosts = data.get("host_list") or []
        token = str(data.get("token", ""))
        if not token:
            raise BilibiliError("弹幕 token 响应缺少 token")
        return DanmakuServer(
            token=token,
            hosts=[host for host in hosts if isinstance(host, dict)],
        )

    @staticmethod
    def _parse_play_info(data: dict[str, object]) -> list[StreamInfo]:
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
                    qn = codec.get("current_qn")
                    if isinstance(qn, str) and qn.isascii() and qn.isdecimal():
                        qn = int(qn)
                    current_qn = qn if isinstance(qn, int) and not isinstance(qn, bool) and qn > 0 else None
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

    优先级:实际清晰度从高到低，同画质优先首选协议；未知画质排在已知画质后。

    :param streams: 候选流列表。
    :param preferred_protocol: 首选协议,``hls`` 或 ``flv``。
    :returns: 选中的 :class:`StreamInfo`;无可用流时返回 ``None``。
    """
    if not streams:
        return None

    def sort_key(s: StreamInfo) -> tuple[int, int]:
        # 按实际画质排序，协议偏好只用于同档候选。
        return (s.quality if s.quality is not None else -1, 1 if s.protocol == preferred_protocol else 0)

    return max(streams, key=sort_key)
