from __future__ import annotations

import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection as ProcessConnection
from os import stat_result
from pathlib import Path
from threading import Event

import pytest
from filelock import ReadWriteLock
from sqlmodel import select
from starlette.types import Message

from app.analysis import editorial_review_sources as sources
from app.core.media_usage import media_cleanup_scope, media_reference_scope
from app.core.paths import raw_dir, storage_root
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline import scheduler, stale_recovery, storage_lifecycle
from app.web.media_response import MediaFileResponse
from tests.unit.test_editorial_contracts import _source


def seed_media() -> tuple[int, int, Path]:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    path = raw_dir() / "managed.ts"
    path.write_bytes(b"recorded video")
    with get_session() as db:
        db.add(LiveRoom(id=7, input_url="media-lock", auto_approve=True, auto_approve_threshold=0.8))
        db.add(RecordingSession(id=7, room_id=7, started_at=old, ended_at=old, status="stopped"))
        db.add(
            RawSegment(id=1, session_id=7, seq=0, file_path=str(path), start_ts=old, end_ts=old + timedelta(seconds=10))
        )
    return 7, 1, path


def _hold_shared_in_child(root: str, pipe: ProcessConnection) -> None:
    lock = ReadWriteLock(Path(root) / ".media-references.lock", is_singleton=False)
    try:
        with lock.read_lock():
            pipe.send("ready")
            assert pipe.recv() == "release"
    finally:
        lock.close()
        pipe.close()


def test_media_validation_reuses_file_state_without_filesystem_calls_in_write_transaction(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id, segment_id, path = seed_media()
    with get_session() as db:
        expected = sources.segment_review_revision(db, segment_id)
    with sources.media_validation_scope(session_ids=[session_id]):
        original_stat = Path.stat

        def stat_outside_transaction(file: Path, *, follow_symlinks: bool = True) -> stat_result:
            assert file != path, "提交事务不能重新访问媒体文件"
            return original_stat(file, follow_symlinks=follow_symlinks)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", stat_outside_transaction)
            with get_session() as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                segment = db.get(RawSegment, segment_id)
                assert segment is not None and segment.start_ts is not None and segment.end_ts is not None
                assert sources.segment_review_revision(db, segment_id) == expected
                assert sources.media_file_identity(segment) is not None
                assert sources.media_bounds_accessible(db, _source(), segment.start_ts, segment.end_ts)


def test_changed_database_media_reference_invalidates_preflight_snapshot(temp_db: None) -> None:
    session_id, segment_id, path = seed_media()
    with sources.media_validation_scope(session_ids=[session_id]):
        with get_session() as db:
            first = db.get(RawSegment, segment_id)
            assert first is not None
            db.add(
                RawSegment(
                    session_id=session_id, seq=1, file_path=str(path), start_ts=first.start_ts, end_ts=first.end_ts
                )
            )
        with get_session() as db:
            assert not sources.media_snapshot_is_current(db, session_id)
            with pytest.raises(ValueError, match="媒体引用"):
                sources.segment_review_revision(db, segment_id)


def test_slow_cleanup_releases_business_database_and_serializes_orphan_recovery(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, path = seed_media()
    deleting, release, recovering = Event(), Event(), Event()
    unlink = storage_lifecycle._safe_unlink

    def slow_unlink(name: str, root: Path) -> bool:
        deleting.set()
        assert release.wait(5)
        return unlink(name, root)

    def unrelated_write() -> None:
        with get_session() as db:
            db.add(AppSetting(key="unrelated", value="committed during slow filesystem operation"))

    def recover() -> None:
        recovering.set()
        stale_recovery.recover_orphans()

    monkeypatch.setattr(storage_lifecycle, "_safe_unlink", slow_unlink)
    with ThreadPoolExecutor(max_workers=3) as executor:
        cleanup = executor.submit(storage_lifecycle.cleanup_old_raw_files)
        try:
            assert deleting.wait(5)
            executor.submit(unrelated_write).result(timeout=2)
            recovery = executor.submit(recover)
            assert recovering.wait(5)
            assert not recovery.done()
            with get_session() as db:
                assert not db.exec(select(SegmentTask)).all()
        finally:
            release.set()
        assert cleanup.result(timeout=5) == 1
        recovery.result(timeout=5)
    assert not path.exists()
    with get_session() as db:
        assert db.get(AppSetting, "unrelated") is not None
        assert db.exec(select(SegmentTask)).one().stage == TaskStatus.RECORDED


def test_shared_media_operations_can_overlap_and_prevent_cleanup(temp_db: None) -> None:
    seed_media()
    entered, release = Event(), Event()

    def other_reader() -> None:
        with media_reference_scope():
            entered.set()
            assert release.wait(5)

    with media_reference_scope(), ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(other_reader)
        try:
            assert entered.wait(5)
            assert storage_lifecycle.cleanup_old_raw_files() == 0
        finally:
            release.set()
        future.result(timeout=5)


def test_other_process_read_lock_prevents_cleanup_and_releases(temp_db: None) -> None:
    seed_media()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_shared_in_child, args=(str(storage_root()), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "ready"
        assert storage_lifecycle.cleanup_old_raw_files() == 0
    finally:
        parent.send("release")
        parent.close()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert storage_lifecycle.cleanup_old_raw_files() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_media_response_protects_actual_transfer_and_releases_after_cancel(
    temp_db: None, cancelled: bool
) -> None:
    _, _, path = seed_media()
    sending, release = asyncio.Event(), asyncio.Event()
    response = MediaFileResponse(path)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            sending.set()
            await release.wait()

    task = asyncio.create_task(
        response({"type": "http", "method": "GET", "headers": [], "extensions": {}}, receive, send)
    )
    try:
        await asyncio.wait_for(sending.wait(), timeout=5)
        assert await asyncio.to_thread(storage_lifecycle.cleanup_old_raw_files) == 0
    finally:
        if cancelled:
            task.cancel()
        else:
            release.set()
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    with media_cleanup_scope() as acquired:
        assert acquired
    assert await asyncio.to_thread(storage_lifecycle.cleanup_old_raw_files) == 1


@pytest.mark.parametrize("reason", ["low_score", "missing_event"])
def test_ineligible_candidate_does_not_probe_session_media(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    from app.db.entities import HighlightCandidate

    session_id, segment_id, _ = seed_media()
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        assert segment is not None
        candidate = HighlightCandidate(
            session_id=session_id,
            start_ts=segment.start_ts,
            peak_ts=segment.start_ts,
            end_ts=segment.end_ts,
            highlight_score=0.1 if reason == "low_score" else 1.0,
            dedup_hash="not-eligible",
        )
        db.add(candidate)
        db.flush()
        db.add(
            SegmentTask(
                segment_id=segment_id,
                session_id=session_id,
                candidate_id=candidate.id,
                event_id=1 if reason == "low_score" else None,
                stage=TaskStatus.CANDIDATE_CREATED,
            )
        )
    probes: list[str] = []
    probe = sources._probe_media

    def observe(name: str) -> sources._MediaState:
        probes.append(name)
        return probe(name)

    monkeypatch.setattr(sources, "_probe_media", observe)
    scheduler.advance_candidate()
    assert not probes
    with get_session() as db:
        assert db.exec(select(SegmentTask)).one().stage == TaskStatus.AWAITING_REVIEW


def test_event_pass_releases_preflight_before_model_calculation(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.entities import HighlightCandidate, Transcript
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.analyze import commit_highlight
    from tests.unit.test_editorial_review import START, _seed, _service

    _, segment_id = _seed(pcm_wav, heat=1.0)
    calls = _service(monkeypatch, "recommend")
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        assert segment is not None
        session_id = segment.session_id
        task = SegmentTask(
            segment_id=segment_id,
            session_id=session_id,
            stage=TaskStatus.ANALYZING,
            claimed_by="test-worker",
            lease_token="live-source",
        )
        db.add(task)
        db.flush()
        task_id = task.id
    assert task_id is not None
    probe = sources._probe_media
    appended = False

    def append_after_preflight(name: str) -> sources._MediaState:
        nonlocal appended
        state = probe(name)
        if not appended:
            appended = True
            with get_session() as db:
                late = RawSegment(
                    session_id=session_id,
                    seq=1,
                    file_path=str(pcm_wav),
                    start_ts=START + timedelta(seconds=8.336),
                    end_ts=START + timedelta(seconds=16.672),
                )
                db.add(late)
                db.flush()
                db.add(Transcript(segment_id=late.id, final_text="", words_json="[]"))
        return state

    monkeypatch.setattr(sources, "_probe_media", append_after_preflight)
    commit_highlight(
        TaskLease(task_id, "test-worker", "live-source", TaskStatus.ANALYZING),
        {"decision": "event_pass", "segment_id": segment_id, "session_id": session_id},
        0,
    )
    assert appended and calls
    with get_session() as db:
        assert db.get(SegmentTask, task_id).stage == TaskStatus.CANDIDATE_CREATED
        assert len(db.exec(select(HighlightCandidate)).all()) == 1
