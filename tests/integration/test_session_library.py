from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.paths import clips_dir, ready_to_upload_dir, session_clips_dir
from app.db.entities import (
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    HighlightTopic,
    LiveRoom,
    RawSegment,
    RecordingSession,
    Topic,
    Transcript,
)
from app.db.session import get_session


def add_session(db: Session, index: int) -> int:
    room = LiveRoom(input_url=f"local:{index}", platform="local", title=f"录播 {index}", authorized=True)
    db.add(room)
    db.flush()
    session = RecordingSession(room_id=room.id, started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index))
    db.add(session)
    db.flush()
    assert session.id is not None
    return session.id


def add_items(db: Session, session_id: int, seq: int, *, auxiliary: str | None = None) -> tuple[int, int, int]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    segment = RawSegment(session_id=session_id, seq=seq, file_path=f"session_{session_id}/part_{seq}.ts")
    db.add(segment)
    db.flush()
    transcript = Transcript(segment_id=segment.id, final_text=f"原文 {seq}", auxiliary_json=auxiliary, created_at=now)
    candidate = HighlightCandidate(
        session_id=session_id,
        peak_ts=now,
        start_ts=now,
        end_ts=now + timedelta(seconds=1),
        dedup_hash=f"library-{session_id}-{seq}",
        created_at=now,
    )
    db.add(transcript)
    db.add(candidate)
    db.flush()
    clip = FinalClip(
        candidate_id=candidate.id, file_path=f"legacy_{session_id}_{seq}.mp4", title=f"成品 {seq}", created_at=now
    )
    db.add(clip)
    db.flush()
    assert transcript.id is not None and clip.id is not None and candidate.id is not None
    return transcript.id, clip.id, candidate.id


def test_library_pages_cover_more_than_200_records_without_renumbering(temp_db: None) -> None:
    from app.web.main import app

    with get_session() as db:
        old, recent = add_session(db, 1), add_session(db, 2)
        ids = [add_items(db, old, seq) for seq in range(215)]
        new_ids = add_items(db, recent, 0)
        rejected = db.get(FinalClip, ids[1][1])
        rejected.status = "rejected"
        db.add(rejected)
    with TestClient(app) as client:
        transcripts, clips = [], []
        for offset in range(0, 215, 30):
            transcripts.extend(client.get(f"/api/transcripts?session_id={old}&limit=30&offset={offset}").json())
            clips.extend(client.get(f"/api/clips?session_id={old}&limit=30&offset={offset}").json())
        assert [item["id"] for item in transcripts] == [row[0] for row in reversed(ids)]
        assert [item["segment_seq"] for item in transcripts] == list(reversed(range(215)))
        assert [item["id"] for item in clips] == [row[1] for row in reversed(ids) if row != ids[1]]
        assert [item["display_number"] for item in clips] == [n for n in reversed(range(1, 216)) if n != 2]
        new_transcript = client.get(f"/api/transcripts?session_id={recent}").json()[0]
        new_clip = client.get(f"/api/clips?session_id={recent}").json()[0]
        assert new_transcript["id"] == new_ids[0] and new_transcript["segment_seq"] == 0
        assert new_clip["id"] == new_ids[1] and new_clip["display_number"] == 1


def test_search_filters_actual_refined_text_and_status_before_paging(temp_db: None) -> None:
    from app.web.main import app

    refined = {
        "transcript_refinement": {"version": 1, "applied": True, "clean_text": "目标正文", "summary": "隐藏摘要"}
    }
    with get_session() as db:
        sid = add_session(db, 1)
        first, clip_id, _ = add_items(db, sid, 0, auxiliary=json.dumps(refined))
        clip = db.get(FinalClip, clip_id)
        clip.status, clip.description = "ready", "目标简介"
        db.add(clip)
        for index in range(1, 206):
            add_items(db, sid, index)
        add_items(db, sid, 206, auxiliary='{"transcript_refinement":{"clean_text":"目标正文","applied":false}}')
        add_items(db, sid, 207, auxiliary="{broken-json")
    with TestClient(app) as client:
        result = client.get("/api/transcripts", params={"session_id": sid, "q": "目标正文", "limit": 1}).json()
        assert [item["id"] for item in result] == [first]
        assert client.get("/api/transcripts", params={"q": "隐藏摘要"}).json() == []
        assert client.get("/api/transcripts", params={"q": "原文 0"}).json() == []
        assert client.get("/api/transcripts", params={"q": "目标正文", "offset": 1}).json() == []
        grouped = client.get("/api/sessions/history", params={"kind": "transcripts", "q": "目标正文"}).json()
        assert len(grouped) == 1 and grouped[0]["matching_count"] == 1
        clips = client.get(
            "/api/clips", params={"session_id": sid, "status": "ready", "q": "目标简介", "limit": 1}
        ).json()
        assert len(clips) == 1 and clips[0]["id"] == clip_id and clips[0]["display_number"] == 1


def test_session_cards_paginate_and_preserve_all_orphan_cases(temp_db: None) -> None:
    from app.web.main import app

    with get_session() as db:
        sessions = []
        for index in range(15):
            sid = add_session(db, index)
            sessions.append(sid)
            add_items(db, sid, 0)
        orphan_transcript, orphan_clip, _ = add_items(db, 999, 0)
        missing_segment = Transcript(segment_id=99999, final_text="缺少片段")
        missing_candidate = FinalClip(candidate_id=99999, file_path="orphan.mp4")
        db.add(missing_segment)
        db.add(missing_candidate)
        db.flush()
        missing_ids = missing_segment.id, missing_candidate.id
    with TestClient(app) as client:
        assert [item["session_id"] for item in client.get("/api/sessions/history").json()] == list(reversed(sessions))
        for kind, orphan_id, missing_id in [
            ("transcripts", orphan_transcript, missing_ids[0]),
            ("clips", orphan_clip, missing_ids[1]),
        ]:
            first = client.get("/api/sessions/history", params={"kind": kind, "limit": 12}).json()
            second = client.get("/api/sessions/history", params={"kind": kind, "limit": 12, "offset": 12}).json()
            assert [item["session_id"] for item in first + second] == list(reversed(sessions)) + [None]
            assert second[-1]["matching_count"] == 2
            unknown = client.get(f"/api/{kind}", params={"unknown_session": True}).json()
            assert {item["id"] for item in unknown} == {orphan_id, missing_id}
            assert all(item["session_id"] is None for item in unknown)
            assert (
                client.get(f"/api/{kind}", params={"unknown_session": True, "session_id": sessions[0]}).status_code
                == 422
            )
            assert client.get(f"/api/{kind}?offset=-1").status_code == 422


def test_session_directories_reject_invalid_ids_and_link_redirection(temp_db: None, tmp_path: Path) -> None:
    for value in (0, -1, True, "../outside"):
        with pytest.raises(ValueError):
            session_clips_dir(value)
    root = clips_dir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "session_99"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="重定向"):
            session_clips_dir(99)
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_upload_manifests_group_by_session_and_old_media_stays_accessible(temp_db: None) -> None:
    from app.publishing.copywriter import export_manifest
    from app.web.main import app

    legacy = clips_dir() / "old-flat.mp4"
    legacy.write_bytes(b"legacy video")
    old_manifest = ready_to_upload_dir() / "old-flat.json"
    old_manifest.write_text("historical", encoding="utf-8")
    with get_session() as db:
        sid = add_session(db, 1)
        _, cid, _ = add_items(db, sid, 0)
        clip = db.get(FinalClip, cid)
        clip.file_path = str(legacy)
        db.add(clip)
    first = Path(export_manifest(cid))
    content = first.read_bytes()
    assert first.parent == ready_to_upload_dir() / f"session_{sid}"
    assert json.loads(content)["session_id"] == sid
    assert Path(export_manifest(cid)).read_bytes() == content
    assert old_manifest.read_text(encoding="utf-8") == "historical"
    with TestClient(app) as client:
        response = client.get(f"/api/clips/{cid}/video")
        assert response.status_code == 200 and response.content == b"legacy video"
    assert legacy.read_bytes() == b"legacy video"


def test_collection_rejects_cross_session_members_before_rendering(temp_db: None) -> None:
    from app.pipeline.collection import render_collection

    with get_session() as db:
        first, second = add_session(db, 1), add_session(db, 2)
        topic = Topic(session_id=first, title="同场主题")
        db.add(topic)
        db.flush()
        event_ids = []
        for sid in (second, first):
            _, _, candidate_id = add_items(db, sid, 0)
            event = HighlightEvent(candidate_id=candidate_id, session_id=sid)
            db.add(event)
            db.flush()
            db.add(HighlightTopic(topic_id=topic.id, event_id=event.id))
            event_ids.append(event.id)
        topic_id = topic.id
    with pytest.raises(ValueError, match="同一场次"):
        render_collection(topic_id, event_ids)
    assert not list(clips_dir().rglob("collection_*.mp4"))


def test_open_directory_uses_validated_session_scope(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web.main import app
    from app.web.services import clips

    with get_session() as db:
        sid = add_session(db, 1)
    opened: list[str] = []
    monkeypatch.setattr(clips, "open_path", opened.append)
    with TestClient(app) as client:
        for query in ("", f"?session_id={sid}", "?unknown_session=true"):
            assert client.post(f"/api/open-clips-dir{query}").status_code == 200
        assert client.post("/api/open-clips-dir?session_id=999999").status_code == 422
        assert client.post(f"/api/open-clips-dir?session_id={sid}&unknown_session=true").status_code == 422
    assert [Path(path) for path in opened] == [clips_dir(), session_clips_dir(sid), session_clips_dir(None)]
