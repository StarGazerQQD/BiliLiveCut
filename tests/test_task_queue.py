"""P0 测试: 状态机转换 + 幂等任务创建(V0.1.6)。"""

from __future__ import annotations

import pytest

from app.db.models import TaskStatus


# ---- 状态机 ----
_VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.RECORDED: {TaskStatus.QUEUED_FOR_TRANS},
    TaskStatus.QUEUED_FOR_TRANS: {TaskStatus.TRANSCRIBING},
    TaskStatus.TRANSCRIBING: {TaskStatus.TRANSCRIBED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSCRIBED: {TaskStatus.QUEUED_FOR_ANALYSIS},
    TaskStatus.QUEUED_FOR_ANALYSIS: {TaskStatus.ANALYZING},
    TaskStatus.ANALYZING: {TaskStatus.CANDIDATE_CREATED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.CANDIDATE_CREATED: {TaskStatus.QUEUED_FOR_RENDER},
    TaskStatus.QUEUED_FOR_RENDER: {TaskStatus.RENDERING},
    TaskStatus.RENDERING: {TaskStatus.AWAITING_REVIEW, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.APPROVED, TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.COMPLETED},
    TaskStatus.TRANSIENT_FAILED: {TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.QUEUED_FOR_RENDER, TaskStatus.FAILED},
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
            (TaskStatus.CANDIDATE_CREATED, TaskStatus.QUEUED_FOR_RENDER, True),
            (TaskStatus.QUEUED_FOR_RENDER, TaskStatus.RENDERING, True),
            (TaskStatus.RENDERING, TaskStatus.AWAITING_REVIEW, True),
            (TaskStatus.RENDERING, TaskStatus.FAILED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.COMPLETED, True),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.CANCELLED, True),
            (TaskStatus.APPROVED, TaskStatus.COMPLETED, True),
            (TaskStatus.TRANSIENT_FAILED, TaskStatus.QUEUED_FOR_TRANS, True),
            (TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED, True),
            # 非法转换。
            (TaskStatus.RECORDED, TaskStatus.TRANSCRIBING, False),
            (TaskStatus.RECORDED, TaskStatus.COMPLETED, False),
            (TaskStatus.AWAITING_REVIEW, TaskStatus.RECORDED, False),
            (TaskStatus.COMPLETED, TaskStatus.QUEUED_FOR_TRANS, False),
            (TaskStatus.FAILED, TaskStatus.RENDERING, False),
            (TaskStatus.CANCELLED, TaskStatus.ANALYZING, False),
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
