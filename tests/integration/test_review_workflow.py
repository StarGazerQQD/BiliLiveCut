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
    from app.db.models import (
        CandidateStatus,
        ClipStatus,
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        ReviewStatus,
        SegmentTask,
        SystemLog,
        TaskStatus,
    )
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
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        assert candidate is not None
        clip = FinalClip(
            candidate_id=candidate_id,
            file_path="reviewing.mp4",
            status=ClipStatus.REVIEWING,
        )
        db.add(clip)
        db.flush()
        clip_id = clip.id
        task = SegmentTask(
            segment_id=9001,
            session_id=candidate.session_id,
            candidate_id=candidate_id,
            clip_id=clip_id,
            stage=TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
            stage_key="stage:9001:awaiting_publish_confirmation",
            idempotency_key="9001:awaiting_publish_confirmation",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    assert clip_id is not None
    assert task_id is not None

    auth = ("alice", "alice-pass")
    with review_client as client:
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        submitted = client.post(
            f"/review/api/{candidate_id}/review",
            json={"decision": "rejected", "reason": "无有效内容"},
            auth=auth,
        )
        after_submit = client.get(f"/review/api/{candidate_id}", auth=auth).json()
        clips_after_submit = client.get("/api/clips", auth=("admin", "admin-pass")).json()
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        undone = client.post(f"/review/api/{candidate_id}/undo", auth=auth)
        clips_after_undo = client.get("/api/clips", auth=("admin", "admin-pass")).json()
        audit = client.get("/review/api/audit", auth=("admin", "admin-pass"))

    assert submitted.status_code == 200
    assert after_submit["workflow"]["claim"]["active"] is False
    assert all(item["id"] != clip_id for item in clips_after_submit)
    assert undone.status_code == 200
    assert undone.json()["review_status"] == ReviewStatus.PENDING
    assert any(item["id"] == clip_id and item["status"] == ClipStatus.REVIEWING for item in clips_after_undo)
    assert audit.status_code == 200
    assert {item["event"] for item in audit.json()["items"]} >= {
        "review.claim",
        "review.submit_review",
        "review.undo",
    }

    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).one()
        clip = db.get(FinalClip, clip_id)
        task = db.get(SegmentTask, task_id)
        logs = db.exec(select(SystemLog).where(SystemLog.module == "review")).all()
    assert candidate is not None and candidate.status == CandidateStatus.PENDING
    assert event.review_status == ReviewStatus.PENDING
    assert clip is not None and clip.status == ClipStatus.REVIEWING
    assert task is not None and task.stage == TaskStatus.AWAITING_PUBLISH_CONFIRMATION
    assert task.stage_key == "stage:9001:awaiting_publish_confirmation"
    assert task.idempotency_key == "9001:awaiting_publish_confirmation"
    assert len(logs) >= 4
    assert feedback_calls == [
        (candidate_id, ReviewStatus.REJECTED, "alice"),
        (candidate_id, ReviewStatus.PENDING, "alice"),
    ]


def test_review_submission_does_not_fail_after_threshold_learning_error(
    review_client: TestClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """审核事务已提交后，阈值学习异常只能降级记录，不能返回 500。"""
    from app.analysis import threshold_learning

    def fail_sync(candidate_id: int, *, decision: str) -> dict[str, object]:
        raise ValueError(f"invalid feedback candidate={candidate_id} decision={decision}")

    monkeypatch.setattr(threshold_learning, "sync_candidate_feedback", fail_sync)
    candidate_id = _seed_candidate()
    auth = ("alice", "alice-pass")
    with review_client as client:
        claimed = client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        submitted = client.post(
            f"/review/api/{candidate_id}/review",
            json={"decision": "hold", "reason": "稍后复核"},
            auth=auth,
        )

    assert claimed.status_code == 200
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "hold"


@pytest.mark.parametrize(
    ("decision", "expected_stage"),
    [
        ("hold", "reviewed_waiting_action"),
        ("end_too_early", "reviewed_waiting_action"),
        ("approved_solo", "queued_for_render"),
    ],
)
def test_review_submission_advances_linked_task_state(
    review_client: TestClient,
    decision: str,
    expected_stage: str,
) -> None:
    """人工反馈必须离开 awaiting_review，独立成片还要立即进入渲染队列。"""
    from app.db.models import HighlightCandidate, SegmentTask, TaskStatus
    from app.db.session import get_session

    candidate_id = _seed_candidate()
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        assert candidate is not None
        task = SegmentTask(
            segment_id=9100 + candidate_id,
            session_id=candidate.session_id,
            candidate_id=candidate_id,
            stage=TaskStatus.AWAITING_REVIEW,
            pipeline_key=f"pipeline:{9100 + candidate_id}",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    assert task_id is not None

    auth = ("alice", "alice-pass")
    with review_client as client:
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        submitted = client.post(
            f"/review/api/{candidate_id}/review",
            json={"decision": decision, "reason": "回归测试"},
            auth=auth,
        )

    assert submitted.status_code == 200
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        assert task.stage == expected_stage


def test_unlinked_approved_candidate_enqueues_background_render(
    review_client: TestClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """没有主 SegmentTask 的额外爆点通过审核后也必须进入后台出片作业。"""
    from app.web.services.background_jobs import web_job_manager

    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_enqueue(job_type: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append((job_type, payload))
        return {"id": "job-1", "status": "queued"}

    monkeypatch.setattr(web_job_manager, "enqueue", fake_enqueue)
    candidate_id = _seed_candidate()
    auth = ("alice", "alice-pass")
    with review_client as client:
        client.post(f"/review/api/{candidate_id}/claim", json={"force": False}, auth=auth)
        submitted = client.post(
            f"/review/api/{candidate_id}/review",
            json={"decision": "approved_solo", "reason": "独立成片"},
            auth=auth,
        )

    assert submitted.status_code == 200
    assert submitted.json()["job"]["id"] == "job-1"
    assert calls == [("candidate_render", {"candidate_id": candidate_id, "reviewed_by": "alice"})]


def test_background_candidate_render_keeps_task_lease_until_clip_is_recorded(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """网页后台渲染必须持有心跳租约，且成功后把任务推进到成品阶段。"""
    import threading

    from app.db.models import FinalClip, HighlightCandidate, HighlightEvent, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline import heartbeat, orchestrator
    from app.pipeline.stale_recovery import recover_orphans
    from app.web.services.candidates import approve_candidate_sync

    candidate_id = _seed_candidate()
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        assert candidate is not None
        event = HighlightEvent(candidate_id=candidate_id, session_id=candidate.session_id)
        db.add(event)
        db.flush()
        task = SegmentTask(
            segment_id=9101,
            session_id=candidate.session_id,
            candidate_id=candidate_id,
            event_id=event.id,
            stage=TaskStatus.AWAITING_REVIEW,
            stage_key="stage:9101:awaiting_review",
            idempotency_key="9101:awaiting_review",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    assert task_id is not None

    heartbeat_calls: list[tuple[int, str | None, str | None]] = []
    heartbeat_stop = threading.Event()

    def fake_start_heartbeat(
        claimed_task_id: int,
        lease_token: str | None = None,
        expected_stage: str | None = None,
    ) -> threading.Event:
        heartbeat_calls.append((claimed_task_id, lease_token, expected_stage))
        return heartbeat_stop

    def fake_produce_clip(render_candidate_id: int, **_kwargs: object) -> FinalClip:
        assert render_candidate_id == candidate_id
        with get_session() as db:
            claimed = db.get(SegmentTask, task_id)
            assert claimed is not None
            assert claimed.stage == TaskStatus.RENDERING
            assert claimed.heartbeat_at is not None
            assert claimed.lease_token
        recover_orphans()
        with get_session() as db:
            claimed = db.get(SegmentTask, task_id)
            assert claimed is not None
            assert claimed.stage == TaskStatus.RENDERING
            clip = FinalClip(candidate_id=candidate_id, file_path="approved.mp4")
            db.add(clip)
            db.flush()
            clip_id = clip.id
        assert clip_id is not None
        return clip

    monkeypatch.setattr(heartbeat, "start_heartbeat_thread", fake_start_heartbeat)
    monkeypatch.setattr(orchestrator, "produce_clip", fake_produce_clip)

    clip_id = approve_candidate_sync(candidate_id, reviewed_by="alice")

    assert clip_id is not None
    assert heartbeat_stop.is_set()
    assert len(heartbeat_calls) == 1
    claimed_task_id, lease_token, expected_stage = heartbeat_calls[0]
    assert claimed_task_id == task_id
    assert lease_token
    assert expected_stage == TaskStatus.RENDERING
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        assert task.stage == TaskStatus.RENDERED
        assert task.clip_id == clip_id


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
                text="爆点。候选结束后才发生的内容。",
                words_json=json.dumps(
                    [
                        {"word": "爆点", "start": 5, "end": 7},
                        {"word": "候选结束后才发生的内容", "start": 120, "end": 125},
                    ]
                ),
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


def test_review_preview_cache_path_is_opaque_and_contained(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """候选请求参数不得直接进入预览文件名，缓存路径必须留在受控目录。"""
    from app.core import paths
    from app.web.routers import review_router

    clips_root = tmp_path / "clips"
    monkeypatch.setattr(paths, "clips_dir", lambda: clips_root)
    start_ts = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    end_ts = start_ts + timedelta(minutes=5)

    preview_path = review_router._review_preview_path(123456, start_ts, end_ts)

    assert preview_path.parent == (clips_root / "review_previews").resolve()
    assert "123456" not in preview_path.name
    key_parts = preview_path.stem.split("_")
    assert len(key_parts) == 2
    assert all(len(part) == 16 and set(part) <= set("0123456789abcdef") for part in key_parts)


def test_review_preview_render_uses_server_generated_temporary_path(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """预览渲染仅向受控缓存目录内的随机临时文件写入。"""
    from app.clipping import core
    from app.core import paths
    from app.web.routers import review_router

    clips_root = tmp_path / "clips"
    monkeypatch.setattr(paths, "clips_dir", lambda: clips_root)
    rendered_paths: list[Path] = []

    def fake_render(
        candidate_id: int,
        output_path: str | Path,
        _options: object,
        *,
        start_ts: datetime | None = None,
        end_ts: datetime | None = None,
    ) -> dict[str, object]:
        assert candidate_id == 987654
        assert start_ts is not None and end_ts is not None
        target = Path(output_path)
        rendered_paths.append(target)
        target.write_bytes(b"preview-video")
        return {"file_path": str(target)}

    monkeypatch.setattr(core, "render_clip_to_file", fake_render)
    start_ts = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    end_ts = start_ts + timedelta(minutes=5)

    first = review_router._ensure_review_preview(987654, start_ts, end_ts)
    second = review_router._ensure_review_preview(987654, start_ts, end_ts)

    assert first == second
    assert first.read_bytes() == b"preview-video"
    assert len(rendered_paths) == 1
    assert rendered_paths[0].parent == first.parent
    assert rendered_paths[0].name.endswith(".partial.mp4")
    assert "987654" not in rendered_paths[0].name


def test_review_preview_errors_do_not_expose_internal_details(
    review_client: TestClient,
    monkeypatch: MonkeyPatch,
) -> None:
    """预览及波形失败响应不得泄露内部路径或异常文本。"""
    from app.web.routers import review_router

    candidate_id = _seed_candidate()
    secret_detail = r"C:\sensitive\recording\source.flv"

    def fail_preview(_candidate_id: int, _start_ts: datetime, _end_ts: datetime) -> Path:
        raise RuntimeError(secret_detail)

    monkeypatch.setattr(review_router, "_ensure_review_preview", fail_preview)
    with review_client as client:
        preview = client.get(f"/review/api/{candidate_id}/preview", auth=("admin", "admin-pass"))
        waveform = client.get(f"/review/api/{candidate_id}/waveform", auth=("admin", "admin-pass"))

    assert preview.status_code == 500
    assert preview.json()["detail"] == "候选预览渲染失败"
    assert waveform.status_code == 200
    assert waveform.json()["error"] == "候选预览渲染失败"
    assert secret_detail not in preview.text
    assert secret_detail not in waveform.text
