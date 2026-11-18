"""Per-session archives preserve received messages independently of sampled analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from sqlmodel import select

from app.analysis.source_policy import session_danmaku_view
from app.core.config import settings
from app.db.entities import Danmaku, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import DanmakuEvent, SourceRoom
from app.recording.danmaku import DanmakuCapture
from app.recording.danmaku_archive import DanmakuArchive, archive_path, archive_view, open_archive_snapshot
from app.sources.bilibili.danmaku import OP_AUTH_REPLY, OP_MESSAGE, DanmakuClient, encode_packet
from app.sources.bilibili.source import BilibiliSource


def _room() -> SourceRoom:
    return SourceRoom(platform="bilibili", source_id="123", canonical_url="https://live.bilibili.com/123")


def _records(session_id: int) -> list[dict[str, object]]:
    return [json.loads(line) for line in archive_path(session_id).read_text(encoding="utf-8").splitlines()]


def test_archives_append_unicode_and_keep_sessions_separate(temp_db: None) -> None:
    archive = DanmakuArchive(1, _room())
    archive.start()
    archive.start()
    archive.append([{"text": "中文\n下一行"}], event_format="raw")
    DanmakuArchive(1, _room()).append([{"text": "重连后"}], event_format="raw")
    DanmakuArchive(2, _room()).start()
    records = _records(1)
    assert [record["kind"] for record in records] == ["session", "raw", "session", "raw"]
    assert records[1]["payload"] == {"text": "中文\n下一行"}
    assert all(record["session_id"] == 1 and record["platform"] == "bilibili" for record in records)
    assert len(_records(2)) == 1
    assert archive_view(1) == {"available": True}
    assert archive_view(3) == {"available": False}
    assert not archive_path(3).parent.exists()


def test_raw_archive_precedes_sampling_and_excludes_auth_packets(temp_db: None) -> None:
    archive = DanmakuArchive(1, _room())
    client = DanmakuClient(room_id=123, session_id=1, cookie="SESSDATA=private-cookie", archive=archive)
    client._sampler = SimpleNamespace(record=lambda: None, should_keep=lambda _: False)
    messages = [
        {"cmd": "DANMU_MSG", "info": [[], "保留的正文", [1, "用户"]]},
        {"cmd": "UNKNOWN_EVENT", "data": {"text": "未解析消息也保留"}},
    ]
    frame = encode_packet(OP_AUTH_REPLY, b'{"code":0,"token":"private-auth-token"}')
    frame += b"".join(encode_packet(OP_MESSAGE, json.dumps(message).encode()) for message in messages)
    client._handle_frame(frame)
    assert [record["payload"] for record in _records(1)[1:]] == messages
    text = archive_path(1).read_text(encoding="utf-8")
    assert "private-cookie" not in text and "private-auth-token" not in text
    with get_session() as db:
        assert not db.exec(select(Danmaku)).all()


async def test_normalized_event_archive_keeps_public_count_semantics(temp_db: None) -> None:
    capture = DanmakuCapture(BilibiliSource(), _room(), 3, 1)
    event = DanmakuEvent(occurred_at=datetime.now(UTC), content="插件弹幕", user_name="用户")
    await capture._emit(event)
    await capture.stop()
    assert _records(1)[1]["kind"] == "normalized"
    assert _records(1)[1]["payload"] == event.model_dump(mode="json")
    with get_session() as db:
        row = db.exec(select(Danmaku)).one()
        assert row.content == event.content and row.value == 1.0
    assert session_danmaku_view(1)["archive_error"] is None


async def test_disabled_capture_does_not_create_archive(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "collect_danmaku", False)
    capture = DanmakuCapture(BilibiliSource(), _room(), 3, 1)
    await capture.start()
    await capture.stop()
    assert not archive_path(1).exists()
    assert session_danmaku_view(1)["state"] == "disabled"


async def test_archive_failure_reports_once_and_keeps_database_capture(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = Path.open

    def unavailable(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path.name == "danmaku.jsonl" and mode == "a+b":
            raise PermissionError("simulated read-only archive")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unavailable)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        capture = DanmakuCapture(BilibiliSource(), _room(), 3, 1)
        for text in ["private-message-1", "private-message-2"]:
            await capture._emit(DanmakuEvent(occurred_at=datetime.now(UTC), content=text))
        await capture.stop()
    finally:
        logger.remove(sink)
    with get_session() as db:
        assert len(db.exec(select(Danmaku)).all()) == 2
    assert "PermissionError" in str(session_danmaku_view(1)["archive_error"])
    assert sum("danmaku_archive_failed:" in message for message in messages) == 1
    assert not any("private-message-" in message for message in messages)


@pytest.mark.parametrize("partial", [b"", b'{"partial":', b"x" * 70000], ids=["complete", "partial", "large-partial"])
def test_snapshot_excludes_partial_last_line_and_later_appends(temp_db: None, partial: bytes) -> None:
    archive = DanmakuArchive(1, _room())
    archive.append([{"text": "中文"}], event_format="raw")
    path = archive_path(1)
    complete = path.read_bytes()
    with path.open("ab") as stream:
        stream.write(partial)
    reader, size = open_archive_snapshot(1)
    try:
        with path.open("ab") as stream:
            stream.write(b"{}\n")
        assert size == len(complete)
        assert reader.read(size) == complete
    finally:
        reader.close()


def test_archive_path_rejects_redirects_even_inside_storage(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(settings.storage_root).resolve()
    original_resolve = Path.resolve

    def redirected(path: Path, *args: object, **kwargs: object) -> Path:
        if path.name == "danmaku.jsonl":
            return root / "blc.db"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError, match="不能重定向"):
        archive_path(1)
    assert archive_view(1) == {"available": False}


def test_download_and_overview_expose_only_existing_session_archives(temp_db: None) -> None:
    from app.web.main import app

    with get_session() as db:
        db.add_all([RecordingSession(id=1, room_id=3), RecordingSession(id=2, room_id=3)])
    archive = DanmakuArchive(1, _room())
    archive.append([{"text": "下载中文"}], event_format="raw")
    content = archive_path(1).read_bytes()
    with archive_path(1).open("ab") as stream:
        stream.write(b'{"unfinished":')
    with TestClient(app) as client:
        response = client.get("/api/sessions/1/danmaku-archive")
        assert response.status_code == 200
        assert response.content == content
        assert int(response.headers["content-length"]) == len(content)
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert "danmaku-session-1.jsonl" in response.headers["content-disposition"]
        assert client.get("/api/sessions/2/danmaku-archive").status_code == 404
        assert client.get("/api/sessions/3/danmaku-archive").status_code == 404
        sessions = client.get("/api/danmaku").json()["sessions"]
        assert next(item for item in sessions if item["session_id"] == 1)["archive"]["available"]
        assert not next(item for item in sessions if item["session_id"] == 2)["archive"]["available"]
