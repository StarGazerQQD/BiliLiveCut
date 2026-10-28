"""渲染提交的成片关联、租约和人工审核回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.clipping.paths import build_final_clip_path, build_lease_partial_path
from app.db.entities import (
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    RawSegment,
    RenderStatus,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from app.pipeline.workers import render


def seed_render(tmp_path: Path) -> tuple[TaskLease, dict[str, Any]]:
    """建立真实来源链，并让变体与成片主键刻意错开。"""
    now = datetime.now(UTC)
    with get_session() as db:
        unrelated = FinalClip(candidate_id=999, file_path=str(tmp_path / "other.mp4"))
        db.add(unrelated)
        segment = RawSegment(session_id=1, seq=0, file_path=str(tmp_path / "raw.ts"))
        candidate = HighlightCandidate(
            session_id=1, peak_ts=now, start_ts=now, end_ts=now + timedelta(seconds=20), dedup_hash="render"
        )
        db.add(segment)
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id, session_id=1, segment_id=segment.id, review_status=ReviewStatus.APPROVED_SOLO
        )
        db.add(event)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=1,
            candidate_id=candidate.id,
            event_id=event.id,
            stage=TaskStatus.RENDERING,
            claimed_by="worker",
            lease_token="lease",
        )
        db.add(task)
        db.flush()
        lease = TaskLease(task.id, "worker", "lease", TaskStatus.RENDERING)
        temp = Path(build_lease_partial_path(task.id, lease.lease_token, session_id=1))
        temp.write_bytes(b"rendered media")
        cover = temp.with_suffix(".jpg")
        cover.write_bytes(b"rendered cover")
        result = dict(
            temp_path=str(temp),
            cover_path=str(cover),
            formal_path=build_final_clip_path(event.id, "single", "config", session_id=1),
            event_id=event.id,
            candidate_id=candidate.id,
            render_config_hash="config",
            content_hash="content",
            duration_s=20,
        )
    return lease, result


def test_render_and_reuse_reference_actual_final_clip(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        clip = db.get(FinalClip, task.clip_id)
        variant = db.exec(select(ClipVariant)).one()
        assert clip.id != variant.id and clip.candidate_id == task.candidate_id
        assert Path(clip.file_path).read_bytes() == b"rendered media"
        assert clip.cover_path == variant.cover_path == str(Path(clip.file_path).with_suffix(".jpg"))
        assert Path(clip.cover_path).read_bytes() == b"rendered cover"
        task.stage = TaskStatus.RENDERING
        task.claimed_by = lease.worker_id
        task.lease_token = lease.lease_token
        db.add(task)
        clip_id = clip.id
    Path(result["temp_path"]).write_bytes(b"unused retry")
    Path(result["cover_path"]).write_bytes(b"unused cover")
    render.commit_render(lease, result, 2)
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).clip_id == clip_id
        assert len(db.exec(select(FinalClip)).all()) == 2
        assert Path(db.get(FinalClip, clip_id).cover_path).read_bytes() == b"rendered cover"
    assert not Path(result["temp_path"]).exists() and not Path(result["cover_path"]).exists()


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("during_cover", [False, True])
def test_cancel_or_reject_during_move_cannot_be_overwritten(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject: bool, during_cover: bool
) -> None:
    lease, result = seed_render(tmp_path)
    replace = Path.replace

    def cancel_after_move(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) != result["cover_path" if during_cover else "temp_path"]:
            return moved
        if reject:
            from app.pipeline.rejection import reject_candidate_and_outputs

            with get_session() as db:
                reject_candidate_and_outputs(db, result["candidate_id"], rejected_by="test")
        else:
            from app.pipeline.task_worker import cancel_task

            assert cancel_task(lease.task_id)
        return moved

    monkeypatch.setattr(Path, "replace", cancel_after_move)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.CANCELLING
        assert db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first() is None
        if reject:
            assert db.get(HighlightCandidate, result["candidate_id"]).status == "rejected"
    assert not list(Path(result["formal_path"]).parent.glob("*.mp4"))
    assert not list(Path(result["formal_path"]).parent.glob("*.jpg"))


def test_wrong_clip_cannot_prepare_upload(temp_db: None, tmp_path: Path) -> None:
    from app.pipeline.workers.publish import prepare_publish_attempt

    lease, _ = seed_render(tmp_path)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        task.stage = TaskStatus.PUBLISHING
        task.clip_id = 1
        db.add(task)
    prepared = prepare_publish_attempt(TaskLease(lease.task_id, "worker", "lease", TaskStatus.PUBLISHING))
    assert "来源不一致" in prepared["error"] and not prepared.get("ready")


def test_recovered_variant_creates_clip_and_updates_candidate(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    media = tmp_path / "recovered.mp4"
    media.write_bytes(b"recovered")
    cover = media.with_suffix(".jpg")
    cover.write_bytes(b"recovered cover")
    with get_session() as db:
        db.add(
            ClipVariant(
                event_id=result["event_id"],
                render_config_hash="config",
                file_path=str(media),
                cover_path=str(cover),
                render_status="done",
                duration_s=20,
            )
        )
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.get(HighlightCandidate, result["candidate_id"]).status == "clipped"
        task = db.get(SegmentTask, lease.task_id)
        assert db.get(FinalClip, task.clip_id).file_path == str(media)
        assert db.get(FinalClip, task.clip_id).cover_path == str(cover)


@pytest.mark.parametrize("during_cover", [False, True])
def test_stale_generation_does_not_remove_new_worker_output(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_cover: bool
) -> None:
    lease, result = seed_render(tmp_path)
    replace = Path.replace
    new_temp = build_lease_partial_path(lease.task_id, "new-lease", session_id=1)
    new_result = {**result, "temp_path": new_temp, "cover_path": str(Path(new_temp).with_suffix(".jpg"))}
    Path(new_result["temp_path"]).write_bytes(b"new worker media")
    Path(new_result["cover_path"]).write_bytes(b"new worker cover")

    def transfer_lease(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) == result["cover_path" if during_cover else "temp_path"]:
            with get_session() as db:
                task = db.get(SegmentTask, lease.task_id)
                task.lease_token = "new-lease"
                db.add(task)
            render.commit_render(TaskLease(lease.task_id, "worker", "new-lease", TaskStatus.RENDERING), new_result, 1)
        return moved

    monkeypatch.setattr(Path, "replace", transfer_lease)
    render.commit_render(lease, result, 2)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task.stage == TaskStatus.RENDERED
        assert Path(db.get(FinalClip, task.clip_id).file_path).read_bytes() == b"new worker media"
        assert Path(db.get(FinalClip, task.clip_id).cover_path).read_bytes() == b"new worker cover"
    assert len(list(Path(result["formal_path"]).parent.glob("*.mp4"))) == 1
    assert len(list(Path(result["formal_path"]).parent.glob("*.jpg"))) == 1


@pytest.mark.parametrize("during_cover", [False, True])
def test_pair_move_failure_cleans_only_current_generation(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    during_cover: bool,
) -> None:
    lease, result = seed_render(tmp_path)
    old = Path(result["formal_path"]).with_name("old.mp4")
    old.write_bytes(b"old media")
    old.with_suffix(".jpg").write_bytes(b"old cover")
    replace = Path.replace

    def fail_move(path: Path, target: str | Path) -> Path:
        if str(path) == result["cover_path" if during_cover else "temp_path"]:
            raise OSError("interrupted file move")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_move)
    render.commit_render(lease, result, 1)
    assert set(old.parent.iterdir()) == {old, old.with_suffix(".jpg")}
    with get_session() as db:
        assert db.exec(select(ClipVariant)).one().render_status == RenderStatus.FAILED
        assert db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first() is None


def test_foreign_lease_paths_are_rejected_without_deleting_foreign_files(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    foreign = tmp_path / "other-lease.mp4"
    foreign.write_bytes(b"other media")
    foreign.with_suffix(".jpg").write_bytes(b"other cover")
    render.commit_render(lease, {**result, "temp_path": str(foreign)}, 1)
    assert foreign.read_bytes() == b"other media"
    assert foreign.with_suffix(".jpg").read_bytes() == b"other cover"


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_database_commit_checks_actual_pair_references(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    from sqlmodel import Session

    lease, result = seed_render(tmp_path)
    original = Session.commit
    calls = 0

    def commit(db: Session) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            if committed:
                original(db)
            raise SQLAlchemyError("commit result unavailable")
        original(db)

    monkeypatch.setattr(Session, "commit", commit)
    with pytest.raises(SQLAlchemyError):
        render.commit_render(lease, result, 1)
    with get_session() as db:
        clip = db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first()
        variant = db.exec(select(ClipVariant)).one()
        if committed:
            assert clip is not None and variant.render_status == RenderStatus.DONE
            assert Path(clip.file_path).is_file() and Path(clip.cover_path).is_file()
        else:
            assert clip is None and variant.render_status == RenderStatus.FAILED
            assert not Path(variant.file_path).exists() and not Path(variant.cover_path).exists()


@pytest.mark.parametrize("cover_exists", [False, True])
def test_pending_recovery_requires_declared_cover(temp_db: None, tmp_path: Path, cover_exists: bool) -> None:
    from app.pipeline.stale_recovery import recover_pending_clips

    lease, result = seed_render(tmp_path)
    media, cover = tmp_path / "ready.mp4", tmp_path / "ready.jpg"
    media.write_bytes(b"media")
    if cover_exists:
        cover.write_bytes(b"cover")
    with get_session() as db:
        db.add(ClipVariant(event_id=result["event_id"], file_path=str(media), cover_path=str(cover)))
    assert recover_pending_clips() == 1
    with get_session() as db:
        assert db.exec(select(ClipVariant)).one().render_status == (
            RenderStatus.DONE if cover_exists else RenderStatus.FAILED
        )


def test_cleanup_preserves_pair_referenced_only_by_completed_variant(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease, result = seed_render(tmp_path)
    replace = Path.replace

    def finish_variant_after_move(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) == result["temp_path"]:
            with get_session() as db:
                variant = db.exec(select(ClipVariant)).one()
                variant.render_status = RenderStatus.DONE
                db.add(variant)
                task = db.get(SegmentTask, lease.task_id)
                task.lease_token = "replacement"
                db.add(task)
        return moved

    monkeypatch.setattr(Path, "replace", finish_variant_after_move)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first() is None
        variant = db.exec(select(ClipVariant)).one()
        assert variant.render_status == RenderStatus.DONE
        assert Path(variant.file_path).read_bytes() == b"rendered media"
        assert Path(variant.cover_path).read_bytes() == b"rendered cover"


def test_cleanup_defers_pair_deletion_when_database_cannot_verify_references(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy import event

    lease, result = seed_render(tmp_path)
    with get_session() as db:
        engine = db.get_bind()

    def fail_reference_query(
        connection: object, cursor: object, statement: str, parameters: object, context: object, many: bool
    ) -> None:
        if statement.startswith("SELECT final_clips.id"):
            raise SQLAlchemyError("reference database unavailable")

    replace = Path.replace

    def fail_video_move(path: Path, target: str | Path) -> Path:
        if str(path) == result["temp_path"]:
            raise OSError("video move interrupted")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_video_move)
    event.listen(engine, "before_cursor_execute", fail_reference_query)
    try:
        render.commit_render(lease, result, 1)
    finally:
        event.remove(engine, "before_cursor_execute", fail_reference_query)
    with get_session() as db:
        variant = db.exec(select(ClipVariant)).one()
        assert Path(variant.cover_path).read_bytes() == b"rendered cover"
    assert Path(result["temp_path"]).read_bytes() == b"rendered media"
    assert "render_cleanup_deferred" in caplog.text
