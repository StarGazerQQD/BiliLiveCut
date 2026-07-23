"""多人审核队列、权限、草稿、盲审、撤销和审计回归测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


def test_review_submission_releases_claim_and_can_be_undone(review_client: TestClient) -> None:
    """提交决策后自动释放，重新领取后可撤销并留下审计记录。"""
    from app.db.models import CandidateStatus, HighlightCandidate, HighlightEvent, ReviewStatus, SystemLog
    from app.db.session import get_session

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
