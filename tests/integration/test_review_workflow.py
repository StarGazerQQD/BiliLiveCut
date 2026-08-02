"""多人审核队列、权限、草稿、盲审、撤销和审计回归测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import select  # noqa: E402

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_candidate() -> int:
    from app.db.models import HighlightCandidate, LiveRoom, RecordingSession, SessionStatus
    from app.db.session import get_session

    now = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room = LiveRoom(input_url="review", room_id=200, authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status=SessionStatus.STOPPED, ended_at=now)
        db.add(session)
        db.flush()
        candidate = HighlightCandidate(
            session_id=session.id,
            peak_ts=now,
            start_ts=now - timedelta(seconds=20),
            end_ts=now + timedelta(seconds=20),
            rule_score=0.8,
            llm_score=0.9,
            highlight_score=0.85,
            reason="model reason",
        )
        db.add(candidate)
        db.flush()
        candidate_id = candidate.id
    assert candidate_id is not None
    return candidate_id


@pytest.fixture()
def review_client(temp_db: None, monkeypatch: MonkeyPatch) -> Iterator[TestClient]:
    """启用管理员和两个审核员账号。"""
    from app.web import main
    from app.web.services import notifications

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setattr(main, "_REVIEWER_PASSWORDS", {"alice": "alice-pass", "bob": "bob-pass"})
    main._rate_buckets.clear()
    notifications._NOTIFICATIONS.clear()
    yield TestClient(main.app)
    main._rate_buckets.clear()
    notifications._NOTIFICATIONS.clear()


def test_reviewer_role_is_limited_to_review_routes(review_client: TestClient) -> None:
    """审核员可进入审核队列，但不能访问普通管理 API。"""
    with review_client as client:
        queue = client.get("/review/api/queue", auth=("alice", "alice-pass"))
        forbidden = client.get("/api/stats", auth=("alice", "alice-pass"))
    assert queue.status_code == 200
    assert queue.json()["role"] == "reviewer"
    assert forbidden.status_code == 403


def test_claim_collision_blind_queue_and_draft_privacy(review_client: TestClient) -> None:
    """有效领取不可被覆盖，盲审隐藏评分，草稿不泄露给其他审核员。"""
    candidate_id = _seed_candidate()
    with review_client as client:
        queue = client.get("/review/api/queue", auth=("alice", "alice-pass")).json()
        assert queue["items"][0]["score"] is None
        assert queue["items"][0]["reason"] is None

        claimed = client.post(
            f"/review/api/{candidate_id}/claim",
            json={"force": False},
            auth=("alice", "alice-pass"),
        )
        collision = client.post(
            f"/review/api/{candidate_id}/claim",
            json={"force": False},
            auth=("bob", "bob-pass"),
        )
        draft = client.put(
            f"/review/api/{candidate_id}/draft",
            json={"decision": "hold", "reason": "需要回看"},
            auth=("alice", "alice-pass"),
        )
        alice_view = client.get(f"/review/api/{candidate_id}", auth=("alice", "alice-pass")).json()
        bob_view = client.get(f"/review/api/{candidate_id}", auth=("bob", "bob-pass")).json()

    assert claimed.status_code == 200
    assert collision.status_code == 409
    assert draft.status_code == 200
    assert alice_view["candidate"]["highlight_score"] is None
    assert alice_view["score_breakdown"] == []
    assert alice_view["workflow"]["draft"]["reason"] == "需要回看"
    assert bob_view["workflow"]["draft"] is None


def test_review_submission_releases_claim_and_can_be_undone(
    review_client: TestClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """提交决策后自动释放，重新领取后可撤销并留下审计记录。"""
    from app.db.models import CandidateStatus, HighlightCandidate, HighlightEvent, ReviewStatus, SystemLog
    from app.db.session import get_session
    from app.pipeline import highlight_feedback

    feedback_calls: list[tuple[int, str, str]] = []

    def record_feedback(
        candidate_id: int,
        *,
        decision: str,
        reviewed_by: str,
        reviewed_at: datetime | None = None,
    ) -> None:
        feedback_calls.append((candidate_id, decision, reviewed_by))

    monkeypatch.setattr(highlight_feedback, "record_candidate_review_feedback", record_feedback)

    candidate_id = _seed_candidate()
    auth = ("alice", "alice-pass")
    with review_client as client:
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        submitted = client.post(
            f"/review/api/{candidate_id}/review",
            json={"decision": "rejected", "reason": "无有效内容"},
            auth=auth,
        )
        after_submit = client.get(f"/review/api/{candidate_id}", auth=auth).json()
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        undone = client.post(f"/review/api/{candidate_id}/undo", auth=auth)
        audit = client.get("/review/api/audit", auth=("admin", "admin-pass"))

    assert submitted.status_code == 200
    assert after_submit["workflow"]["claim"]["active"] is False
    assert undone.status_code == 200
    assert undone.json()["review_status"] == ReviewStatus.PENDING
    assert audit.status_code == 200
    assert {item["event"] for item in audit.json()["items"]} >= {
        "review.claim",
        "review.submit_review",
        "review.undo",
    }

    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).one()
        logs = db.exec(select(SystemLog).where(SystemLog.module == "review")).all()
    assert candidate is not None and candidate.status == CandidateStatus.PENDING
    assert event.review_status == ReviewStatus.PENDING
    assert len(logs) >= 4
    assert feedback_calls == [
        (candidate_id, ReviewStatus.REJECTED, "alice"),
        (candidate_id, ReviewStatus.PENDING, "alice"),
    ]


def test_admin_can_force_take_over_claim(review_client: TestClient) -> None:
    """管理员只有显式 force 时才能接管他人的有效领取。"""
    candidate_id = _seed_candidate()
    with review_client as client:
        client.post(
            f"/review/api/{candidate_id}/claim",
            json={"force": False},
            auth=("alice", "alice-pass"),
        )
        conflict = client.post(
            f"/review/api/{candidate_id}/claim",
            json={"force": False},
            auth=("admin", "admin-pass"),
        )
        edit_conflict = client.put(
            f"/review/api/{candidate_id}/draft",
            json={"reason": "admin edit"},
            auth=("admin", "admin-pass"),
        )
        takeover = client.post(
            f"/review/api/{candidate_id}/claim",
            json={"force": True},
            auth=("admin", "admin-pass"),
        )
    assert conflict.status_code == 409
    assert edit_conflict.status_code == 409
    assert takeover.status_code == 200
    assert takeover.json()["claim"]["claimed_by"] == "admin"


def test_review_queue_page_exposes_filters_and_claim_action(review_client: TestClient) -> None:
    """审核队列页面提供筛选、我的领取和进入审核操作。"""
    with review_client as client:
        response = client.get("/review/queue", auth=("alice", "alice-pass"))
    assert response.status_code == 200
    assert 'id="mine"' in response.text
    assert 'data-status="pending"' in response.text
    assert "/claim" in response.text


def test_review_data_buckets_scalar_danmaku_timestamps(review_client: TestClient) -> None:
    """审片详情应兼容 SQLModel 单列查询返回的时间戳标量。"""
    from app.db.models import Danmaku, HighlightCandidate
    from app.db.session import get_session

    candidate_id = _seed_candidate()
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        assert candidate is not None
        db.add(
            Danmaku(
                session_id=candidate.session_id,
                room_id=200,
                ts=candidate.peak_ts,
                msg_type="danmaku",
                content="测试弹幕",
            )
        )

    with review_client as client:
        response = client.get(f"/review/api/{candidate_id}", auth=("admin", "admin-pass"))

    assert response.status_code == 200
    assert sum(bucket["count"] for bucket in response.json()["danmaku_buckets"]) == 1


def test_multi_room_queues_keep_source_identity_and_candidate_transcript(
    review_client: TestClient,
) -> None:
    """多直播间任务不得串来源，审片转写应覆盖候选跨分段前文。"""
    from app.db.models import (
        HighlightCandidate,
        LiveRoom,
        RawSegment,
        RecordingSession,
        SegmentTask,
        SessionStatus,
        Transcript,
    )
    from app.db.session import get_session

    base = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room_a = LiveRoom(input_url="a", room_id=1001, uploader_name="主播甲", title="甲的直播")
        room_b = LiveRoom(input_url="b", room_id=1002, uploader_name="主播乙", title="乙的直播")
        db.add(room_a)
        db.add(room_b)
        db.flush()
        session_a = RecordingSession(room_id=room_a.id, status=SessionStatus.STOPPED, ended_at=base)
        session_b = RecordingSession(room_id=room_b.id, status=SessionStatus.STOPPED, ended_at=base)
        db.add(session_a)
        db.add(session_b)
        db.flush()
        segment_a1 = RawSegment(
            session_id=session_a.id,
            seq=1,
            file_path="a-1.ts",
            start_ts=base,
            end_ts=base + timedelta(seconds=300),
            duration_s=300,
        )
        segment_a2 = RawSegment(
            session_id=session_a.id,
            seq=2,
            file_path="a-2.ts",
            start_ts=base + timedelta(seconds=300),
            end_ts=base + timedelta(seconds=600),
            duration_s=300,
        )
        segment_b = RawSegment(
            session_id=session_b.id,
            seq=1,
            file_path="b-1.ts",
            start_ts=base,
            end_ts=base + timedelta(seconds=300),
            duration_s=300,
        )
        db.add(segment_a1)
        db.add(segment_a2)
        db.add(segment_b)
        db.flush()
        candidate_a = HighlightCandidate(
            session_id=session_a.id,
            peak_ts=base + timedelta(seconds=315),
            start_ts=base + timedelta(seconds=240),
            end_ts=base + timedelta(seconds=360),
            highlight_score=0.7,
            dedup_hash="multi-room-a",
        )
        candidate_b = HighlightCandidate(
            session_id=session_b.id,
            peak_ts=base + timedelta(seconds=120),
            start_ts=base + timedelta(seconds=60),
            end_ts=base + timedelta(seconds=150),
            highlight_score=0.6,
            dedup_hash="multi-room-b",
        )
        db.add(candidate_a)
        db.add(candidate_b)
        db.flush()
        db.add(
            Transcript(
                segment_id=segment_a1.id,
                text="前文",
                words_json=json.dumps([{"word": "前文", "start": 250, "end": 252}]),
            )
        )
        db.add(
            Transcript(
                segment_id=segment_a2.id,
                text="爆点",
                words_json=json.dumps([{"word": "爆点", "start": 5, "end": 7}]),
            )
        )
        db.add(
            SegmentTask(
                segment_id=segment_a2.id,
                session_id=session_a.id,
                candidate_id=candidate_a.id,
                pipeline_key=f"pipeline:{segment_a2.id}",
            )
        )
        db.add(
            SegmentTask(
                segment_id=segment_b.id,
                session_id=session_b.id,
                candidate_id=candidate_b.id,
                pipeline_key=f"pipeline:{segment_b.id}",
            )
        )
        db.flush()
        candidate_a_id = candidate_a.id
        session_a_id = session_a.id
        session_b_id = session_b.id
        segment_a1_id = segment_a1.id
        segment_a2_id = segment_a2.id

    assert candidate_a_id is not None
    assert session_a_id is not None and session_b_id is not None
    assert segment_a1_id is not None and segment_a2_id is not None
    admin_auth = ("admin", "admin-pass")
    with review_client as client:
        tasks = client.get("/api/tasks", auth=admin_auth).json()["tasks"]
        candidates = client.get("/api/candidates", auth=admin_auth).json()
        queue = client.get("/review/api/queue", auth=("alice", "alice-pass")).json()["items"]
        review = client.get(f"/review/api/{candidate_a_id}", auth=admin_auth).json()

    expected = {session_a_id: "主播甲 · 房间 1001", session_b_id: "主播乙 · 房间 1002"}
    assert {item["session_id"]: item["source_label"] for item in tasks} == expected
    assert {item["session_id"]: item["source_label"] for item in candidates} == expected
    assert {item["session_id"]: item["source_label"] for item in queue} == expected
    assert review["source"]["source_label"] == expected[session_a_id]
    assert review["transcript"]["text"] == "前文\n爆点"
    assert review["transcript"]["segment_ids"] == [segment_a1_id, segment_a2_id]
    assert [word["start"] for word in review["transcript"]["words"]] == [10.0, 65.0]


def test_pending_candidate_preview_is_available_without_final_clip(
    review_client: TestClient,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """候选状态尚未出片时，审片工作台仍应返回可播放预览。"""
    from app.web.routers import review_router

    candidate_id = _seed_candidate()
    preview = tmp_path / "candidate-preview.mp4"
    preview.write_bytes(b"preview-video")

    def fake_preview(_candidate_id: int, _start_ts: datetime, _end_ts: datetime) -> Path:
        return preview

    monkeypatch.setattr(review_router, "_ensure_review_preview", fake_preview)
    with review_client as client:
        data = client.get(f"/review/api/{candidate_id}", auth=("admin", "admin-pass"))
        media = client.get(f"/review/api/{candidate_id}/preview", auth=("admin", "admin-pass"))

    assert data.status_code == 200
    assert data.json()["existing_clips"] == []
    assert data.json()["media_url"] == f"/review/api/{candidate_id}/preview"
    assert media.status_code == 200
    assert media.headers["content-type"].startswith("video/mp4")
    assert media.content == b"preview-video"
