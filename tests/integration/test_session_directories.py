from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.recording_storage import RecordingStorage
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession
from app.db.session import get_session
from app.recording.session_storage import JOURNAL_COLLISION, LOCATION_PREFIX, OWNER_FILE
from app.web.routers.segments import router
from app.web.services import session_directories as directories

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _client(layout: RecordingStorage | None = None, *, local: str | None = None) -> TestClient:
    with get_session() as db:
        db.add(LiveRoom(id=2, input_url="fixture", platform="local" if local else "bilibili"))
        db.flush()
        db.add(RecordingSession(id=9, room_id=2))
        if layout:
            db.add(AppSetting(key=f"{LOCATION_PREFIX}9", value=layout.model_dump_json()))
        if local:
            db.add(AppSetting(key="local_source:9", value=json.dumps({"import_id": local, "has_comments": True})))
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_rebuilt_session_opens_separate_original_numbered_directories(
    temp_db: None, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    raw, chat = tmp_path / "video", tmp_path / "comments"
    roots = [p / "session_14" for p in (raw, chat)]
    for path in roots:
        path.mkdir(parents=True)
    calls = []

    def open_directory(path: str, *, existing_directory: bool) -> bool:
        assert existing_directory
        calls.append(path)
        return True

    monkeypatch.setattr(directories, "open_path", open_directory)
    endpoint = "/api/sessions/9/open-storage-directory"
    with _client(RecordingStorage(raw=str(raw), danmaku=str(chat), source_session_id=14)) as client:
        response = client.post(endpoint)
        assert response.status_code == 200
        assert [item["path"] for item in response.json()["directories"]] == list(map(str, roots))
        assert not response.json()["opened"] and not calls
        assert client.post(endpoint, params={"kind": "danmaku"}).json()["opened"]
        assert calls == [str(roots[1])]
        assert client.post(endpoint, params={"kind": str(tmp_path)}).status_code == 422
        assert client.post("/api/sessions/99/open-storage-directory").status_code == 404
        (roots[0] / OWNER_FILE).write_text("a" * 32 + "\n", encoding="ascii", newline="\n")
        assert client.post(endpoint, params={"kind": "raw"}).status_code == 409
        assert calls == [str(roots[1])]


def test_shared_location_and_unavailable_volume_do_not_open_parent(
    temp_db: None, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    root = tmp_path / "volume"
    path = root / "session_9"
    path.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(directories, "open_path", lambda target, *, existing_directory: calls.append(target) or False)
    endpoint = "/api/sessions/9/open-storage-directory"
    with _client(RecordingStorage(raw=str(root), danmaku=str(root))) as client:
        result = client.post(endpoint).json()
        assert result["directories"] == [{"kind": "raw", "label": "原片、弹幕", "path": str(path), "available": True}]
        assert result["selected"] == "raw" and not result["opened"]
        path.rmdir()
        result = client.post(endpoint).json()
        assert not result["directories"][0]["available"] and result["selected"] is None
        assert calls == [str(path)] and not path.exists()


@pytest.mark.parametrize("old_layout", [False, True])
def test_local_import_uses_uuid_media_and_comment_directories(
    temp_db: None, tmp_path: Path, monkeypatch: MonkeyPatch, old_layout: bool
) -> None:
    uid = "c" * 32
    raw = Path(settings.storage_root) / "raw" if old_layout else tmp_path / "raw"
    chat = raw if old_layout else tmp_path / "chat"
    media = raw / "imports" / uid / "segments"
    comments = chat / "imports" / uid
    media.mkdir(parents=True)
    comments.mkdir(parents=True, exist_ok=True)
    layout = None if old_layout else RecordingStorage(raw=str(raw), danmaku=str(chat), import_id=uid)
    with _client(layout, local=uid) as client:
        monkeypatch.setattr(directories, "open_path", lambda *args, **kwargs: False)
        result = client.post("/api/sessions/9/open-storage-directory").json()
        assert [item["path"] for item in result["directories"]] == [str(media), str(comments)]


def test_legacy_shared_raw_root_is_returned_for_a_registered_flat_file(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    raw = Path(settings.storage_root) / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(directories, "open_path", lambda *args, **kwargs: False)
    with _client() as client:
        with get_session() as db:
            db.add(RawSegment(session_id=9, seq=0, file_path="raw/old.ts"))
        result = client.post("/api/sessions/9/open-storage-directory").json()
        assert result["selected"] == "legacy_raw" and result["directories"][0]["path"] == str(raw)


def test_known_legacy_collision_never_opens_or_exposes_original_directory(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    raw = Path(settings.storage_root) / "raw" / "session_9"
    raw.mkdir(parents=True)
    monkeypatch.setattr(directories, "open_path", lambda *args, **kwargs: pytest.fail("打开了原场次目录"))
    with _client() as client:
        with get_session() as db:
            session = db.get(RecordingSession, 9)
            assert session is not None
            session.error_message = f"录制失败：RuntimeError: {JOURNAL_COLLISION}"
        for params in ({}, {"kind": "raw"}, {"kind": "danmaku"}):
            response = client.post("/api/sessions/9/open-storage-directory", params=params)
            assert response.status_code == 409 and "旧弹幕档案冲突" in response.json()["detail"]
            assert str(raw) not in response.json()["detail"]
