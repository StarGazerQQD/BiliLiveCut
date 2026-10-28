from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlmodel import select

from app.clipping.drafts import ClipDraft, input_fingerprint, publication, publish_draft
from app.db import job_store
from app.db.entities import FinalClip, HighlightCandidate
from app.db.session import get_session
from app.jobs.context import JobContext
from app.jobs.contracts import JobAttempt, JobData, JobSuperseded, now_iso
from app.jobs.manager import JobManager


def _job(*, owner: str = "alice", job_type: str = "render", value: int = 1) -> JobData:
    now = now_iso()
    return {
        "version": 1,
        "id": uuid4().hex,
        "type": job_type,
        "label": "事务测试",
        "owner": owner,
        "payload": {"value": value},
        "dedup_key": "same-input",
        "cancellable_while_running": True,
        "status": "queued",
        "progress": 0,
        "message": "等待执行",
        "result": None,
        "error": None,
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "recovered": False,
    }


def _context() -> JobContext:
    job = job_store.register(_job())
    job_store.claim(JobAttempt(job["id"], 1))
    return JobContext(job["id"], Event())


def _draft() -> ClipDraft:
    now = datetime.now(UTC)
    with get_session() as db:
        candidate = HighlightCandidate(
            session_id=1,
            start_ts=now,
            end_ts=now,
            peak_ts=now,
            dedup_hash=uuid4().hex,
            status="approved",
        )
        db.add(candidate)
        db.flush()
        assert candidate.id is not None
        return ClipDraft(
            clip=FinalClip(candidate_id=candidate.id, file_path="verified-artifact.mp4"),
            variants=[],
            input_fingerprint=input_fingerprint(db, candidate.id),
        )


def test_result_and_success_rollback_together(temp_db: None) -> None:
    context, draft = _context(), _draft()
    with pytest.raises(OSError, match="写入失败"):
        with context.result_transaction() as db:
            clip = publish_draft(db, draft)
            context.complete(db, {"clip_id": clip.id})
            raise OSError("数据库写入失败")
    with get_session() as db:
        assert db.exec(select(FinalClip)).all() == []
        assert db.get(HighlightCandidate, draft.clip.candidate_id).status == "approved"
    assert job_store.get_job(context.job_id)["status"] == "running"


def test_result_scope_requires_success_in_same_transaction(temp_db: None) -> None:
    context, draft = _context(), _draft()
    with pytest.raises(JobSuperseded):
        with context.result_transaction() as db:
            publish_draft(db, draft)
    with get_session() as db:
        assert db.exec(select(FinalClip)).all() == []


def test_cancel_wins_before_result_publication(temp_db: None) -> None:
    context, draft = _context(), _draft()
    job_store.cancel(context.job_id, "alice")
    with pytest.raises(JobSuperseded):
        with context.result_transaction() as db:
            clip = publish_draft(db, draft)
            context.complete(db, {"clip_id": clip.id})
    with get_session() as db:
        assert db.exec(select(FinalClip)).all() == []
    assert job_store.get_job(context.job_id)["status"] == "cancelling"


def test_published_success_rejects_cancel_and_progress(temp_db: None) -> None:
    context, draft = _context(), _draft()
    with context.result_transaction() as db:
        clip = publish_draft(db, draft)
        context.complete(db, {"clip_id": clip.id})
    before = job_store.get_job(context.job_id)
    with pytest.raises(ValueError, match="已经结束"):
        job_store.cancel(context.job_id, "alice")
    with pytest.raises(JobSuperseded):
        context.report(30, "迟到进度")
    job_store.fail(context.token, "迟到错误")
    assert job_store.get_job(context.job_id) == before


def test_new_attempt_rejects_all_old_reports(temp_db: None) -> None:
    old = _context()
    job_store.fail(old.token, "当前代次失败")
    renewed = job_store.retry(old.job_id, "alice")
    current = JobAttempt(old.job_id, renewed["attempt"])
    job_store.claim(current)
    with pytest.raises(JobSuperseded):
        job_store.finish(old.token, {"stale": True})
    with pytest.raises(JobSuperseded):
        old.report(90, "旧执行")
    job_store.fail(old.token, "旧错误")
    job_store.finish(current, {"current": True})
    assert job_store.get_job(old.job_id)["result"] == {"current": True}


def test_changed_domain_input_rolls_back_job_and_result(temp_db: None) -> None:
    context, draft = _context(), _draft()
    with get_session() as db:
        candidate = db.get(HighlightCandidate, draft.clip.candidate_id)
        candidate.reason = "人工修改"
        db.add(candidate)
    with pytest.raises(ValueError, match="已改变"):
        with context.result_transaction() as db:
            publish_draft(db, draft)
            context.complete(db, {"clip_id": draft.clip.id})
    with get_session() as db:
        assert db.exec(select(FinalClip)).all() == []
        assert db.get(HighlightCandidate, draft.clip.candidate_id).reason == "人工修改"
    assert job_store.get_job(context.job_id)["status"] == "running"


def test_cancelled_web_render_settles_pipeline_cancellation(temp_db: None) -> None:
    from app.db.entities import HighlightEvent, SegmentTask
    from app.web.services.candidates import approve_candidate_sync

    context, draft = _context(), _draft()
    candidate_id = draft.clip.candidate_id
    with get_session() as db:
        event = HighlightEvent(candidate_id=candidate_id, session_id=1, segment_id=1)
        db.add(event)
        db.flush()
        pipeline = SegmentTask(
            segment_id=1,
            session_id=1,
            candidate_id=candidate_id,
            event_id=event.id,
            stage="awaiting_review",
            stage_key="stage:1:awaiting_review",
        )
        db.add(pipeline)
        db.flush()
        pipeline_id = pipeline.id
    job_store.cancel(context.job_id, "alice")
    with pytest.raises(JobSuperseded):
        approve_candidate_sync(candidate_id, progress_callback=context.report, job_context=context)
    with get_session() as db:
        pipeline = db.get(SegmentTask, pipeline_id)
        assert pipeline.stage == "cancelled"
        assert pipeline.lease_token is None
        assert db.exec(select(FinalClip)).all() == []
    assert job_store.get_job(context.job_id)["status"] == "cancelling"


@pytest.mark.parametrize("referenced", [False, True])
def test_failed_publication_only_cleans_unreferenced_owned_files(
    temp_db: None, tmp_path: Path, referenced: bool
) -> None:
    output = tmp_path / "current.mp4"
    original = tmp_path / "previous.mp4"
    output.write_bytes(b"current artifact")
    original.write_bytes(b"previous artifact")
    if referenced:
        with get_session() as db:
            db.add(FinalClip(candidate_id=1, file_path=str(output)))
    with pytest.raises(OSError, match="失败"):
        with publication((output,)):
            raise OSError("发布失败")
    assert output.exists() == referenced
    assert original.read_bytes() == b"previous artifact"


@pytest.mark.parametrize("repeat", range(8))
def test_concurrent_cancel_and_publication_have_one_winner(temp_db: None, repeat: int) -> None:
    context, draft = _context(), _draft()
    barrier = Barrier(2)

    def publish() -> bool:
        barrier.wait(timeout=5)
        try:
            with context.result_transaction() as db:
                clip = publish_draft(db, draft)
                context.complete(db, {"clip_id": clip.id})
        except JobSuperseded:
            return False
        return True

    def cancel() -> bool:
        barrier.wait(timeout=5)
        try:
            job_store.cancel(context.job_id, "alice")
        except ValueError as exc:
            assert str(exc) == "作业已经结束"
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication, cancellation = pool.submit(publish), pool.submit(cancel)
        published, cancelled = publication.result(timeout=10), cancellation.result(timeout=10)
    assert published != cancelled
    state = job_store.get_job(context.job_id)
    assert state["status"] == ("succeeded" if published else "cancelling")
    with get_session() as db:
        clips = db.exec(select(FinalClip)).all()
        assert len(clips) == int(published)
        if published:
            assert state["result"] == {"clip_id": clips[0].id}


def test_registration_is_atomic_and_scoped_to_owner_and_operation(temp_db: None) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        registered = list(pool.map(job_store.register, [_job() for _ in range(12)]))
    identifiers = {job["id"] for job in registered}
    assert len(identifiers) == 1
    assert job_store.register(_job(owner="bob"))["id"] not in identifiers
    assert job_store.register(_job(job_type="import"))["id"] not in identifiers
    with pytest.raises(ValueError, match="不同请求"):
        job_store.register(_job(value=2))
    with pytest.raises(PermissionError):
        job_store.cancel(registered[0]["id"], "bob")


async def test_concurrent_first_requests_start_manager_once(temp_db: None) -> None:
    manager = JobManager(max_concurrency=1)
    entered, release = Event(), Event()
    calls: list[str] = []

    def handler(context: JobContext, payload: dict[str, object]) -> dict[str, bool]:
        calls.append(context.job_id)
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    manager.register("render", handler)
    try:
        jobs = await asyncio.gather(
            *(manager.enqueue("render", {}, label="test", owner="alice", dedup_key="same") for _ in range(6))
        )
        assert await asyncio.to_thread(entered.wait, 2)
        assert len({job["id"] for job in jobs}) == 1
        release.set()
        await asyncio.gather(*tuple(manager._tasks.values()))
        assert calls == [jobs[0]["id"]]
        assert job_store.get_job(jobs[0]["id"])["status"] == "succeeded"
    finally:
        release.set()
        await manager.stop()


@pytest.mark.parametrize("operation", ["candidate_render", "review_rerender"])
async def test_web_render_publishes_real_media_copy_and_task_together(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from app.core.config import settings
    from app.db.entities import ClipVariant, HighlightEvent, LiveRoom, RawSegment, RecordingSession, SegmentTask
    from app.web.services.background_jobs import WebJobManager

    monkeypatch.setattr(settings, "clip_loudnorm", False)
    monkeypatch.setattr(settings, "clip_remove_silence", False)
    monkeypatch.setattr(settings, "clip_preset", "ultrafast")
    source = tmp_path / "source.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    base = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(platform="local", input_url="local:test", authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status="stopped")
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path=str(source),
            start_ts=base,
            end_ts=base + timedelta(seconds=4),
            duration_s=4,
        )
        candidate = HighlightCandidate(
            session_id=session.id,
            start_ts=base,
            end_ts=base + timedelta(seconds=3),
            peak_ts=base,
            dedup_hash="real-job-render",
            status="approved",
        )
        db.add(segment)
        db.add(candidate)
        db.flush()
        event = HighlightEvent(candidate_id=candidate.id, session_id=session.id, segment_id=segment.id)
        db.add(event)
        db.flush()
        pipeline = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            candidate_id=candidate.id,
            event_id=event.id,
            stage="awaiting_review",
            stage_key=f"stage:{segment.id}:awaiting_review",
        )
        db.add(pipeline)
        db.flush()
        candidate_id, pipeline_id = candidate.id, pipeline.id
    manager = WebJobManager()
    payload = (
        {"candidate_id": candidate_id, "reviewed_by": "alice"}
        if operation == "candidate_render"
        else {
            "candidate_id": candidate_id,
            "start_ts": base.isoformat(),
            "end_ts": (base + timedelta(seconds=3)).isoformat(),
            "version": "review",
        }
    )
    try:
        job = await manager.enqueue(operation, payload, label="真实媒体", owner="alice")
        await asyncio.wait_for(asyncio.gather(*tuple(manager._tasks.values())), timeout=60)
        state = job_store.get_job(job["id"])
        assert state["status"] == "succeeded", state["error"]
        with get_session() as db:
            clip = db.exec(select(FinalClip)).one()
            assert state["result"]["clip_id"] == clip.id
            assert clip.title and clip.tags_json and clip.status == "reviewing"
            assert Path(clip.file_path).stat().st_size > 0
            assert clip.duration_s == pytest.approx(3, abs=0.2)
            if operation == "candidate_render":
                assert db.get(SegmentTask, pipeline_id).stage == "rendered"
                variants = db.exec(select(ClipVariant)).all()
                assert len(variants) >= 4
                assert all(Path(variant.file_path).is_file() for variant in variants)
    finally:
        await manager.stop()


@pytest.mark.parametrize("publication_state", ["current", "cancelled", "changed"])
def test_collection_real_media_publication_checks_cancellation_and_inputs(
    temp_db: None, tmp_path: Path, publication_state: str
) -> None:
    from app.core.config import settings
    from app.core.paths import clips_dir
    from app.db.entities import ClipVariant, HighlightEvent, HighlightTopic, LiveRoom, RecordingSession, Topic
    from app.pipeline.collection import render_collection

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            settings.ffmpeg_path,
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
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    event_ids, candidate_ids = [], []
    base = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(platform="local", input_url="local:collection", authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status="stopped")
        db.add(session)
        db.flush()
        topic = Topic(session_id=session.id, title="真实合集")
        db.add(topic)
        db.flush()
        topic_id = topic.id
        for index in range(2):
            candidate = HighlightCandidate(
                session_id=session.id,
                start_ts=base,
                end_ts=base + timedelta(seconds=2),
                peak_ts=base,
                dedup_hash=f"collection-{index}",
                status="approved",
            )
            db.add(candidate)
            db.flush()
            candidate_ids.append(candidate.id)
            event = HighlightEvent(candidate_id=candidate.id, session_id=session.id)
            db.add(event)
            db.flush()
            event_ids.append(event.id)
            db.add(HighlightTopic(event_id=event.id, topic_id=topic_id, sort_order=index))
            db.add(FinalClip(candidate_id=candidate.id, file_path=str(source), duration_s=2, status="generated"))
    context = _context()

    def report(progress: int, message: str) -> None:
        if progress == 90:
            if publication_state == "cancelled":
                job_store.cancel(context.job_id, "alice")
            elif publication_state == "changed":
                with get_session() as db:
                    candidate = db.get(HighlightCandidate, candidate_ids[0])
                    candidate.reason = "人工更新"
                    db.add(candidate)
        context.report(progress, message)

    def render() -> ClipVariant | None:
        return render_collection(
            topic_id, event_ids, include_chapter_cards=False, progress_callback=report, job_context=context
        )

    if publication_state == "current":
        result = render()
        assert result is not None and Path(result.file_path).stat().st_size > 0
        assert result.duration_s == pytest.approx(4, abs=0.3)
    else:
        with pytest.raises(JobSuperseded if publication_state == "cancelled" else ValueError):
            render()
        assert not list(clips_dir().rglob("collection_*.mp4"))
    state = job_store.get_job(context.job_id)
    assert (
        state["status"] == {"current": "succeeded", "cancelled": "cancelling", "changed": "running"}[publication_state]
    )
    with get_session() as db:
        variants = db.exec(select(ClipVariant)).all()
        assert len(variants) == int(publication_state == "current")
        assert db.get(Topic, topic_id).is_collection == (publication_state == "current")
        if variants:
            assert state["result"]["variant_id"] == variants[0].id
        for candidate_id in candidate_ids:
            assert db.get(HighlightCandidate, candidate_id).status == ("merged" if variants else "approved")
    assert source.stat().st_size > 0
