"""弹幕模块测试:协议编解码、消息解析、cookie 解析与弹幕热度评分。"""

from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta

import brotli

from app.analysis.highlight import _danmaku_score, danmaku_rate_score
from app.db.models import Danmaku, DanmakuType, RecordingSession, SessionStatus, utcnow
from app.db.session import get_session
from app.sources.bilibili.client import parse_uid_from_cookie
from app.sources.bilibili.danmaku import (
    OP_HEARTBEAT_REPLY,
    OP_MESSAGE,
    decode,
    encode_packet,
    parse_message,
)

_HEADER = struct.Struct(">IHHII")


def _make_packet(operation: int, payload: bytes, protover: int = 1) -> bytes:
    """构造一个原始协议包(用于测试)。

    :param operation: 操作码。
    :param payload: 包体。
    :param protover: 协议版本。
    :returns: 完整数据包字节。
    """
    return _HEADER.pack(16 + len(payload), 16, protover, operation, 1) + payload


def test_encode_packet_header() -> None:
    """编码出的包头应正确反映长度、操作码与协议版本。"""
    pkt = encode_packet(OP_MESSAGE, b"hi", protover=1)
    plen, hlen, ver, op, _seq = _HEADER.unpack(pkt[:16])
    assert plen == 18
    assert hlen == 16
    assert ver == 1
    assert op == OP_MESSAGE
    assert pkt[16:] == b"hi"


def test_decode_plain_message() -> None:
    """未压缩(protover=0)的 op=5 包应被解析为 JSON。"""
    body = json.dumps({"cmd": "DANMU_MSG", "info": [0, "hello"]}).encode("utf-8")
    frame = _make_packet(OP_MESSAGE, body, protover=0)
    decoded = decode(frame)
    assert len(decoded) == 1
    op, parsed = decoded[0]
    assert op == OP_MESSAGE
    assert parsed["cmd"] == "DANMU_MSG"


def test_decode_brotli_aggregate() -> None:
    """brotli 聚合包(protover=3)应被解压并展开为多条消息。"""
    inner = b""
    for i in range(3):
        msg = json.dumps({"cmd": "DANMU_MSG", "info": [0, f"m{i}"]}).encode("utf-8")
        inner += _make_packet(OP_MESSAGE, msg, protover=0)
    outer = _make_packet(OP_MESSAGE, brotli.compress(inner), protover=3)
    decoded = decode(outer)
    assert len(decoded) == 3
    assert [p["info"][1] for _op, p in decoded] == ["m0", "m1", "m2"]


def test_decode_heartbeat_reply() -> None:
    """op=3 心跳回复应被解析为在线人气整数。"""
    frame = _make_packet(OP_HEARTBEAT_REPLY, (12345).to_bytes(4, "big"))
    decoded = decode(frame)
    assert decoded == [(OP_HEARTBEAT_REPLY, 12345)]


def test_parse_message_danmaku() -> None:
    """普通弹幕应解析出文本与用户。"""
    msg = {"cmd": "DANMU_MSG", "info": [0, "好厉害", [42, "观众甲"]]}
    result = parse_message(msg)
    assert result is not None
    msg_type, user, content, value = result
    assert msg_type == DanmakuType.DANMAKU
    assert user == "观众甲"
    assert content == "好厉害"
    assert value == 1.0


def test_parse_message_superchat() -> None:
    """SC 应解析出价格作为价值权重。"""
    msg = {
        "cmd": "SUPER_CHAT_MESSAGE",
        "data": {"message": "加油", "price": 30, "user_info": {"uname": "土豪"}},
    }
    result = parse_message(msg)
    assert result is not None
    msg_type, user, content, value = result
    assert msg_type == DanmakuType.SUPERCHAT
    assert user == "土豪"
    assert value == 30.0


def test_parse_message_unknown_ignored() -> None:
    """未知命令应返回 None 被忽略。"""
    assert parse_message({"cmd": "ONLINE_RANK_COUNT", "data": {}}) is None


def test_parse_uid_from_cookie() -> None:
    """应从 cookie 中提取 DedeUserID;缺失返回 0。"""
    assert parse_uid_from_cookie("SESSDATA=x; DedeUserID=98765; foo=1") == 98765
    assert parse_uid_from_cookie("SESSDATA=x") == 0
    assert parse_uid_from_cookie("") == 0


def test_danmaku_rate_score_pure() -> None:
    """窗口速率高于全场平均时分数更高;无数据返回 0。"""
    # 全场:600 强度 / 600 秒 = 1/s;窗口:30 强度 / 10 秒 = 3/s,ratio=3 -> 满分。
    assert danmaku_rate_score(30, 10, 600, 600) == 1.0
    # 与平均持平 -> 0。
    assert danmaku_rate_score(10, 10, 600, 600) == 0.0
    # 无弹幕数据。
    assert danmaku_rate_score(0, 10, 0, 0) == 0.0


def test_danmaku_score_db(temp_db: None) -> None:
    """落库的弹幕应能被窗口评分查询并给出非零热度。

    :param temp_db: 隔离数据库夹具。
    """
    base = utcnow()
    with get_session() as db:
        session = RecordingSession(room_id=1, status=SessionStatus.RECORDING)
        db.add(session)
        db.flush()
        sid = session.id
        # 全场 60 条均匀分布在 600 秒;在窗口 [base+100, base+110] 内集中 40 条(刷屏)。
        for i in range(60):
            db.add(Danmaku(session_id=sid, room_id=1, ts=base + timedelta(seconds=i * 10)))
        for _ in range(40):
            db.add(Danmaku(session_id=sid, room_id=1, ts=base + timedelta(seconds=105)))

    start = base + timedelta(seconds=100)
    end = base + timedelta(seconds=110)
    score = _danmaku_score(sid, start, end)
    assert score > 0.5


def test_danmaku_score_no_data(temp_db: None) -> None:
    """无弹幕数据时评分应为 0。

    :param temp_db: 隔离数据库夹具。
    """
    assert _danmaku_score(999, datetime.now(), datetime.now() + timedelta(seconds=10)) == 0.0
