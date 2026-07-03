"""Bilibili 直播弹幕采集(WebSocket)。

基于 B 站公开的网页端弹幕协议(已配置合规账号 cookie 时使用其鉴权):

1. 调 ``getDanmuInfo`` 取弹幕服务器列表与鉴权 token;
2. 连接 ``wss://{host}:{wss_port}/sub``,发送鉴权包(op=7,protover=3);
3. 每 30s 发送心跳(op=2),服务器以 op=3 回复在线人气;
4. 接收 op=5 消息包(brotli/zlib 压缩,内含多条 JSON),解析弹幕/礼物/SC/互动并入库。

协议的编解码是纯函数(便于单测);网络与持久化在 :class:`DanmakuClient` 中。
合规说明:仅连接平台公开的弹幕广播,不做任何逆向破解;请遵守平台条款与合理频率。
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from collections.abc import Iterator

import brotli
from loguru import logger

from app.core.config import settings
from app.db.models import Danmaku, DanmakuType
from app.db.session import get_session
from app.sources.bilibili.client import (
    BilibiliLiveClient,
    parse_uid_from_cookie,
)

# 操作码(operation)
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3   # 回复:在线人气值
OP_MESSAGE = 5           # 普通消息(弹幕/礼物等),可能压缩
OP_AUTH = 7
OP_AUTH_REPLY = 8

_HEADER = struct.Struct(">IHHII")  # 包长(4) 头长(2) 协议版本(2) 操作码(4) 序列(4)
_HEADER_LEN = 16
_HEARTBEAT_INTERVAL_S = 30
_DEFAULT_HOST = "broadcastlv.chat.bilibili.com"


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
        plen, _hlen, ver, op, _seq = _HEADER.unpack(data[offset : offset + _HEADER_LEN])
        if plen <= 0 or offset + plen > n:
            break
        body = data[offset + _HEADER_LEN : offset + plen]
        yield ver, op, body
        offset += plen


def decode(data: bytes) -> list[tuple[int, object]]:
    """解码一个(可能压缩、可能聚合)的弹幕数据帧。

    压缩包(protover==2 zlib / ==3 brotli)会被解压后递归解析,
    最终展开为若干 ``(operation, parsed)``:

    * op=3:``parsed`` 为在线人气整数;
    * op=5:``parsed`` 为消息 JSON(dict);
    * op=8:``parsed`` 为 ``None``(鉴权回复)。

    :param data: 收到的二进制帧。
    :returns: ``(operation, parsed)`` 列表。
    """
    results: list[tuple[int, object]] = []
    for ver, op, body in _iter_raw(data):
        if op == OP_HEARTBEAT_REPLY:
            popularity = int.from_bytes(body[:4], "big") if len(body) >= 4 else 0
            results.append((op, popularity))
        elif op == OP_AUTH_REPLY:
            results.append((op, None))
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
    """单个直播间的弹幕采集客户端(异步,带心跳与重连)。

    :param room_id: 真实房间号。
    :param session_id: 关联的录制会话 id(弹幕据此入库,便于按窗口统计)。
    :param cookie: 登录态 cookie(用于鉴权;留空则匿名)。
    """

    def __init__(self, room_id: int, session_id: int, cookie: str = "") -> None:
        self.room_id = room_id
        self.session_id = session_id
        self.cookie = cookie
        self.popularity = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """请求停止采集。"""
        self._stop.set()

    async def run(self) -> None:
        """启动采集主循环,断线自动重连,直到被请求停止。"""
        uid = parse_uid_from_cookie(self.cookie)
        backoff = 1
        while not self._stop.is_set():
            try:
                server = await self._fetch_server()
                await self._connect_and_consume(server, uid)
                backoff = 1
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — 弹幕断线不应中断录制
                msg = str(exc)
                # B 站 getDanmuInfo 接口要求登录态:未配置 cookie 时返回 code=-352
                # 这不是网络问题,重试也不会成功,无需反复打印 WARNING。
                if "-352" in msg and not self.cookie:
                    logger.info("弹幕接口需要登录态({}),跳过弹幕采集。", msg.strip())
                    break
                if "-352" in msg:
                    logger.warning("弹幕鉴权失败,请检查 Cookie 是否过期: {}", msg.strip())
                    break
                logger.warning("弹幕连接异常 room={}: {},{}s 后重连。", self.room_id, exc, backoff)
            if self._stop.is_set():
                break
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, settings.reconnect_max_backoff_s)
        logger.info("弹幕采集已停止 room={} session={}", self.room_id, self.session_id)

    async def _fetch_server(self) -> tuple[str, int, str]:
        """获取弹幕服务器地址与 token。

        :returns: ``(host, wss_port, token)``。
        """
        async with BilibiliLiveClient(cookie=self.cookie) as client:
            info = await client.get_danmaku_server(self.room_id)
        if info.hosts:
            host = info.hosts[0].get("host", _DEFAULT_HOST)
            port = int(info.hosts[0].get("wss_port", 443))
        else:
            host, port = _DEFAULT_HOST, 443
        return host, port, info.token

    async def _connect_and_consume(self, server: tuple[str, int, str], uid: int) -> None:
        """建立 WebSocket 连接、鉴权、心跳并消费消息。

        :param server: ``(host, wss_port, token)``。
        :param uid: 登录用户 UID(匿名为 0)。
        """
        import websockets

        host, port, token = server
        uri = f"wss://{host}:{port}/sub"
        headers = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
            ("Origin", "https://live.bilibili.com"),
        ]
        async with websockets.connect(uri, additional_headers=headers, max_size=None) as ws:
            auth = {
                "uid": uid,
                "roomid": self.room_id,
                "protover": 3,
                "platform": "web",
                "type": 2,
                "key": token,
            }
            await ws.send(encode_packet(OP_AUTH, json.dumps(auth).encode("utf-8")))
            logger.info("弹幕已连接 room={} host={} uid={}", self.room_id, host, uid)

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

    async def _heartbeat(self, ws: object) -> None:
        """周期性发送心跳包以维持连接。

        :param ws: WebSocket 连接。
        """
        try:
            while not self._stop.is_set():
                await ws.send(encode_packet(OP_HEARTBEAT))  # type: ignore[attr-defined]
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _handle_frame(self, frame: bytes) -> None:
        """解码一帧并把其中的弹幕消息批量入库。

        :param frame: 收到的二进制帧。
        """
        rows: list[Danmaku] = []
        for op, parsed in decode(frame):
            if op == OP_HEARTBEAT_REPLY and isinstance(parsed, int):
                self.popularity = parsed
            elif op == OP_MESSAGE and isinstance(parsed, dict):
                result = parse_message(parsed)
                if result is None:
                    continue
                msg_type, user, content, value = result
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
