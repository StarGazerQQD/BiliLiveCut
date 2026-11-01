"""解析类型边界和现有存储适配的回归。"""

from __future__ import annotations

import copy
import json
import zlib

import brotli
import pytest
from sqlmodel import select

from app.db.entities import Danmaku
from app.db.session import get_session
from app.sources.bilibili.danmaku import OP_MESSAGE, DanmakuClient, decode, encode_packet, parse_message
from app.sources.bilibili.messages import DanmakuMessage, GuardMessage, LiveMessage, parse_stream_message


def test_danmaku_positional_fields_are_typed_without_mutating_source() -> None:
    detail = [0] * 15 + [{"user": {"base": {"face": "https://example.invalid/face"}}}]
    raw = {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [
            detail,
            "测试正文",
            [42, "观众"],
            [7, "粉丝牌", "主播", 100, 0, 0, 0, 0, 0, 0, 0, 1, 99],
            [18],
            None,
            None,
            None,
            None,
            {"ts": 1700000000},
        ],
    }
    before = copy.deepcopy(raw)
    parsed = parse_stream_message(raw)
    assert isinstance(parsed, DanmakuMessage)
    assert parsed.content == "测试正文"
    assert parsed.user.uid == 42 and parsed.user.name == "观众" and parsed.user.level == 18
    assert parsed.user.face == "https://example.invalid/face"
    assert parsed.medal.name == "粉丝牌" and parsed.medal.anchor_uid == 99
    assert parsed.sent_at == 1700000000
    assert raw == before
    assert parse_message(raw) == ("danmaku", "观众", "测试正文", 1.0)


@pytest.mark.parametrize("optional", [None, {}, "not an array", [None, None, None]])
def test_bad_optional_user_or_medal_does_not_discard_valid_text(optional: object) -> None:
    raw = {"cmd": "DANMU_MSG", "info": [[], "保留正文", optional, optional]}
    result = parse_stream_message(raw)
    assert isinstance(result, DanmakuMessage) and result.content == "保留正文"
    assert result.user.name is None and result.medal is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"cmd": None},
        {"cmd": "DANMU_MSG_FAKE", "info": [[], "不是弹幕"]},
        {"cmd": "DANMU_MSG", "info": "bad"},
        {"cmd": "DANMU_MSG", "info": [[], {"bad": "content"}]},
        {"cmd": "SEND_GIFT", "data": {"total_coin": "not a number"}},
        {"cmd": "SEND_GIFT", "data": {"total_coin": float("nan")}},
        {"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": float("inf")}},
        {"cmd": "LIVE", "live_time": -1},
        {"cmd": "LIVE", "live_time": True},
        {"cmd": "LIVE", "live_time": {}},
    ],
)
def test_malformed_message_is_isolated(raw: object) -> None:
    assert parse_stream_message(raw) is None


@pytest.mark.parametrize("coin, expected", [(0, 1.0), (5000, 5.0), ("10000", 10.0)])
def test_gift_weight_and_currency_policy_are_unchanged(coin: object, expected: float) -> None:
    for currency in ("gold", "silver"):
        assert parse_message(
            {
                "cmd": "SEND_GIFT",
                "data": {"total_coin": coin, "coin_type": currency, "giftName": "礼物", "uname": "观众"},
            }
        ) == ("gift", "观众", "礼物", expected)


def test_control_and_guard_models_do_not_extend_database_retention() -> None:
    guard = {
        "cmd": "GUARD_BUY",
        "data": {"uid": 42, "username": "舰长", "guard_level": 3, "num": 1, "price": 198000, "gift_name": "舰长"},
    }
    assert isinstance(parse_stream_message(guard), GuardMessage)
    for raw in (guard, {"cmd": "LIVE", "live_time": 1700000000}, {"cmd": "PREPARING", "roomid": "100"}):
        assert parse_message(raw) is None
    parsed = parse_stream_message({"room_real_id": 100, "data": {"cmd": "LIVE", "live_time": 1700000000}})
    assert isinstance(parsed, LiveMessage) and parsed.live_time == 1700000000


def test_bad_message_does_not_lose_later_messages_or_change_archive_order(temp_db: None) -> None:
    from app.analysis.danmaku_sampling import DanmakuSampler
    from app.recording.danmaku_journal import drain_journal

    raw = [
        {"cmd": "SEND_GIFT", "data": {"total_coin": "bad"}},
        {"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": 30, "message": "保留留言", "user_info": {"uname": "观众"}}},
        {"cmd": "LIVE", "live_time": 1700000000},
    ]
    archived: list[dict] = []

    class Archive:
        def append(self, messages: list[dict], *, event_format: str) -> None:
            assert event_format == "raw"
            archived.extend(messages)

    client = DanmakuClient(100, 200, archive=Archive())
    client._sampler = DanmakuSampler()
    client._handle_frame(b"".join(encode_packet(OP_MESSAGE, json.dumps(item).encode()) for item in raw))
    assert drain_journal(200)
    assert archived == raw
    assert client._sampler._seq == 1
    with get_session() as db:
        rows = db.exec(select(Danmaku)).all()
        assert [(row.msg_type, row.user, row.content, row.value) for row in rows] == [
            ("superchat", "观众", "保留留言", 30.0)
        ]


@pytest.mark.parametrize("version, compress", [(2, zlib.compress), (3, brotli.compress)])
def test_compressed_and_broken_packets_do_not_lose_valid_neighbors(version: int, compress) -> None:
    raw = {"cmd": "PREPARING", "roomid": "100"}
    valid = encode_packet(OP_MESSAGE, json.dumps(raw).encode())
    packed = encode_packet(OP_MESSAGE, compress(valid), protover=version)
    broken = encode_packet(OP_MESSAGE, b"not compressed", protover=version)
    invalid_utf8 = encode_packet(OP_MESSAGE, b'{"cmd":"LIVE","extra":"\xff"}')
    assert decode(broken + invalid_utf8 + packed + valid) == [(OP_MESSAGE, raw), (OP_MESSAGE, raw)]


def test_deep_compression_nesting_is_bounded_and_preserves_outer_neighbors() -> None:
    raw = {"cmd": "LIVE", "live_time": 1700000000}
    valid = encode_packet(OP_MESSAGE, json.dumps(raw).encode())
    nested = valid
    for _ in range(12):
        nested = encode_packet(OP_MESSAGE, zlib.compress(nested), protover=2)
    assert decode(nested + valid) == [(OP_MESSAGE, raw)]
