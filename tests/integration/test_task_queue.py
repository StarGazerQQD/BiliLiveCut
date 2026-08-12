"""P0 测试: 状态机转换 + 幂等任务创建(V0.1.6)。"""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.db.entities import TaskStatus
from app.pipeline.stage_result import can_transition


class TestStateMachine:
    """合法/非法状态转换。"""

    @pytest.mark.parametrize(
        "src,dst,expected",
        [
            # 正向链路。
            (TaskStatus.RECORDED, TaskStatus.QUEUED_FOR_TRANS, True),
            (TaskStatus.QUEUED_FOR_TRANS, TaskStatus.TRANSCRIBING, True),
            (TaskStatus.TRANSCRIBING, TaskStatus.TRANSCRIBED, True),
            (TaskStatus.TRANSCRIBING, TaskStatus.FAILED, True),
            (TaskStatus.TRANSCRIBING, TaskStatus.TRANSIENT_FAILED, True),
            (TaskStatus.TRANSCRIBED, TaskStatus.QUEUED_FOR_ANALYSIS, True),
            (TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.ANALYZING, True),
            (TaskStatus.ANALYZING, TaskStatus.CANDIDATE_CREATED, True),
            (TaskStatus.ANALYZING, TaskStatus.COMPLETED, True),
            (TaskStatus.ANALYZING, TaskStatus.FAILED, True),
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.AWAITING_REVIEW, True),
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.REVIEWED_WAITING_ACTION, True),
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.APPROVED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.REVIEWED_WAITING_ACTION, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED, True),
            (TaskStatus.REVIEWED_WAITING_ACTION, TaskStatus.APPROVED, True),
            (TaskStatus.REVIEWED_WAITING_ACTION, TaskStatus.CANCELLED, True),
            (TaskStatus.APPROVED, TaskStatus.QUEUED_FOR_RENDER, True),
            (TaskStatus.APPROVED, TaskStatus.APPROVED_WAITING_RENDER, True),
            (TaskStatus.APPROVED_WAITING_RENDER, TaskStatus.QUEUED_FOR_RENDER, True),
            (TaskStatus.QUEUED_FOR_RENDER, TaskStatus.RENDERING, True),
            (TaskStatus.RENDERING, TaskStatus.RENDERED, True),
            (TaskStatus.RENDERING, TaskStatus.TRANSIENT_FAILED, True),
            (TaskStatus.RENDERED, TaskStatus.QUEUED_FOR_PUBLISH, True),
            (TaskStatus.RENDERED, TaskStatus.AWAITING_PUBLISH_CONFIRMATION, True),
            (TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.PUBLISHING, True),
            (TaskStatus.PUBLISHING, TaskStatus.COMPLETED, True),
            (TaskStatus.PUBLISHING, TaskStatus.TRANSIENT_FAILED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.CANCELLED, True),
            (TaskStatus.TRANSIENT_FAILED, TaskStatus.QUEUED_FOR_PUBLISH, True),
            (TaskStatus.TRANSIENT_FAILED, TaskStatus.QUEUED_FOR_TRANS, True),
            (TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED, True),
            # 非法转换。
            (TaskStatus.RECORDED, TaskStatus.TRANSCRIBING, False),
            (TaskStatus.RECORDED, TaskStatus.COMPLETED, False),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.RECORDED, False),
            (TaskStatus.COMPLETED, TaskStatus.QUEUED_FOR_TRANS, False),
            (TaskStatus.FAILED, TaskStatus.PUBLISHING, False),
            (TaskStatus.CANCELLED, TaskStatus.ANALYZING, False),
            (TaskStatus.RENDERING, TaskStatus.AWAITING_REVIEW, False),
            (TaskStatus.REVIEWED_WAITING_ACTION, TaskStatus.QUEUED_FOR_RENDER, False),
            (TaskStatus.APPROVED, TaskStatus.COMPLETED, False),
        ],
    )
    def test_transition(self, src: str, dst: str, expected: bool) -> None:
        """验证所有合法/非法转换。"""
        assert can_transition(src, dst) == expected

    def test_terminal_not_escapable(self) -> None:
        """COMPLETED/FAILED/CANCELLED 不可再转换。"""
        for terminal in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            for target in (
                TaskStatus.QUEUED_FOR_TRANS,
                TaskStatus.TRANSCRIBING,
                TaskStatus.RENDERING,
            ):
                assert can_transition(terminal, target) is False


def test_analysis_lookahead_waits_for_following_transcript(temp_db: None) -> None:
    """活动直播的当前分段必须等到下一相邻分段转写完成，避免尾部卡在断点。"""
    from datetime import UTC, datetime, timedelta

    from app.db.entities import LiveRoom, RawSegment, RecordingSession, SegmentTask, SessionStatus, Transcript
    from app.db.session import get_session
    from app.pipeline.scheduler import _analysis_lookahead_ready

    base = datetime(2026, 8, 6, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="lookahead", room_id=188, auto_analyze=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status=SessionStatus.RECORDING, started_at=base)
        db.add(session)
        db.flush()
        current = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="0.ts",
            start_ts=base,
            end_ts=base + timedelta(seconds=300),
            duration_s=300,
        )
        following = RawSegment(
            session_id=session.id,
            seq=1,
            file_path="1.ts",
            start_ts=base + timedelta(seconds=300),
            end_ts=base + timedelta(seconds=600),
            duration_s=300,
        )
        db.add(current)
        db.add(following)
        db.flush()
        task = SegmentTask(
            segment_id=current.id,
            session_id=session.id,
            stage=TaskStatus.TRANSCRIBED,
            pipeline_key=f"pipeline:{current.id}",
        )
        db.add(task)
        db.add(Transcript(segment_id=current.id, final_text="当前片段"))
        db.flush()

        assert _analysis_lookahead_ready(db, task) is False
        db.add(Transcript(segment_id=following.id, final_text="后续片段"))
        db.flush()
        assert _analysis_lookahead_ready(db, task) is True


def test_analysis_lookahead_releases_final_segment_after_session_stop(temp_db: None) -> None:
    """会话结束后没有下一分段的尾段不能永久停在 transcribed。"""
    from datetime import UTC, datetime, timedelta

    from app.db.entities import LiveRoom, RawSegment, RecordingSession, SegmentTask, SessionStatus
    from app.db.session import get_session
    from app.pipeline.scheduler import _analysis_lookahead_ready

    base = datetime(2026, 8, 6, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="final-segment", room_id=189)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status=SessionStatus.STOPPED, started_at=base, ended_at=base)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="final.ts",
            start_ts=base,
            end_ts=base + timedelta(seconds=300),
            duration_s=300,
        )
        db.add(segment)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            stage=TaskStatus.TRANSCRIBED,
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(task)
        db.flush()

        assert _analysis_lookahead_ready(db, task) is True


class TestEnqueueNext:
    """推进队列:enqueue_next。"""

    def test_valid_transition(self) -> None:
        """合法转换:RECORDED → QUEUED_FOR_TRANS。"""
        from app.db.entities import SegmentTask
        from app.pipeline.stage_result import enqueue_next

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RECORDED)
        enqueue_next(t, TaskStatus.QUEUED_FOR_TRANS)
        assert t.stage == TaskStatus.QUEUED_FOR_TRANS
        assert t.attempts == 0

    def test_invalid_transition_raises(self) -> None:
        """非法转换抛出 ValueError。"""
        from app.db.entities import SegmentTask
        from app.pipeline.stage_result import enqueue_next

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RECORDED)
        with pytest.raises(ValueError, match="非法"):
            enqueue_next(t, TaskStatus.COMPLETED)


# ════════════════════════════════════════════════════
# V0.1.11-alpha: 重试和 failed_stage
# ════════════════════════════════════════════════════


class TestAttempts:
    """attempts 只在开始执行时增加一次 (V0.1.11-alpha)。"""

    def test_mark_active_increments_once(self) -> None:
        """mark_active 只增一次 attempts。"""
        from app.db.entities import SegmentTask
        from app.pipeline.stage_result import mark_active

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.TRANSCRIBING)
        assert t.attempts == 0
        mark_active(t)
        assert t.attempts == 1

    def test_enqueue_next_resets_attempts(self) -> None:
        """enqueue_next 重置 attempts 为 0。"""
        from app.db.entities import SegmentTask
        from app.pipeline.stage_result import enqueue_next

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RECORDED, attempts=3)
        enqueue_next(t, TaskStatus.QUEUED_FOR_TRANS)
        assert t.attempts == 0


class TestFailedStage:
    """V0.1.11-alpha: failed_stage 精确恢复。"""

    def test_resume_stage_from_transcribing(self) -> None:
        """TRANSCRIBING → QUEUED_FOR_TRANS。"""
        from app.pipeline.stale_recovery import resume_stage

        assert resume_stage(TaskStatus.TRANSCRIBING) == TaskStatus.QUEUED_FOR_TRANS

    def test_resume_stage_from_analyzing(self) -> None:
        """ANALYZING → QUEUED_FOR_ANALYSIS。"""
        from app.pipeline.stale_recovery import resume_stage

        assert resume_stage(TaskStatus.ANALYZING) == TaskStatus.QUEUED_FOR_ANALYSIS

    def test_resume_stage_from_rendering(self) -> None:
        """RENDERING → QUEUED_FOR_RENDER。"""
        from app.pipeline.stale_recovery import resume_stage

        assert resume_stage(TaskStatus.RENDERING) == TaskStatus.QUEUED_FOR_RENDER

    def test_resume_stage_none_defaults_trans(self) -> None:
        """None → QUEUED_FOR_TRANS (安全默认)。"""
        from app.pipeline.stale_recovery import resume_stage

        assert resume_stage(None) == TaskStatus.QUEUED_FOR_TRANS

    def test_mark_failed_records_failed_stage(self) -> None:
        """mark_failed 记录 failed_stage。"""
        from app.db.entities import SegmentTask
        from app.pipeline.stage_result import mark_failed

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RENDERING, attempts=1)
        mark_failed(t, "test error", permanent=False)
        assert t.failed_stage == TaskStatus.RENDERING
        assert t.stage == TaskStatus.TRANSIENT_FAILED


class TestRetry:
    """V0.1.11-alpha: 重试恢复逻辑。"""

    def test_retry_from_failed_stage(self) -> None:
        """TRANSIENT_FAILED(TRANSCRIBING) → QUEUED_FOR_TRANS。"""
        from app.pipeline.stale_recovery import resume_stage

        assert resume_stage(TaskStatus.TRANSCRIBING) == TaskStatus.QUEUED_FOR_TRANS

    def test_retry_uses_failed_stage(self) -> None:
        """失败阶段决定恢复后的排队阶段。"""
        from app.pipeline.stale_recovery import resume_stage

        # failed_stage=ANALYZING → QUEUED_FOR_ANALYSIS
        assert resume_stage(TaskStatus.ANALYZING) == TaskStatus.QUEUED_FOR_ANALYSIS


def test_recover_orphans_handles_existing_task_ids(temp_db: None) -> None:
    """孤儿恢复应正确处理标量 segment_id 查询并只补建缺失任务。"""
    from app.db.entities import RawSegment, SegmentStatus, SegmentTask
    from app.db.session import get_session
    from app.pipeline.stale_recovery import recover_orphans

    with get_session() as db:
        db.add_all(
            [
                RawSegment(id=4101, session_id=5101, seq=0, file_path="existing.ts", status=SegmentStatus.RECORDED),
                RawSegment(id=4102, session_id=5101, seq=1, file_path="orphan.ts", status=SegmentStatus.RECORDED),
                SegmentTask(segment_id=4101, session_id=5101, stage=TaskStatus.RECORDED),
            ]
        )

    recover_orphans()

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).order_by(SegmentTask.segment_id)).all()
        assert [task.segment_id for task in tasks] == [4101, 4102]


# ════════════════════════════════════════════════════
# V0.1.11-alpha: 模型一致性
# ════════════════════════════════════════════════════


class TestDataModelConsistency:
    """V0.1.11-alpha: 数据模型语义校验。"""

    def test_highlight_topic_has_confirmed_by_user(self) -> None:
        """HighlightTopic 有 confirmed_by_user 字段。"""
        from app.db.entities import HighlightTopic

        assert hasattr(HighlightTopic, "confirmed_by_user")

    def test_segment_task_has_failed_stage(self) -> None:
        """SegmentTask 有 failed_stage 字段。"""
        from app.db.entities import SegmentTask

        assert hasattr(SegmentTask, "failed_stage")

    def test_segment_task_has_heartbeat_at(self) -> None:
        """SegmentTask 有 heartbeat_at 字段。"""
        from app.db.entities import SegmentTask

        assert hasattr(SegmentTask, "heartbeat_at")

    def test_segment_task_has_event_id(self) -> None:
        """SegmentTask 有 event_id 字段。"""
        from app.db.entities import SegmentTask

        assert hasattr(SegmentTask, "event_id")

    def test_task_status_has_stale(self) -> None:
        """TaskStatus 有 STALE 状态。"""
        from app.db.entities import TaskStatus

        assert hasattr(TaskStatus, "STALE")


def test_resolve_event_id_returns_existing_event(temp_db: None) -> None:
    """_resolve_event_id:找到已有 Event 时返回其 ID (不创建新)。"""
    import datetime

    from app.db.entities import HighlightCandidate, HighlightEvent, ReviewStatus
    from app.db.session import get_session

    with get_session() as db:
        cand = HighlightCandidate(
            session_id=1,
            peak_ts=datetime.datetime(2025, 1, 1, 12, 0),
            start_ts=datetime.datetime(2025, 1, 1, 11, 59),
            end_ts=datetime.datetime(2025, 1, 1, 12, 1),
            highlight_score=0.75,
            dedup_hash="resolve-existing-event",
        )
        db.add(cand)
        db.flush()
        event = HighlightEvent(
            candidate_id=cand.id,
            session_id=1,
            review_status=ReviewStatus.APPROVED_SOLO,
            review_by="manual",
        )
        db.add(event)
        db.flush()
        db.refresh(event)

    from app.clipping.clipper import _resolve_event_id

    with get_session() as db:
        resolved = _resolve_event_id(db, cand.id)
    assert resolved == event.id
