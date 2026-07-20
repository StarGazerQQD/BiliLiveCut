"""转写阶段提交与幂等回归测试。"""

from __future__ import annotations

from sqlmodel import select


def _seed_claimed_task() -> tuple[int, int]:
    """创建一个持有有效租约的转写任务，返回 ``(task_id, segment_id)``。"""
    from app.db.models import RawSegment, SegmentTask, TaskStatus
    from app.db.session import get_session

    with get_session() as db:
        segment = RawSegment(session_id=1, seq=0, file_path="test.ts")
        db.add(segment)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=1,
            stage=TaskStatus.TRANSCRIBING,
            claimed_by="worker-test",
            lease_token="lease-test",
        )
        db.add(task)
        db.flush()
        return task.id, segment.id


def _lease(task_id: int):  # noqa: ANN202
    """构造与 ``_seed_claimed_task`` 匹配的租约。"""
    from app.db.models import TaskStatus
    from app.pipeline.lease import TaskLease

    return TaskLease(
        task_id=task_id,
        worker_id="worker-test",
        lease_token="lease-test",
        expected_stage=TaskStatus.TRANSCRIBING,
    )


def test_commit_transcript_advances_without_nonexistent_task_field(temp_db: None) -> None:
    """新转写应落库并推进任务，不依赖不存在的 ``task.transcript_id``。"""
    from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id = _seed_claimed_task()
    commit_transcript(
        _lease(task_id),
        {
            "segment_id": segment_id,
            "text": "测试转写",
            "final_text": "测试转写",
            "language": "zh",
            "words_json": "[]",
        },
        12,
    )

    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        task = db.get(SegmentTask, task_id)
        segment = db.get(RawSegment, segment_id)
        assert transcript.text == "测试转写"
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        assert segment is not None and segment.status == SegmentStatus.TRANSCRIBED


def test_commit_transcript_reuses_existing_transcript(temp_db: None) -> None:
    """幂等重试应复用已有转写并修复片段、任务状态。"""
    from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id = _seed_claimed_task()
    with get_session() as db:
        db.add(Transcript(segment_id=segment_id, text="已有转写"))

    commit_transcript(_lease(task_id), {"segment_id": segment_id, "text": "不应重复写入"}, 8)

    with get_session() as db:
        transcripts = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).all()
        task = db.get(SegmentTask, task_id)
        segment = db.get(RawSegment, segment_id)
        assert [item.text for item in transcripts] == ["已有转写"]
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        assert segment is not None and segment.status == SegmentStatus.TRANSCRIBED
