"""房间采样配置经 API 保存后，实际影响弹幕入库而不影响原始档。"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import select

from app.analysis.danmaku_sampling import DanmakuSampler, DanmakuSamplingPolicy, session_sampling_policy
from app.analysis.room_config import load_room_config
from app.db.entities import Danmaku, DanmakuType, LiveRoom, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import SourceRoom
from app.recording.danmaku_archive import DanmakuArchive, archive_path
from app.sources.bilibili.danmaku import OP_MESSAGE, DanmakuClient, encode_packet


def _seed_room(number: int) -> tuple[int, int]:
    # 模拟已有数据库：保存完整旧配置，不包含本次新增的采样字段。
    config = load_room_config(None)
    config.pop("danmaku_sampling")
    config["hotwords"] = ["保留的词典"]
    with get_session() as db:
        room = LiveRoom(
            input_url=f"https://live.bilibili.com/{number}", room_id=number, room_config_json=json.dumps(config)
        )
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id)
        db.add(recording)
        db.flush()
        assert room.id is not None and recording.id is not None
        return room.id, recording.id


def _patch(client: TestClient, room_id: int, updates: object):
    return client.patch(f"/api/rooms/{room_id}", json={"room_config": {"danmaku_sampling": updates}})


def _frame(messages: list[dict]) -> bytes:
    return b"".join(encode_packet(OP_MESSAGE, json.dumps(message).encode()) for message in messages)


def test_old_rooms_use_defaults_and_nested_updates_are_independent(temp_db: None) -> None:
    from app.web.main import app

    room_id, session_id = _seed_room(22001)
    _, other_session_id = _seed_room(22002)
    defaults = DanmakuSamplingPolicy()
    assert session_sampling_policy(session_id) == defaults
    client = TestClient(app)
    assert _patch(client, room_id, {"danmaku_rate": 0.29}).status_code == 200
    assert _patch(client, room_id, {"gift_rate": 1}).status_code == 200
    policy = session_sampling_policy(session_id)
    assert policy.danmaku_rate == 0.29 and policy.gift_rate == 1
    assert policy.high_density_rate == defaults.high_density_rate
    assert session_sampling_policy(other_session_id) == defaults
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        assert load_room_config(room)["hotwords"] == ["保留的词典"]
    # 保存词典时不能重置刚保存的采样规则。
    assert client.patch(f"/api/rooms/{room_id}", json={"room_config": {"hotwords": ["新词"]}}).status_code == 200
    assert session_sampling_policy(session_id) == policy


@pytest.mark.parametrize(
    "updates",
    [
        {"danmaku_rate": -0.01},
        {"gift_rate": 1.01},
        {"superchat_rate": 0.333},
        {"interact_rate": True},
        {"danmaku_rate": "0.3"},
        {"high_density_enabled": 1},
        {"high_density_threshold": 0},
        {"high_density_threshold": 1.5},
        {"high_density_threshold": True},
        {"high_density_rate": 2},
        {"unknown_rule": 1},
        [],
        None,
    ],
)
def test_invalid_policies_are_rejected_atomically(temp_db: None, updates: object) -> None:
    from app.web.main import app

    room_id, session_id = _seed_room(23001)
    response = _patch(TestClient(app), room_id, updates)
    assert response.status_code == 400, response.text
    assert session_sampling_policy(session_id) == DanmakuSamplingPolicy()


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_rates_are_rejected(rate: float) -> None:
    with pytest.raises(ValidationError):
        DanmakuSamplingPolicy(gift_rate=rate)


def test_active_collectors_refresh_only_their_room_and_archive_every_message(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.recording.danmaku_journal import drain_journal
    from app.sources.bilibili import danmaku
    from app.web.main import app
    from app.web.services.rooms import recorder_manager

    clock = [0.0]
    monkeypatch.setattr(danmaku, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(recorder_manager, "is_running", lambda _: True)
    room_id, session_id = _seed_room(24001)
    other_room_id, other_session_id = _seed_room(24002)
    client = TestClient(app)
    stop_policy = {
        "danmaku_rate": 0,
        "gift_rate": 0,
        "superchat_rate": 0,
        "interact_rate": 0,
        "high_density_enabled": False,
    }
    assert _patch(client, room_id, stop_policy).status_code == 200
    assert _patch(client, other_room_id, {"danmaku_rate": 1, "high_density_enabled": False}).status_code == 200
    source_room = SourceRoom(platform="bilibili", source_id="24001", canonical_url="https://live.bilibili.com/24001")
    collector = DanmakuClient(
        24001,
        session_id,
        archive=DanmakuArchive(session_id, source_room),
        sampling_policy=lambda: session_sampling_policy(session_id),
    )
    other_collector = DanmakuClient(
        24002, other_session_id, sampling_policy=lambda: session_sampling_policy(other_session_id)
    )
    # 独立实例避免全局房间采样器污染其他测试，也验证各实例加载自己的房间配置。
    collector._sampler = DanmakuSampler()
    other_collector._sampler = DanmakuSampler()
    messages = [
        {"cmd": "DANMU_MSG", "info": [[], "普通弹幕", [42, "观众"]]},
        {"cmd": "SEND_GIFT", "data": {"total_coin": 5000, "giftName": "礼物", "uname": "观众"}},
        {"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": 30, "message": "醒目留言"}},
        {"cmd": "INTERACT_WORD", "data": {"uname": "观众", "msg_type": 1}},
    ]
    collector._handle_frame(_frame(messages))
    other_collector._handle_frame(_frame([messages[0]]))
    assert drain_journal(other_session_id)
    with get_session() as db:
        assert [row.session_id for row in db.exec(select(Danmaku)).all()] == [other_session_id]
    assert _patch(client, room_id, {key: 1 for key in stop_policy if key.endswith("_rate")}).status_code == 200
    clock[0] = 4
    collector._handle_frame(_frame(messages))  # 缓存尚未到刷新时间。
    with get_session() as db:
        assert len(db.exec(select(Danmaku)).all()) == 1
    clock[0] = 5
    control = [
        {
            "cmd": "GUARD_BUY",
            "data": {"uid": 42, "username": "舰长", "guard_level": 3, "num": 1, "price": 198000, "gift_name": "舰长"},
        },
        {"cmd": "UNKNOWN_EVENT", "data": {"text": "原始消息"}},
    ]
    collector._handle_frame(_frame(messages + control))
    assert drain_journal(session_id)
    with get_session() as db:
        rows = db.exec(select(Danmaku).where(Danmaku.session_id == session_id)).all()
        assert [row.msg_type for row in rows] == ["danmaku", "gift", "superchat", "interact"]
    raw = [json.loads(line) for line in archive_path(session_id).read_text(encoding="utf-8").splitlines()]
    assert [record["payload"] for record in raw if record["kind"] == "raw"] == messages * 3 + control
    assert session_sampling_policy(other_session_id).gift_rate == 0.8


@pytest.mark.parametrize(
    "kind, rate",
    [
        (DanmakuType.DANMAKU, 0.30),
        (DanmakuType.GIFT, 0.80),
        (DanmakuType.SUPERCHAT, 1),
        (DanmakuType.INTERACT, 1),
    ],
)
def test_default_sampling_keeps_existing_percentages(kind: DanmakuType, rate: float) -> None:
    sampler = DanmakuSampler()
    assert sum(sampler.should_keep(kind) for _ in range(100)) == round(rate * 100)


def test_one_percent_precision_and_density_override() -> None:
    sampler = DanmakuSampler(policy=DanmakuSamplingPolicy(danmaku_rate=0.29, high_density_rate=0.07))
    assert sum(sampler.should_keep(DanmakuType.DANMAKU) for _ in range(100)) == 29
    sampler._timestamps.extend([time.time()] * 100)
    assert sum(sampler.should_keep(DanmakuType.DANMAKU) for _ in range(100)) == 7
    sampler.policy = sampler.policy.model_copy(update={"high_density_enabled": False})
    assert sum(sampler.should_keep(DanmakuType.DANMAKU) for _ in range(100)) == 29
    # 原有默认高密度保留率仍然是 10%。
    sampler.policy = DanmakuSamplingPolicy()
    assert sum(sampler.should_keep(DanmakuType.DANMAKU) for _ in range(100)) == 10
