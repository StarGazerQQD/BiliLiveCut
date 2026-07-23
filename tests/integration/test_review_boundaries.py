"""审片边界调整和版本化重渲染回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_candidate(tmp_path: Path) -> tuple[int, datetime]:
    """创建带完整录像覆盖范围的候选。"""
    from app.db.models import HighlightCandidate, LiveRoom, RawSegment, RecordingSession, SessionStatus
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
        )
        db.add(candidate)
        db.flush()
        candidate_id = candidate.id
    assert candidate_id is not None
    return candidate_id, base


def test_adjust_boundary_accepts_json_and_persists(temp_db: None, tmp_path: Path) -> None:
    """边界接口接收前端 JSON,校验后持久化并能在审片数据中恢复。"""
    from app.db.models import HighlightEvent
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
    """重渲染显式传递人工边界,不再依赖未提交的候选临时修改。"""
    from app.pipeline import orchestrator
    from app.web.main import app

    candidate_id, base = _seed_candidate(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_produce_clip(candidate_id_arg: int, auto_upload: bool = False, **kwargs: object) -> SimpleNamespace:
        calls.append({"candidate_id": candidate_id_arg, "auto_upload": auto_upload, **kwargs})
        return SimpleNamespace(id=77, file_path="clip-review.mp4", title="review")

    monkeypatch.setattr(orchestrator, "produce_clip", fake_produce_clip)
    with TestClient(app) as client:
        adjusted = client.post(
            f"/review/api/{candidate_id}/adjust",
            json={"adjust_s": -4, "side": "start"},
        )
        assert adjusted.status_code == 200
        response = client.post(f"/review/api/{candidate_id}/rerender")

    assert response.status_code == 200
    assert calls[0]["candidate_id"] == candidate_id
    assert calls[0]["auto_upload"] is False
    assert calls[0]["start_ts"] == (base + timedelta(seconds=6)).replace(tzinfo=None)
    assert calls[0]["end_ts"] == (base + timedelta(seconds=40)).replace(tzinfo=None)
    assert str(calls[0]["output_suffix"]).startswith("review-")
    assert response.json()["version"] == calls[0]["output_suffix"]


def test_validate_clip_boundary_rejects_recording_gap(temp_db: None, tmp_path: Path) -> None:
    """跨越真实录像缺口的剪辑边界必须被拒绝。"""
    from app.clipping.clipper import validate_clip_boundary
    from app.db.models import LiveRoom, RawSegment, RecordingSession
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
