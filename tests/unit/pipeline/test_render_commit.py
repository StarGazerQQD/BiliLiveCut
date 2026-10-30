"""渲染提交的成片关联、租约和人工审核回归。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
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
from app.pipeline.render_checkpoint import render_input_fingerprint, save_checkpoint, verify_media
from app.pipeline.workers import render


def write_media_pair(path: Path, color: str = "blue") -> None:
    from app.core.config import settings

    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=96x64:r=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:a",
            "aac",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(path.with_suffix(".jpg")),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


def content_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seed_render(tmp_path: Path) -> tuple[TaskLease, dict[str, Any]]:
    """建立真实来源链，并让变体与成片主键刻意错开。"""
    now = datetime.now(UTC)
    with get_session() as db:
        unrelated = FinalClip(candidate_id=999, file_path=str(tmp_path / "other.mp4"))
        db.add(unrelated)
        segment = RawSegment(
            session_id=1, seq=0, file_path=str(tmp_path / "raw.ts"), start_ts=now, end_ts=now + timedelta(seconds=2)
        )
        candidate = HighlightCandidate(
            session_id=1, peak_ts=now, start_ts=now, end_ts=now + timedelta(seconds=2), dedup_hash="render"
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
        write_media_pair(temp)
        cover = temp.with_suffix(".jpg")
        Path(segment.file_path).write_bytes(temp.read_bytes())
        config_hash = render_input_fingerprint(db, task)
        media = verify_media(str(temp))
        result = dict(
            temp_path=str(temp),
            cover_path=str(cover),
            formal_path=build_final_clip_path(event.id, "single", config_hash, session_id=1),
            event_id=event.id,
            candidate_id=candidate.id,
            render_config_hash=config_hash,
            content_hash=media.facts.sha256,
            duration_s=media.facts.duration,
            cover_hash=content_hash(cover),
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
        assert content_hash(clip.file_path) == result["content_hash"]
        assert (clip.width, clip.height) == (96, 64)
        assert clip.cover_path == variant.cover_path == str(Path(clip.file_path).with_suffix(".jpg"))
        assert content_hash(clip.cover_path) == result["cover_hash"]
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
        assert content_hash(db.get(FinalClip, clip_id).cover_path) == result["cover_hash"]
    assert not Path(result["temp_path"]).exists() and not Path(result["cover_path"]).exists()


@pytest.mark.parametrize("interrupted", [False, True])
def test_shutdown_preserves_render_state_and_removes_only_lease_files(
    temp_db: None, tmp_path: Path, interrupted: bool
) -> None:
    from app.core.cancellation import OperationShutdownError, shutdown_event
    from app.core.process_control import ProcessShutdownError

    lease, result = seed_render(tmp_path)
    if interrupted:
        result["interruption"] = ProcessShutdownError("shutdown at media boundary")
    shutdown_event.set()
    try:
        with pytest.raises((OperationShutdownError, ProcessShutdownError)):
            render.commit_render(lease, result, 1)
    finally:
        shutdown_event.clear()
    assert not Path(result["temp_path"]).exists() and not Path(result["cover_path"]).exists()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task.stage == TaskStatus.RENDERING and task.lease_token == lease.lease_token
        assert task.last_error is None
        assert len(db.exec(select(FinalClip)).all()) == 1
        assert not db.exec(select(ClipVariant)).all()


@pytest.mark.parametrize("interruption", ["cancel", "reject", "shutdown"])
@pytest.mark.parametrize("during_cover", [False, True])
def test_interruption_during_move_cannot_be_overwritten(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str, during_cover: bool
) -> None:
    from app.core.cancellation import OperationShutdownError, shutdown_event

    lease, result = seed_render(tmp_path)
    replace = Path.replace

    def cancel_after_move(path: Path, target: str | Path) -> Path:
        moved = replace(path, target)
        if str(path) != result["cover_path" if during_cover else "temp_path"]:
            return moved
        if interruption == "shutdown":
            shutdown_event.set()
        elif interruption == "reject":
            from app.pipeline.rejection import reject_candidate_and_outputs

            with get_session() as db:
                reject_candidate_and_outputs(db, result["candidate_id"], rejected_by="test")
        else:
            from app.pipeline.task_worker import cancel_task

            assert cancel_task(lease.task_id)
        return moved

    monkeypatch.setattr(Path, "replace", cancel_after_move)
    try:
        if interruption == "shutdown":
            with pytest.raises(OperationShutdownError):
                render.commit_render(lease, result, 1)
        else:
            render.commit_render(lease, result, 1)
    finally:
        shutdown_event.clear()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if interruption == "shutdown":
            assert task.stage == TaskStatus.RENDERING and task.lease_token == lease.lease_token
            assert task.attempts == 0 and task.last_error is None
        else:
            assert task.stage == TaskStatus.CANCELLING
        assert db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).first() is None
        if interruption == "reject":
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
    media.write_bytes(Path(result["temp_path"]).read_bytes())
    cover = media.with_suffix(".jpg")
    cover.write_bytes(Path(result["cover_path"]).read_bytes())
    proof, cover_proof = verify_media(str(media)), verify_media(str(cover), cover=True)
    with get_session() as db:
        variant = ClipVariant(
            event_id=result["event_id"],
            render_config_hash=result["render_config_hash"],
            file_path=str(media),
            file_hash=proof.facts.sha256,
            cover_path=str(cover),
            render_status="done",
            duration_s=proof.facts.duration,
        )
        db.add(variant)
        db.flush()
        save_checkpoint(db, lease, variant, result["candidate_id"], proof, cover_proof)
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
    write_media_pair(Path(new_result["temp_path"]), "red")
    new_result["content_hash"] = content_hash(new_result["temp_path"])
    new_result["cover_hash"] = content_hash(new_result["cover_path"])

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
        assert content_hash(db.get(FinalClip, task.clip_id).file_path) == new_result["content_hash"]
        assert content_hash(db.get(FinalClip, task.clip_id).cover_path) == new_result["cover_hash"]
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
        if db.dirty or db.new:
            calls += 1
        if calls == 2 and (db.dirty or db.new):
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
    media.write_bytes(Path(result["temp_path"]).read_bytes())
    cover.write_bytes(Path(result["cover_path"]).read_bytes())
    proof, cover_proof = verify_media(str(media)), verify_media(str(cover), cover=True)
    if not cover_exists:
        cover.unlink()
    with get_session() as db:
        variant = ClipVariant(
            event_id=result["event_id"],
            file_path=str(media),
            cover_path=str(cover),
            render_config_hash=result["render_config_hash"],
            file_hash=proof.facts.sha256,
            duration_s=proof.facts.duration,
        )
        db.add(variant)
        db.flush()
        save_checkpoint(db, lease, variant, result["candidate_id"], proof, cover_proof)
        task = db.get(SegmentTask, lease.task_id)
        task.stage = TaskStatus.QUEUED_FOR_RENDER
        task.claimed_by = task.lease_token = None
        db.add(task)
    assert recover_pending_clips() == 1
    with get_session() as db:
        assert db.exec(select(ClipVariant)).one().render_status == (
            RenderStatus.QUEUED if cover_exists else RenderStatus.FAILED
        )
        assert db.get(SegmentTask, lease.task_id).clip_id is None
        if cover_exists:
            task = db.get(SegmentTask, lease.task_id)
            task.stage, task.claimed_by, task.lease_token = TaskStatus.RENDERING, "new-worker", "new-lease"
            db.add(task)
    if cover_exists:
        assert render.resume_render(TaskLease(lease.task_id, "new-worker", "new-lease", TaskStatus.RENDERING))
        with get_session() as db:
            task = db.get(SegmentTask, lease.task_id)
            assert task.stage == TaskStatus.RENDERED
            assert db.get(FinalClip, task.clip_id).file_path == str(media)


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
        assert content_hash(variant.file_path) == result["content_hash"]
        assert content_hash(variant.cover_path) == result["cover_hash"]


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
        assert content_hash(variant.cover_path) == result["cover_hash"]
    assert content_hash(result["temp_path"]) == result["content_hash"]
    assert "render_cleanup_deferred" in caplog.text


@pytest.mark.parametrize("change", ["truncate", "replace", "cover", "options", "candidate", "source"])
def test_resume_rejects_changed_media_or_inputs(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from app.core.config import settings

    lease, result = seed_render(tmp_path)
    render.commit_render(lease, result, 1)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        clip = db.get(FinalClip, task.clip_id)
        video_path, cover_path = Path(clip.file_path), Path(clip.cover_path)
        if change == "candidate":
            candidate = db.get(HighlightCandidate, task.candidate_id)
            candidate.end_ts += timedelta(seconds=1)
            db.add(candidate)
        if change == "source":
            segment = db.get(RawSegment, task.segment_id)
            Path(segment.file_path).write_bytes(b"changed original input")
        task.stage, task.claimed_by, task.lease_token = TaskStatus.RENDERING, "next-worker", "next-lease"
        db.add(task)
    if change == "truncate":
        video_path.write_bytes(video_path.read_bytes()[:512])
    elif change == "replace":
        write_media_pair(video_path, "red")
    elif change == "cover":
        cover_path.write_bytes(b"invalid jpg")
    elif change == "options":
        monkeypatch.setattr(settings, "clip_vertical", not settings.clip_vertical)
    assert not render.resume_render(TaskLease(lease.task_id, "next-worker", "next-lease", TaskStatus.RENDERING))
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.RENDERING
        assert len(db.exec(select(FinalClip)).all()) == 2


def test_invalid_video_cannot_be_committed(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    Path(result["temp_path"]).write_bytes(b"not video" * 300)
    result["content_hash"] = content_hash(result["temp_path"])
    render.commit_render(lease, result, 1)
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.FAILED
        assert db.exec(select(ClipVariant)).first() is None


def test_real_render_compute_and_recovery_keep_media_checks_outside_write_transaction(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    lease, _ = seed_render(tmp_path)
    with get_session() as db:
        engine = db.get_bind()
    write_transactions: set[object] = set()

    def observe_sql(
        connection: object, cursor: object, statement: str, parameters: object, context: object, many: bool
    ) -> None:
        if statement == "BEGIN IMMEDIATE":
            write_transactions.add(connection)

    def closed(connection: object) -> None:
        write_transactions.discard(connection)

    popen = subprocess.Popen

    def observe_process(*args: Any, **kwargs: Any) -> subprocess.Popen:
        assert not write_transactions
        return popen(*args, **kwargs)

    # 只在 OS 边界观察，仍启动真实 FFmpeg/ffprobe。
    monkeypatch.setattr(subprocess, "Popen", observe_process)
    event.listen(engine, "before_cursor_execute", observe_sql)
    event.listen(engine, "commit", closed)
    event.listen(engine, "rollback", closed)
    try:
        result = render.render_compute(lease)
        assert "error" not in result and result["width"] == 96 and result["height"] == 64
        render.commit_render(lease, result, 1)
        with get_session() as db:
            task = db.get(SegmentTask, lease.task_id)
            assert task.stage == TaskStatus.RENDERED
            task.stage, task.claimed_by, task.lease_token = TaskStatus.RENDERING, "next-worker", "next-lease"
            db.add(task)
        assert render.resume_render(TaskLease(lease.task_id, "next-worker", "next-lease", TaskStatus.RENDERING))
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
        event.remove(engine, "commit", closed)
        event.remove(engine, "rollback", closed)


def test_process_exit_after_move_recovers_once_without_rerender(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.claiming import pop_and_claim
    from app.pipeline.stale_recovery import recover_pending_clips, recover_stale

    lease, result = seed_render(tmp_path)
    payload = tmp_path / "render-crash.json"
    payload.write_text(json.dumps({"lease": asdict(lease), "result": result}), encoding="utf-8")
    root = Path(__file__).resolve().parents[3]
    actor = root / "tests" / "integration" / "render_crash_actor.py"
    completed = subprocess.run(
        [sys.executable, str(actor), str(payload)],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 23, completed.stderr.decode(errors="replace")
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        variant = db.exec(select(ClipVariant)).one()
        assert task.clip_id is None and variant.render_status == RenderStatus.QUEUED
        assert content_hash(variant.file_path) == result["content_hash"]
        assert content_hash(variant.cover_path) == result["cover_hash"]
        task.heartbeat_at = datetime.now(UTC) - timedelta(days=1)
        db.add(task)
    recover_stale()
    assert recover_pending_clips() == 1
    claim = pop_and_claim(TaskStatus.QUEUED_FOR_RENDER)
    assert claim is not None and claim.id == lease.task_id
    new_lease = TaskLease(claim.id, claim.claimed_by, claim.lease_token, TaskStatus.RENDERING)
    commands: list[str] = []
    popen = subprocess.Popen

    def observe_process(args: list[str], *positional: Any, **kwargs: Any) -> subprocess.Popen:
        commands.append(Path(args[0]).name)
        return popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", observe_process)
    render.run_render(new_lease)
    assert commands and all("ffprobe" in command for command in commands)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task.stage == TaskStatus.RENDERED
        clips = db.exec(select(FinalClip).where(FinalClip.candidate_id == result["candidate_id"])).all()
        assert len(clips) == 1 and clips[0].id == task.clip_id
        assert db.exec(select(ClipVariant)).one().generation == 1
    assert recover_pending_clips() == 0


@pytest.mark.parametrize("change", ["transcript", "subtitle_template", "intro_template", "danmaku", "danmaku_lag"])
def test_render_fingerprint_tracks_effective_text_inputs(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from app.core.config import settings
    from app.db.entities import AppSetting, Danmaku, IntroTemplate, SubtitleTemplate, Transcript

    lease, _ = seed_render(tmp_path)
    monkeypatch.setattr(settings, "clip_subtitle", True)
    monkeypatch.setattr(settings, "clip_danmaku", True)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        before = render_input_fingerprint(db, task)
        candidate = db.get(HighlightCandidate, task.candidate_id)
        if change == "transcript":
            db.add(Transcript(segment_id=task.segment_id, final_text="字幕变化"))
        elif change == "subtitle_template":
            db.add(SubtitleTemplate(name="恢复断句", is_default=True, max_chars_per_line=10))
        elif change == "intro_template":
            db.add(IntroTemplate(name="恢复片头", is_default=True, intro_enabled=True, intro_text="新片头"))
        elif change == "danmaku":
            db.add(Danmaku(session_id=1, room_id=1, ts=candidate.start_ts + timedelta(seconds=1), content="新弹幕"))
        else:
            from app.plugins.live_source import DanmakuStatus
            from app.recording.danmaku import DanmakuEvidence

            db.add(
                AppSetting(
                    key="session_danmaku:1",
                    value=DanmakuEvidence(
                        status=DanmakuStatus.AVAILABLE,
                        lag_s=15,
                    ).model_dump_json(),
                )
            )
        db.flush()
        assert render_input_fingerprint(db, task) != before


async def test_worker_start_checks_interrupted_variant_before_scheduling(temp_db: None, tmp_path: Path) -> None:
    from app.pipeline.task_worker import TaskWorker

    lease, result = seed_render(tmp_path)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        task.stage, task.claimed_by, task.lease_token = TaskStatus.QUEUED_FOR_RENDER, None, None
        db.add(task)
        db.add(ClipVariant(event_id=result["event_id"], file_path=str(tmp_path / "unverified.mp4")))
    worker = TaskWorker()
    await worker.start()
    try:
        with get_session() as db:
            assert db.exec(select(ClipVariant)).one().render_status == RenderStatus.FAILED
    finally:
        await worker.stop()
