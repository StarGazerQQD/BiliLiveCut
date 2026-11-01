from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import select

from app.core.cancellation import shutdown_event
from app.core.media_usage import media_reference_scope
from app.core.paths import clips_dir
from app.db.entities import AppSetting, CandidateStatus, ClipStatus, FinalClip, HighlightCandidate
from app.db.session import get_session
from app.pipeline import storage_lifecycle


def _seed(count: int, *, single_candidate: bool = False, covers: bool = False) -> list[Path]:
    now = datetime(2026, 11, 1)
    files: list[Path] = []
    with get_session() as db:
        for index in range(count):
            candidate_id = 1 if single_candidate else index + 1
            if not single_candidate or index == 0:
                db.add(
                    HighlightCandidate(
                        id=candidate_id,
                        session_id=1,
                        start_ts=now,
                        peak_ts=now,
                        end_ts=now,
                        dedup_hash=f"cleanup-{candidate_id}",
                        status=CandidateStatus.REJECTED,
                    )
                )
            path = clips_dir() / f"clip-{index}.mp4"
            path.write_bytes(b"real file")
            cover = path.with_suffix(".jpg") if covers else None
            if cover is not None:
                cover.write_bytes(b"real cover")
            db.add(
                FinalClip(
                    id=index + 1,
                    candidate_id=candidate_id,
                    file_path=str(path),
                    cover_path=str(cover) if cover else None,
                )
            )
            files.append(path)
    return files


@pytest.mark.parametrize("single_candidate", [False, True])
def test_bounded_batches_finish_large_candidates_without_repeating_successful_files(
    temp_db: None, single_candidate: bool
) -> None:
    files = _seed(70, single_candidate=single_candidate)
    assert storage_lifecycle.cleanup_rejected_candidates() == 32
    assert sum(path.exists() for path in files) == 38
    with get_session() as db:
        if single_candidate:
            assert db.get(HighlightCandidate, 1).status == CandidateStatus.REJECTED
    with media_reference_scope():
        assert storage_lifecycle.cleanup_rejected_candidates() == 0
    assert storage_lifecycle.cleanup_rejected_candidates() == 32
    assert storage_lifecycle.cleanup_rejected_candidates() == 6
    assert not any(path.exists() for path in files)
    with get_session() as db:
        assert all(candidate.status == CandidateStatus.CLEANED for candidate in db.exec(select(HighlightCandidate)))


def test_empty_candidates_share_scan_budget(temp_db: None) -> None:
    now = datetime(2026, 11, 1)
    with get_session() as db:
        db.add_all(
            [
                HighlightCandidate(
                    session_id=1,
                    start_ts=now,
                    peak_ts=now,
                    end_ts=now,
                    dedup_hash=str(index),
                    status=CandidateStatus.REJECTED,
                )
                for index in range(70)
            ]
        )
    assert storage_lifecycle.cleanup_rejected_candidates() == 0
    with get_session() as db:
        assert (
            len(db.exec(select(HighlightCandidate).where(HighlightCandidate.status == CandidateStatus.CLEANED)).all())
            == 32
        )
    storage_lifecycle.cleanup_rejected_candidates()
    storage_lifecycle.cleanup_rejected_candidates()
    with get_session() as db:
        assert not db.exec(
            select(HighlightCandidate).where(HighlightCandidate.status == CandidateStatus.REJECTED)
        ).all()


@pytest.mark.parametrize("protected", [True, False])
def test_protected_or_failed_prefix_does_not_starve_later_files_and_can_be_revisited(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, protected: bool
) -> None:
    files = _seed(35)
    original = Path.unlink
    blocked = True

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if not protected and blocked and path in files[:33]:
            raise PermissionError("external lock")
        original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    if protected:
        with get_session() as db:
            for clip in db.exec(select(FinalClip).where(FinalClip.id <= 33)):
                clip.status = ClipStatus.READY
                db.add(clip)
    assert storage_lifecycle.cleanup_rejected_candidates() == 0
    assert storage_lifecycle.cleanup_rejected_candidates() == 2
    assert all(path.exists() for path in files[:33]) and not any(path.exists() for path in files[33:])
    blocked = False
    if protected:
        with get_session() as db:
            for clip in db.exec(select(FinalClip).where(FinalClip.id <= 33)):
                clip.status = ClipStatus.REJECTED
                db.add(clip)
    assert storage_lifecycle.cleanup_rejected_candidates() == 0
    assert storage_lifecycle.cleanup_rejected_candidates() == 32
    assert storage_lifecycle.cleanup_rejected_candidates() == 1


@pytest.mark.parametrize("interruption", ["cover_failure", "shutdown"])
def test_partial_clip_deletion_is_retryable_and_never_prematurely_completed(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    files = _seed(2, single_candidate=True, covers=True)
    original = Path.unlink
    interrupted = False

    def unlink(path: Path, missing_ok: bool = False) -> None:
        nonlocal interrupted
        if not interrupted and interruption == "cover_failure" and path == files[0].with_suffix(".jpg"):
            interrupted = True
            raise PermissionError("locked cover")
        original(path, missing_ok=missing_ok)
        if not interrupted and interruption == "shutdown" and path == files[0]:
            interrupted = True
            shutdown_event.set()

    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        assert storage_lifecycle.cleanup_rejected_candidates() == (1 if interruption == "shutdown" else 2)
        assert files[0].with_suffix(".jpg").exists()
        if interruption == "shutdown":
            assert files[1].exists()
        with get_session() as db:
            assert db.get(HighlightCandidate, 1).status == CandidateStatus.REJECTED
    finally:
        shutdown_event.clear()
    for _ in range(3):
        storage_lifecycle.cleanup_rejected_candidates()
    with get_session() as db:
        assert db.get(HighlightCandidate, 1).status == CandidateStatus.CLEANED
    assert all(not path.exists() and not path.with_suffix(".jpg").exists() for path in files)


def test_new_clip_and_changed_cover_between_batches_require_matching_receipts(temp_db: None) -> None:
    files = _seed(33, single_candidate=True)
    assert storage_lifecycle.cleanup_rejected_candidates() == 32
    new_cover = clips_dir() / "new-cover.jpg"
    new_cover.write_bytes(b"new cover")
    new_clip = clips_dir() / "new-clip.mp4"
    new_clip.write_bytes(b"new clip")
    with get_session() as db:
        clip = db.get(FinalClip, 1)
        assert clip is not None
        clip.cover_path = str(new_cover)
        db.add(clip)
        db.add(FinalClip(candidate_id=1, file_path=str(new_clip)))
    assert storage_lifecycle.cleanup_rejected_candidates() == 2
    with get_session() as db:
        assert db.get(HighlightCandidate, 1).status == CandidateStatus.REJECTED
    assert new_cover.exists() and not new_clip.exists() and not files[-1].exists()
    storage_lifecycle.cleanup_rejected_candidates()
    assert storage_lifecycle.cleanup_rejected_candidates() == 1
    with get_session() as db:
        assert db.get(HighlightCandidate, 1).status == CandidateStatus.REJECTED
    assert storage_lifecycle.cleanup_rejected_candidates() == 0
    with get_session() as db:
        assert db.get(HighlightCandidate, 1).status == CandidateStatus.CLEANED
    assert not new_cover.exists()


def test_failed_database_commit_does_not_advance_cursor_or_claim_completion(temp_db: None) -> None:
    from app.db.session import engine

    files = _seed(2)
    fail_commit = False

    def observe(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal fail_commit
        if statement.startswith("INSERT INTO app_settings") and "rejected_cleanup:cursor" in str(parameters):
            fail_commit = True

    def commit(connection: Connection) -> None:
        if fail_commit:
            raise RuntimeError("simulated database commit failure")

    event.listen(engine, "after_cursor_execute", observe)
    event.listen(engine, "commit", commit)
    try:
        with pytest.raises(RuntimeError, match="database commit failure"):
            storage_lifecycle.cleanup_rejected_candidates()
    finally:
        event.remove(engine, "after_cursor_execute", observe)
        event.remove(engine, "commit", commit)
    assert not any(path.exists() for path in files)
    with get_session() as db:
        assert db.get(AppSetting, "rejected_cleanup:cursor") is None
        assert all(candidate.status == CandidateStatus.REJECTED for candidate in db.exec(select(HighlightCandidate)))
    assert storage_lifecycle.cleanup_rejected_candidates() == 2
    with get_session() as db:
        assert all(candidate.status == CandidateStatus.CLEANED for candidate in db.exec(select(HighlightCandidate)))


def test_slow_deletion_releases_database_and_batch_releases_media_gate(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _seed(40)
    entered, release, reader_started = Event(), Event(), Event()
    original = Path.unlink

    def unlink(path: Path, missing_ok: bool = False) -> None:
        if path == files[0]:
            entered.set()
            assert release.wait(5)
        original(path, missing_ok=missing_ok)

    def reader() -> None:
        reader_started.set()
        with media_reference_scope():
            assert files[-1].exists()

    monkeypatch.setattr(Path, "unlink", unlink)
    with ThreadPoolExecutor(max_workers=2) as executor:
        cleanup = executor.submit(storage_lifecycle.cleanup_rejected_candidates)
        try:
            assert entered.wait(5)
            with get_session() as db:
                db.add(AppSetting(key="unrelated_write", value="committed during deletion"))
            waiting = executor.submit(reader)
            assert reader_started.wait(5) and not waiting.done()
        finally:
            release.set()
        assert cleanup.result(timeout=5) == 32
        waiting.result(timeout=5)
    assert sum(path.exists() for path in files) == 8
