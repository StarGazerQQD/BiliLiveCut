"""审片边界调整和版本化重渲染回归测试。"""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.core.paths import resolve_storage_path  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_candidate(tmp_path: Path) -> tuple[int, datetime]:
    """创建带完整录像覆盖范围的候选。"""
    from app.db.entities import (
        HighlightCandidate,
        HighlightEvent,
        LiveRoom,
        RawSegment,
        RecordingSession,
        SessionStatus,
    )
    from app.db.session import get_session

    base = datetime.now(UTC).replace(microsecond=0)
    media_path = tmp_path / "recording.ts"
    media_path.touch()
    with get_session() as db:
        room = LiveRoom(input_url="test", room_id=100, authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(
            room_id=room.id,
            status=SessionStatus.STOPPED,
            ended_at=base + timedelta(seconds=100),
        )
        db.add(session)
        db.flush()
        db.add(
            RawSegment(
                session_id=session.id,
                seq=0,
                file_path=str(media_path),
                start_ts=base,
                end_ts=base + timedelta(seconds=100),
                duration_s=100,
            )
        )
        candidate = HighlightCandidate(
            session_id=session.id,
            peak_ts=base + timedelta(seconds=25),
            start_ts=base + timedelta(seconds=10),
            end_ts=base + timedelta(seconds=40),
            highlight_score=0.9,
            dedup_hash=f"review-boundary-{session.id}",
        )
        db.add(candidate)
        db.flush()
        candidate_id = candidate.id
        assert candidate_id is not None
        db.add(
            HighlightEvent(
                candidate_id=candidate_id,
                session_id=session.id,
                raw_start_ts=candidate.start_ts,
                raw_end_ts=candidate.end_ts,
            )
        )
    return candidate_id, base


def test_adjust_boundary_accepts_json_and_persists(temp_db: None, tmp_path: Path) -> None:
    """边界接口接收前端 JSON,校验后持久化并能在审片数据中恢复。"""
    from app.db.entities import HighlightEvent
    from app.db.session import get_session
    from app.web.main import app

    candidate_id, base = _seed_candidate(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            f"/review/api/{candidate_id}/adjust",
            json={"adjust_s": -5, "side": "start"},
        )
        assert response.status_code == 200
        expected_start = (base + timedelta(seconds=5)).replace(tzinfo=None)
        assert response.json()["adjusted_start_ts"] == expected_start.isoformat()
        assert response.json()["duration_s"] == 35

        review_data = client.get(f"/review/api/{candidate_id}").json()
        assert review_data["boundary"]["adjusted_start_ts"] == expected_start.isoformat()

    with get_session() as db:
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).one()
        assert event.adjusted_start_ts == expected_start


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"adjust_s": 31, "side": "start"}, "起点必须早于终点"),
        ({"adjust_s": 61, "side": "end"}, "终点晚于现有录像范围"),
    ],
)
def test_adjust_boundary_rejects_invalid_range(
    temp_db: None,
    tmp_path: Path,
    payload: dict[str, object],
    message: str,
) -> None:
    """交叉边界和越过录像范围的调整不会落库。"""
    from app.web.main import app

    candidate_id, _ = _seed_candidate(tmp_path)
    with TestClient(app) as client:
        response = client.post(f"/review/api/{candidate_id}/adjust", json=payload)
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_adjust_boundary_rejects_unknown_side(temp_db: None, tmp_path: Path) -> None:
    """未知边界类型由请求模型直接拒绝。"""
    from app.web.main import app

    candidate_id, _ = _seed_candidate(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            f"/review/api/{candidate_id}/adjust",
            json={"adjust_s": 1, "side": "middle"},
        )
    assert response.status_code == 422


def test_rerender_uses_committed_adjusted_boundary_and_versioned_path(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """通过真实 HTTP 作业及 FFmpeg 核验已提交边界与独立版本。"""
    from app.core.config import settings
    from app.db.entities import FinalClip, UploadTask
    from app.db.session import get_session
    from app.web.main import app

    candidate_id, base = _seed_candidate(tmp_path)
    monkeypatch.setattr(settings, "clip_loudnorm", False)
    monkeypatch.setattr(settings, "clip_remove_silence", False)
    monkeypatch.setattr(settings, "clip_preset", "ultrafast")
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "100",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(tmp_path / "recording.ts"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with TestClient(app) as client:
        adjusted = client.post(
            f"/review/api/{candidate_id}/adjust",
            json={"adjust_s": -4, "side": "start"},
        )
        assert adjusted.status_code == 200
        response = client.post(f"/review/api/{candidate_id}/rerender")
        assert response.status_code == 200
        accepted = response.json()
        assert accepted["status"] == "accepted" and accepted["job"]["type"] == "review_rerender"
        payload = accepted["job"]["payload"]
        assert payload["candidate_id"] == candidate_id
        assert datetime.fromisoformat(payload["start_ts"]) == (base + timedelta(seconds=6)).replace(tzinfo=None)
        assert datetime.fromisoformat(payload["end_ts"]) == (base + timedelta(seconds=40)).replace(tzinfo=None)
        deadline = time.monotonic() + 60
        while True:
            status = client.get(f"/api/jobs/{accepted['job']['id']}")
            assert status.status_code == 200
            state = status.json()
            if state["status"] in {"succeeded", "failed", "cancelled"}:
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.05)
        assert state["status"] == "succeeded", state
        assert state["result"]["version"] == accepted["version"]
        assert accepted["version"].startswith("review-")
        with get_session() as db:
            clip = db.get(FinalClip, state["result"]["clip_id"])
            assert clip.candidate_id == candidate_id
            assert clip.duration_s == pytest.approx(34, abs=0.3)
            assert resolve_storage_path(clip.file_path).stat().st_size > 0
            assert f"job_{state['id']}_{state['attempt']}_" in Path(clip.file_path).stem
            assert db.exec(select(UploadTask)).all() == []


def test_validate_clip_boundary_rejects_recording_gap(temp_db: None, tmp_path: Path) -> None:
    """跨越真实录像缺口的剪辑边界必须被拒绝。"""
    from app.clipping.core import validate_clip_boundary
    from app.db.entities import LiveRoom, RawSegment, RecordingSession
    from app.db.session import get_session

    base = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room = LiveRoom(input_url="gap", room_id=101, authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        for seq, start, end in ((0, 0, 10), (1, 15, 30)):
            path = tmp_path / f"gap-{seq}.ts"
            path.touch()
            db.add(
                RawSegment(
                    session_id=session.id,
                    seq=seq,
                    file_path=str(path),
                    start_ts=base + timedelta(seconds=start),
                    end_ts=base + timedelta(seconds=end),
                    duration_s=end - start,
                )
            )
        session_id = session.id
    assert session_id is not None

    with pytest.raises(ValueError, match="录像缺口"):
        validate_clip_boundary(
            session_id,
            base + timedelta(seconds=5),
            base + timedelta(seconds=20),
            max_duration_s=180,
        )
