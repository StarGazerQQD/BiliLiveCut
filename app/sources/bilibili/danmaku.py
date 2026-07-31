"""Bilibili 直播弹幕采集(WebSocket)。

基于 B 站直播网页实际使用的公开链路:

1. 读取 WBI 图片键并以网页参数签名 ``getDanmuInfo``;
2. 有 Cookie 时优先登录访问，业务错误后立即回退无 Cookie 的匿名请求;
3. 连接 ``wss://{host}:{wss_port}/sub``,发送鉴权包(op=7,protover=3);
4. 匿名采集期间按配置间隔重试登录访问，达到单场失败上限后熔断;
5. 每 30s 发送心跳(op=2),服务器以 op=3 回复在线人气;
6. 接收 op=5 消息包(brotli/zlib 压缩,内含多条 JSON),解析弹幕/礼物/SC/互动并入库。

协议的编解码是纯函数(便于单测);网络与持久化在 :class:`DanmakuClient` 中。
登录 Cookie 仅发送给 HTTPS 网页接口，不写入日志或持久化短期 token；匿名兜底不发送 Cookie。
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import brotli
from loguru import logger

from app.core.config import settings
from app.db.models import Danmaku, DanmakuType
from app.db.session import get_session
from app.sources.bilibili.client import (
    BilibiliError,
    BilibiliLiveClient,
    BilibiliRateLimitError,
    DanmakuServer,
    HttpErrorType,
    parse_uid_from_cookie,
)

# 操作码(operation)
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3  # 回复:在线人气值
OP_MESSAGE = 5  # 普通消息(弹幕/礼物等),可能压缩
OP_AUTH = 7
OP_AUTH_REPLY = 8

_HEADER = struct.Struct(">IHHII")  # 包长(4) 头长(2) 协议版本(2) 操作码(4) 序列(4)
_HEADER_LEN = 16
_HEARTBEAT_INTERVAL_S = 30
_AUTH_TIMEOUT_S = 10
_DEFAULT_HOST = "broadcastlv.chat.bilibili.com"
_DEFAULT_WSS_PORT = 2245
_LIVE_ORIGIN = "https://live.bilibili.com"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class DanmakuProtocolError(BilibiliError):
    """弹幕 WebSocket 鉴权或协议响应无效。"""


class _WebSocketConnection(Protocol):
    """弹幕客户端使用的最小 WebSocket 接口。"""

    async def send(self, message: bytes) -> None:
        """发送二进制协议包。"""

    async def recv(self) -> str | bytes:
        """接收一个 WebSocket 帧。"""


@dataclass(frozen=True, slots=True)
class DanmakuAccess:
    """一次弹幕连接使用的 token、UID 与访问模式。

    :param server: ``getDanmuInfo`` 返回的 token 与候选节点。
    :param uid: WebSocket 鉴权 UID；匿名模式固定为 0。
    :param uses_cookie: token 请求是否携带登录 Cookie。
    """

    server: DanmakuServer
    uid: int
    uses_cookie: bool


# --------------------------------------------------------------------------- #
# 协议编解码(纯函数)
# --------------------------------------------------------------------------- #
def encode_packet(operation: int, payload: bytes = b"", protover: int = 1) -> bytes:
    """编码一个弹幕协议数据包。

    :param operation: 操作码(如 :data:`OP_AUTH` / :data:`OP_HEARTBEAT`)。
    :param payload: 包体字节。
    :param protover: 协议版本。
    :returns: 完整数据包字节(头 + 体)。
    """
    header = _HEADER.pack(_HEADER_LEN + len(payload), _HEADER_LEN, protover, operation, 1)
    return header + payload


def _iter_raw(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """按包长切分原始字节流,逐个产出 ``(protover, operation, body)``。

    :param data: 原始字节流(可能含多个相邻数据包)。
    :yields: ``(protover, operation, body)`` 三元组。
    """
    offset = 0
    n = len(data)
    while offset + _HEADER_LEN <= n:
        plen, hlen, ver, op, _seq = _HEADER.unpack(data[offset : offset + _HEADER_LEN])
        if plen < _HEADER_LEN or hlen < _HEADER_LEN or hlen > plen or offset + plen > n:
            break
        body = data[offset + hlen : offset + plen]
        yield ver, op, body
        offset += plen


def decode(data: bytes) -> list[tuple[int, object]]:
    """解码一个(可能压缩、可能聚合)的弹幕数据帧。

    压缩包(protover==2 zlib / ==3 brotli)会被解压后递归解析,
    最终展开为若干 ``(operation, parsed)``:

    * op=3:``parsed`` 为在线人气整数;
    * op=5:``parsed`` 为消息 JSON(dict);
    * op=8:``parsed`` 为鉴权 JSON 字典;无法解析时为 ``None``。

    :param data: 收到的二进制帧。
    :returns: ``(operation, parsed)`` 列表。
    """
    results: list[tuple[int, object]] = []
    for ver, op, body in _iter_raw(data):
        if op == OP_HEARTBEAT_REPLY:
            popularity = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
            results.append((op, popularity))
        elif op == OP_AUTH_REPLY:
            try:
                auth_reply = json.loads(body.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                auth_reply = None
            results.append((op, auth_reply if isinstance(auth_reply, dict) else None))
        elif op == OP_MESSAGE:
            if ver == 2:
                results.extend(decode(zlib.decompress(body)))
            elif ver == 3:
                results.extend(decode(brotli.decompress(body)))
            else:
                try:
                    results.append((op, json.loads(body.decode("utf-8", errors="ignore"))))
                except json.JSONDecodeError:
                    pass
    return results


def server_endpoints(hosts: list[dict]) -> list[tuple[str, int]]:
    """清洗并去重弹幕候选 WSS 节点。

    :param hosts: ``getDanmuInfo.host_list``。
    :returns: 按服务端顺序排列的 ``(host, wss_port)``；空列表会回退到公开广播节点。
    """
    endpoints: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in hosts:
        host = str(item.get("host", "")).strip()
        try:
            port = int(item.get("wss_port", 0))
        except (TypeError, ValueError):
            continue
        endpoint = (host, port)
        if not host or not 1 <= port <= 65535 or endpoint in seen:
            continue
        seen.add(endpoint)
        endpoints.append(endpoint)
    return endpoints or [(_DEFAULT_HOST, _DEFAULT_WSS_PORT)]


def parse_message(msg: dict) -> tuple[str, str | None, str | None, float] | None:
    """把一条消息 JSON 解析为 ``(类型, 用户, 内容, 价值)``。

    支持普通弹幕、礼物、醒目留言(SC)、进场互动;其它命令返回 ``None`` 忽略。

    :param msg: 消息 JSON。
    :returns: 四元组或 ``None``。
    """
    cmd = str(msg.get("cmd", ""))
    if cmd.startswith("DANMU_MSG"):
        info = msg.get("info") or []
        text = info[1] if len(info) > 1 else ""
        user = info[2][1] if len(info) > 2 and len(info[2]) > 1 else None
        return DanmakuType.DANMAKU, user, text, 1.0
    if cmd == "SEND_GIFT":
        d = msg.get("data") or {}
        # 价值用电池/金瓜子粗略折算(仅作热度权重,非精确金额)。
        value = float(d.get("total_coin", 0)) / 1000.0 or 1.0
        return DanmakuType.GIFT, d.get("uname"), d.get("giftName"), value
    if cmd == "SUPER_CHAT_MESSAGE":
        d = msg.get("data") or {}
        return (
            DanmakuType.SUPERCHAT,
            (d.get("user_info") or {}).get("uname"),
            d.get("message"),
            float(d.get("price", 0)),
        )
    if cmd == "INTERACT_WORD":
        d = msg.get("data") or {}
        return DanmakuType.INTERACT, d.get("uname"), None, 0.2
    return None


# --------------------------------------------------------------------------- #
# 采集客户端
# --------------------------------------------------------------------------- #
class DanmakuClient:
    """单个直播间的登录优先、匿名兜底弹幕采集客户端。

    :param room_id: 真实房间号。
    :param session_id: 关联的录制会话 id(弹幕据此入库,便于按窗口统计)。
    :param cookie: 可选登录 Cookie；仅用于 HTTPS token 请求，不写入日志。
    :param login_retry_max_attempts: 单场登录失败上限(首次计入)；``None`` 使用全局配置。
    :param login_retry_interval_s: 登录失败后的重试间隔；``None`` 使用全局配置。
    """

    def __init__(
        self,
        room_id: int,
        session_id: int,
        cookie: str = "",
        *,
        login_retry_max_attempts: int | None = None,
        login_retry_interval_s: float | None = None,
    ) -> None:
        max_attempts = (
            settings.danmaku_login_retry_max_attempts if login_retry_max_attempts is None else login_retry_max_attempts
        )
        retry_interval = (
            settings.danmaku_login_retry_interval_s if login_retry_interval_s is None else login_retry_interval_s
        )
        if max_attempts < 0:
            raise ValueError("login_retry_max_attempts 必须 >= 0")
        if retry_interval <= 0:
            raise ValueError("login_retry_interval_s 必须 > 0")

        self.room_id = room_id
        self.session_id = session_id
        self.popularity = 0
        self._stop = asyncio.Event()
        self._cookie = cookie.strip()
        self._login_uid = parse_uid_from_cookie(self._cookie)
        self._login_retry_max_attempts = max_attempts
        self._login_retry_interval_s = retry_interval
        self._login_failures = 0
        self._next_login_retry_at = 0.0
        # V0.1.13: DanmakuSampler — lazy init per-room sampler
        self._sampler = None  # type: ignore[var-annotated]

    def stop(self) -> None:
        """请求停止采集。"""
        self._stop.set()

    async def run(self) -> None:
        """启动采集主循环，登录失败时匿名兜底并定时恢复登录。"""
        backoff = 1
        access: DanmakuAccess | None = None
        while not self._stop.is_set():
            try:
                if access is None:
                    access = await self._select_access()
                replacement = await self._consume_access(access)
                access = replacement
                backoff = 1
            except asyncio.CancelledError:
                break
            except DanmakuProtocolError as exc:
                if access is not None and access.uses_cookie:
                    self._record_login_failure(exc)
                    access = None
                    continue
                access = None
                logger.warning(
                    "匿名弹幕鉴权异常 room={}: {},{}s 后重连。",
                    self.room_id,
                    exc,
                    self._login_retry_interval_s,
                )
                await self._sleep_or_stop(self._login_retry_interval_s)
                continue
            except BilibiliRateLimitError as exc:
                access = None
                if exc.error_type in {HttpErrorType.COOKIE_EXPIRED, HttpErrorType.RISK_CONTROL}:
                    logger.warning(
                        "匿名弹幕接口返回 {} room={}: {}；{}s 后重试，录制与实时转写不受影响。",
                        exc.error_type.value,
                        self.room_id,
                        str(exc).strip(),
                        self._login_retry_interval_s,
                    )
                    await self._sleep_or_stop(self._login_retry_interval_s)
                    continue
                logger.warning("弹幕连接异常 room={}: {},{}s 后重连。", self.room_id, exc, backoff)
            except Exception as exc:  # noqa: BLE001 — 弹幕断线不应中断录制
                access = None
                logger.warning("弹幕连接异常 room={}: {},{}s 后重连。", self.room_id, exc, backoff)
            if self._stop.is_set():
                break
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, settings.reconnect_max_backoff_s)
        logger.info("弹幕采集已停止 room={} session={}", self.room_id, self.session_id)

    @property
    def login_disabled(self) -> bool:
        """本场录制是否已禁用登录 Cookie 弹幕访问。"""
        return not self._cookie or self._login_failures >= self._login_retry_max_attempts

    async def _select_access(self) -> DanmakuAccess:
        """选择当前应使用的登录或匿名访问模式。"""
        logged = await self._try_logged_access()
        if logged is not None:
            return logged
        return await self._fetch_anonymous_access()

    async def _try_logged_access(self) -> DanmakuAccess | None:
        """在重试窗口允许时尝试登录访问；失败时记录单场熔断状态。"""
        if self.login_disabled or time.monotonic() < self._next_login_retry_at:
            return None
        try:
            server = await self._fetch_server(self._cookie)
        except BilibiliError as exc:
            self._record_login_failure(exc)
            return None
        logger.info(
            "登录 Cookie 弹幕访问成功 room={} uid={} prior_failures={}",
            self.room_id,
            self._login_uid,
            self._login_failures,
        )
        return DanmakuAccess(server=server, uid=self._login_uid, uses_cookie=True)

    async def _fetch_anonymous_access(self) -> DanmakuAccess:
        """获取不携带 Cookie 的匿名 token 与候选节点。"""
        server = await self._fetch_server("")
        return DanmakuAccess(server=server, uid=0, uses_cookie=False)

    async def _fetch_server(self, cookie: str) -> DanmakuServer:
        """按指定 Cookie 获取弹幕服务器地址与短期 token。

        :param cookie: 登录 Cookie；空字符串表示匿名请求。
        :returns: 包含候选节点与 token 的 :class:`DanmakuServer`。
        """
        async with BilibiliLiveClient(cookie=cookie) as client:
            return await client.get_danmaku_server(self.room_id)

    def _record_login_failure(self, exc: Exception) -> None:
        """记录一次登录链路失败并安排重试或熔断本场登录访问。"""
        self._login_failures += 1
        if self.login_disabled:
            logger.warning(
                "登录 Cookie 弹幕访问失败 room={} attempt={}/{}；已达到单场上限，后续固定匿名访问: {}",
                self.room_id,
                self._login_failures,
                self._login_retry_max_attempts,
                str(exc).strip(),
            )
            return
        self._next_login_retry_at = time.monotonic() + self._login_retry_interval_s
        logger.warning(
            "登录 Cookie 弹幕访问失败 room={} attempt={}/{}；已立即回退匿名，{}s 后重试: {}",
            self.room_id,
            self._login_failures,
            self._login_retry_max_attempts,
            self._login_retry_interval_s,
            str(exc).strip(),
        )

    async def _consume_access(self, access: DanmakuAccess) -> DanmakuAccess | None:
        """消费当前连接；匿名模式下并行等待下一次登录探测。"""
        if access.uses_cookie or self.login_disabled or self._login_failures == 0:
            await self._connect_and_consume(access)
            return None
        return await self._consume_anonymous_with_login_retry(access)

    async def _consume_anonymous_with_login_retry(self, access: DanmakuAccess) -> DanmakuAccess | None:
        """保持匿名连接，同时按配置间隔探测登录链路。"""
        consume_task = asyncio.create_task(self._connect_and_consume(access))
        try:
            while not self._stop.is_set():
                if self.login_disabled:
                    await consume_task
                    return None
                delay = max(0.0, self._next_login_retry_at - time.monotonic())
                timer_task = asyncio.create_task(self._sleep_or_stop(delay))
                done, _pending = await asyncio.wait(
                    {consume_task, timer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if consume_task in done:
                    timer_task.cancel()
                    await asyncio.gather(timer_task, return_exceptions=True)
                    await consume_task
                    return None
                await timer_task
                if self._stop.is_set():
                    return None
                logged = await self._try_logged_access()
                if logged is not None:
                    logger.info("登录 Cookie 弹幕访问已恢复 room={}，准备切回登录连接。", self.room_id)
                    return logged
            return None
        finally:
            if not consume_task.done():
                consume_task.cancel()
            await asyncio.gather(consume_task, return_exceptions=True)

    async def _connect_and_consume(self, access: DanmakuAccess) -> None:
        """按顺序尝试候选节点并消费弹幕。

        :param access: 候选节点、短期 token、UID 与访问模式。
        :raises BilibiliError: 所有候选节点均不可用时。
        :raises DanmakuProtocolError: 服务端拒绝鉴权或返回无效鉴权包时。
        """
        last_error: Exception | None = None
        endpoints = server_endpoints(access.server.hosts)
        for index, (host, port) in enumerate(endpoints, start=1):
            if self._stop.is_set():
                return
            try:
                await self._consume_endpoint(host, port, access)
                return
            except asyncio.CancelledError:
                raise
            except DanmakuProtocolError:
                raise
            except Exception as exc:  # noqa: BLE001 — 单节点故障应继续尝试服务端候选列表
                last_error = exc
                logger.info(
                    "弹幕节点不可用 room={} host={} port={} ({}/{}): {}",
                    self.room_id,
                    host,
                    port,
                    index,
                    len(endpoints),
                    exc,
                )
        raise BilibiliError(f"全部 {len(endpoints)} 个弹幕节点均不可用") from last_error

    async def _consume_endpoint(self, host: str, port: int, access: DanmakuAccess) -> None:
        """连接单个节点，完成鉴权后持续消费消息。

        :param host: WSS 主机名。
        :param port: WSS 端口。
        :param access: 当前 token、UID 与访问模式。
        """
        import websockets

        uri = f"wss://{host}:{port}/sub"
        async with websockets.connect(
            uri,
            origin=_LIVE_ORIGIN,
            user_agent_header=_USER_AGENT,
            max_size=None,
            open_timeout=_AUTH_TIMEOUT_S,
            close_timeout=5,
            ping_interval=None,
        ) as ws:
            await self._authenticate(ws, access.server.token, uid=access.uid)
            mode = "登录" if access.uses_cookie else "匿名"
            logger.info("{}弹幕已连接 room={} host={} uid={}", mode, self.room_id, host, access.uid)

            heartbeat = asyncio.create_task(self._heartbeat(ws))
            try:
                while not self._stop.is_set():
                    frame = await asyncio.wait_for(ws.recv(), timeout=35)
                    if isinstance(frame, str):
                        frame = frame.encode("utf-8")
                    self._handle_frame(frame)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _authenticate(self, ws: _WebSocketConnection, token: str, *, uid: int = 0) -> None:
        """发送鉴权包并确认服务端 ``op=8/code=0``。

        :param ws: WebSocket 连接。
        :param token: ``getDanmuInfo`` 返回的短期 token。
        :param uid: 登录用户 UID；匿名模式为 0。
        :raises DanmakuProtocolError: 超时、缺少鉴权回复或服务端拒绝时。
        """
        auth = {
            "uid": uid,
            "roomid": self.room_id,
            "protover": 3,
            "platform": "web",
            "type": 2,
            "key": token,
        }
        await ws.send(encode_packet(OP_AUTH, json.dumps(auth, separators=(",", ":")).encode("utf-8")))
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT_S)
        except TimeoutError as exc:
            raise DanmakuProtocolError("等待弹幕鉴权回复超时") from exc
        if isinstance(frame, str):
            frame = frame.encode("utf-8")
        for operation, parsed in decode(frame):
            if operation != OP_AUTH_REPLY:
                continue
            if not isinstance(parsed, dict):
                raise DanmakuProtocolError("弹幕鉴权回复不是有效 JSON")
            code = parsed.get("code")
            if code != 0:
                raise DanmakuProtocolError(f"弹幕鉴权被拒绝 code={code!r}")
            return
        raise DanmakuProtocolError("服务端首帧缺少弹幕鉴权回复")

    async def _heartbeat(self, ws: _WebSocketConnection) -> None:
        """周期性发送心跳包以维持连接。

        :param ws: WebSocket 连接。
        """
        try:
            while not self._stop.is_set():
                await ws.send(encode_packet(OP_HEARTBEAT))
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _handle_frame(self, frame: bytes) -> None:
        """解码一帧并把其中的弹幕消息批量入库。

        :param frame: 收到的二进制帧。
        """
        # V0.1.13: Lazy-init per-room DanmakuSampler
        if self._sampler is None:
            from app.analysis.danmaku_sampling import get_sampler

            self._sampler = get_sampler(self.room_id)

        rows: list[Danmaku] = []
        for op, parsed in decode(frame):
            if op == OP_HEARTBEAT_REPLY and isinstance(parsed, int):
                self.popularity = parsed
            elif op == OP_MESSAGE and isinstance(parsed, dict):
                result = parse_message(parsed)
                if result is None:
                    continue
                msg_type, user, content, value = result

                # V0.1.13: Sampling — always record for density, check if keep
                self._sampler.record()
                if not self._sampler.should_keep(msg_type):
                    continue

                rows.append(
                    Danmaku(
                        session_id=self.session_id,
                        room_id=self.room_id,
                        msg_type=msg_type,
                        user=user,
                        content=content,
                        value=value,
                    )
                )
        if rows:
            with get_session() as db:
                db.add_all(rows)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """休眠指定秒数,期间收到停止信号则提前返回。

        :param seconds: 休眠时长(秒)。
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
