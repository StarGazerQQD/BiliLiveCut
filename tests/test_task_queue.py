"""P0 测试: 状态机转换 + 幂等任务创建(V0.1.6)。"""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.db.models import TaskStatus

# ---- 状态机 ----
_VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.RECORDED: {TaskStatus.QUEUED_FOR_TRANS},
    TaskStatus.QUEUED_FOR_TRANS: {TaskStatus.TRANSCRIBING},
    TaskStatus.TRANSCRIBING: {TaskStatus.TRANSCRIBED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSCRIBED: {TaskStatus.QUEUED_FOR_ANALYSIS},
    TaskStatus.QUEUED_FOR_ANALYSIS: {TaskStatus.ANALYZING},
    TaskStatus.ANALYZING: {TaskStatus.CANDIDATE_CREATED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.CANDIDATE_CREATED: {TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.APPROVED, TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.APPROVED_WAITING_RENDER, TaskStatus.QUEUED_FOR_RENDER},
    TaskStatus.APPROVED_WAITING_RENDER: {TaskStatus.QUEUED_FOR_RENDER, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_RENDER: {TaskStatus.RENDERING},
    TaskStatus.RENDERING: {TaskStatus.RENDERED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.RENDERED: {TaskStatus.AWAITING_PUBLISH_CONFIRMATION, TaskStatus.QUEUED_FOR_PUBLISH},
    TaskStatus.AWAITING_PUBLISH_CONFIRMATION: {TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_PUBLISH: {TaskStatus.PUBLISHING},
    TaskStatus.PUBLISHING: {TaskStatus.COMPLETED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSIENT_FAILED: {
        TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.FAILED,
    },  # noqa: E501
}


def _can_transition(current: str, target: str) -> bool:
    return target in _VALID_TRANSITIONS.get(current, set())


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
            (TaskStatus.ANALYZING, TaskStatus.FAILED, True),
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.AWAITING_REVIEW, True),
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.APPROVED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED, True),
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
            (TaskStatus.APPROVED, TaskStatus.COMPLETED, False),
        ],
    )
    def test_transition(self, src: str, dst: str, expected: bool) -> None:
        """验证所有合法/非法转换。"""
        assert _can_transition(src, dst) == expected

    def test_terminal_not_escapable(self) -> None:
        """COMPLETED/FAILED/CANCELLED 不可再转换。"""
        for terminal in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            for target in (
                TaskStatus.QUEUED_FOR_TRANS,
                TaskStatus.TRANSCRIBING,
                TaskStatus.RENDERING,
            ):
                assert _can_transition(terminal, target) is False


class TestIdempotencyKey:
    """幂等键格式。"""

    def test_key_format(self) -> None:
        """幂等键格式: segment_id:stage。"""
        from app.pipeline.task_worker import _make_idempotency_key

        key = _make_idempotency_key(42, "recorded")
        assert key == "42:recorded"

    def test_different_stage_differs(self) -> None:
        """同 segment 不同阶段幂等键不同。"""
        from app.pipeline.task_worker import _make_idempotency_key

        k1 = _make_idempotency_key(1, "recorded")
        k2 = _make_idempotency_key(1, "transcribing")
        assert k1 != k2

    def test_same_stage_same_key(self) -> None:
        """同一 segment+stage 幂等键相同。"""
        from app.pipeline.task_worker import _make_idempotency_key

        assert _make_idempotency_key(7, "analyzing") == _make_idempotency_key(7, "analyzing")


class TestEnqueueNext:
    """推进队列:enqueue_next。"""

    def test_valid_transition(self) -> None:
        """合法转换:RECORDED → QUEUED_FOR_TRANS。"""
        from app.db.models import SegmentTask
        from app.pipeline.task_worker import enqueue_next

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RECORDED)
        enqueue_next(t, TaskStatus.QUEUED_FOR_TRANS)
        assert t.stage == TaskStatus.QUEUED_FOR_TRANS
        assert t.attempts == 0
        assert t.idempotency_key == "1:queued_for_transcription"

    def test_invalid_transition_raises(self) -> None:
        """非法转换抛出 ValueError。"""
        from app.db.models import SegmentTask
        from app.pipeline.task_worker import enqueue_next

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
        from app.db.models import SegmentTask
        from app.pipeline.task_worker import mark_active

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.TRANSCRIBING)
        assert t.attempts == 0
        mark_active(t)
        assert t.attempts == 1

    def test_enqueue_next_resets_attempts(self) -> None:
        """enqueue_next 重置 attempts 为 0。"""
        from app.db.models import SegmentTask
        from app.pipeline.task_worker import enqueue_next

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RECORDED, attempts=3)
        enqueue_next(t, TaskStatus.QUEUED_FOR_TRANS)
        assert t.attempts == 0


class TestFailedStage:
    """V0.1.11-alpha: failed_stage 精确恢复。"""

    def test_resume_stage_from_transcribing(self) -> None:
        """TRANSCRIBING → QUEUED_FOR_TRANS。"""
        from app.pipeline.task_worker import _resume_stage

        assert _resume_stage(TaskStatus.TRANSCRIBING) == TaskStatus.QUEUED_FOR_TRANS

    def test_resume_stage_from_analyzing(self) -> None:
        """ANALYZING → QUEUED_FOR_ANALYSIS。"""
        from app.pipeline.task_worker import _resume_stage

        assert _resume_stage(TaskStatus.ANALYZING) == TaskStatus.QUEUED_FOR_ANALYSIS

    def test_resume_stage_from_rendering(self) -> None:
        """RENDERING → QUEUED_FOR_RENDER。"""
        from app.pipeline.task_worker import _resume_stage

        assert _resume_stage(TaskStatus.RENDERING) == TaskStatus.QUEUED_FOR_RENDER

    def test_resume_stage_none_defaults_trans(self) -> None:
        """None → QUEUED_FOR_TRANS (安全默认)。"""
        from app.pipeline.task_worker import _resume_stage

        assert _resume_stage(None) == TaskStatus.QUEUED_FOR_TRANS

    def test_mark_failed_records_failed_stage(self) -> None:
        """mark_failed 记录 failed_stage。"""
        from app.db.models import SegmentTask
        from app.pipeline.task_worker import mark_failed

        t = SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.RENDERING, attempts=1)
        mark_failed(t, "test error", permanent=False)
        assert t.failed_stage == TaskStatus.RENDERING
        assert t.stage == TaskStatus.TRANSIENT_FAILED


class TestRetry:
    """V0.1.11-alpha: 重试恢复逻辑。"""

    def test_retry_from_failed_stage(self) -> None:
        """TRANSIENT_FAILED(TRANSCRIBING) → QUEUED_FOR_TRANS。"""
        from app.pipeline.task_worker import _resume_stage

        assert _resume_stage(TaskStatus.TRANSCRIBING) == TaskStatus.QUEUED_FOR_TRANS

    def test_retry_no_parse_idempotency_key(self) -> None:
        """V0.1.11-alpha: 不使用 idempotency_key 解析阶段。"""
        from app.pipeline.task_worker import _resume_stage

        # failed_stage=ANALYZING → QUEUED_FOR_ANALYSIS,不用检查 idempotency_key
        assert _resume_stage(TaskStatus.ANALYZING) == TaskStatus.QUEUED_FOR_ANALYSIS


# ════════════════════════════════════════════════════
# V0.1.11-alpha: 模型一致性
# ════════════════════════════════════════════════════


class TestDataModelConsistency:
    """V0.1.11-alpha: 数据模型语义校验。"""

    def test_highlight_topic_has_confirmed_by_user(self) -> None:
        """HighlightTopic 有 confirmed_by_user 字段。"""
        from app.db.models import HighlightTopic

        assert hasattr(HighlightTopic, "confirmed_by_user")

    def test_segment_task_has_failed_stage(self) -> None:
        """SegmentTask 有 failed_stage 字段。"""
        from app.db.models import SegmentTask

        assert hasattr(SegmentTask, "failed_stage")

    def test_segment_task_has_heartbeat_at(self) -> None:
        """SegmentTask 有 heartbeat_at 字段。"""
        from app.db.models import SegmentTask

        assert hasattr(SegmentTask, "heartbeat_at")

    def test_segment_task_has_event_id(self) -> None:
        """SegmentTask 有 event_id 字段。"""
        from app.db.models import SegmentTask

        assert hasattr(SegmentTask, "event_id")

    def test_task_status_has_stale(self) -> None:
        """TaskStatus 有 STALE 状态。"""
        from app.db.models import TaskStatus

        assert hasattr(TaskStatus, "STALE")

    def test_ensure_event_creates_once(self, temp_db: None) -> None:
        """_ensure_event 幂等:同一 candidate 只创建一次 Event。"""
        import datetime

        from app.db.models import HighlightCandidate, HighlightEvent
        from app.db.session import get_session

        with get_session() as db:
            cand = HighlightCandidate(
                session_id=1,
                peak_ts=datetime.datetime(2025, 1, 1, 12, 0),
                start_ts=datetime.datetime(2025, 1, 1, 11, 59),
                end_ts=datetime.datetime(2025, 1, 1, 12, 1),
                highlight_score=0.75,
            )
            db.add(cand)
            db.flush()

        from app.pipeline.task_worker import _ensure_event

        eid1 = _ensure_event(cand.id)
        eid2 = _ensure_event(cand.id)
        assert eid1 is not None
        assert eid1 == eid2  # 第二次调用返回相同 event_id

        with get_session() as db:
            events = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == cand.id)).all()
            assert len(events) == 1

    def test_event_id_not_equal_candidate_id(self, temp_db: None) -> None:
        """event_id 和 candidate_id 数值可以不同。"""
        import datetime

        from app.db.models import HighlightCandidate
        from app.db.session import get_session

        with get_session() as db:
            c1 = HighlightCandidate(
                session_id=1,
                peak_ts=datetime.datetime(2025, 1, 1, 12, 0),
                start_ts=datetime.datetime(2025, 1, 1, 11, 59),
                end_ts=datetime.datetime(2025, 1, 1, 12, 1),
                highlight_score=0.75,
            )
            c2 = HighlightCandidate(
                session_id=1,
                peak_ts=datetime.datetime(2025, 1, 1, 13, 0),
                start_ts=datetime.datetime(2025, 1, 1, 12, 59),
                end_ts=datetime.datetime(2025, 1, 1, 13, 1),
                highlight_score=0.80,
            )
            db.add_all([c1, c2])
            db.flush()

        from app.pipeline.task_worker import _ensure_event

        eid1 = _ensure_event(c1.id)
        eid2 = _ensure_event(c2.id)
        assert eid1 is not None
        assert eid2 is not None
        # 关键:event_id 与 candidate_id 可以不同
        assert eid2 == eid1 + 1  # 第二个 event 的 id 自然比第一个大 1


def test_resolve_event_id_backward_compat(temp_db: None) -> None:
    """_resolve_event_id:找到已有 Event 时返回其 ID (不创建新)。"""
    import datetime

    from app.db.models import HighlightCandidate, HighlightEvent, ReviewStatus
    from app.db.session import get_session

    with get_session() as db:
        cand = HighlightCandidate(
            session_id=1,
            peak_ts=datetime.datetime(2025, 1, 1, 12, 0),
            start_ts=datetime.datetime(2025, 1, 1, 11, 59),
            end_ts=datetime.datetime(2025, 1, 1, 12, 1),
            highlight_score=0.75,
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
