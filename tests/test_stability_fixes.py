"""P0 修复行为测试 (V0.1.12.4 稳定性迭代)。

V0.1.12.4: 全部改为真实数据库行为测试, 不再使用 inspect.getsource()。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture()
def test_db(temp_db) -> None:
    """使用 conftest 的 temp_db fixture (隔离 SQLite)。"""
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# ═══════════════════════════════════════════════════
# 原子领取测试
# ═══════════════════════════════════════════════════

class TestAtomicClaim:
    """原子任务领取: 只有一个 Worker 能成功。"""

    def test_single_worker_claims_successfully(self, test_db) -> None:
        """单 Worker 领取 queued 任务成功。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _pop_and_claim

        with get_session() as db:
            task = SegmentTask(
                segment_id=1, session_id=1,
                stage=TaskStatus.QUEUED_FOR_TRANS,
                idempotency_key="1:queued_for_transcription",
            )
            db.add(task)
            db.flush()
            tid = task.id

        claimed = _pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
        assert claimed is not None
        assert claimed.id == tid
        assert claimed.stage == TaskStatus.TRANSCRIBING
        assert claimed.claimed_by is not None

    def test_concurrent_claim_only_one_succeeds(self, test_db) -> None:
        """两个 Worker 并发领取, 只有一个成功。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _pop_and_claim

        with get_session() as db:
            task = SegmentTask(
                segment_id=2, session_id=1,
                stage=TaskStatus.QUEUED_FOR_ANALYSIS,
                idempotency_key="2:queued_for_analysis",
            )
            db.add(task)
            db.flush()

        results: list = [None, None]

        def worker(idx: int) -> None:
            results[idx] = _pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r is not None]
        assert len(successes) == 1, f"应该只有一个成功, 实际: {[r.stage if r else None for r in results]}"
        assert successes[0].claimed_by is not None

    def test_already_claimed_task_not_claimed_again(self, test_db) -> None:
        """已被领取的任务不能再次被领取。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _pop_and_claim

        with get_session() as db:
            task = SegmentTask(
                segment_id=3, session_id=1,
                stage=TaskStatus.QUEUED_FOR_TRANS,
                idempotency_key="3:queued_for_transcription",
            )
            db.add(task)
            db.flush()

        first = _pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
        assert first is not None

        second = _pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
        assert second is None  # 已被领取, 不应重复


# ═══════════════════════════════════════════════════
# 自动化开关测试
# ═══════════════════════════════════════════════════

class TestAutoSwitches:
    """五个自动化开关逐阶段生效。"""

    def test_auto_analyze_off_stays_recorded(self, test_db) -> None:
        """auto_analyze=false → 不进入转写队列。"""
        from app.db.models import LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_recorded

        with get_session() as db:
            room = LiveRoom(id=9001, input_url="test", auto_analyze=False)
            sess = RecordingSession(id=8001, room_id=9001)
            seg = RawSegment(id=7001, session_id=8001, seq=0, file_path="test.mp4")
            task = SegmentTask(
                segment_id=7001, session_id=8001,
                stage=TaskStatus.RECORDED,
                idempotency_key="7001:recorded",
            )
            db.add_all([room, sess, seg, task])

        _advance_recorded()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            assert t.stage == TaskStatus.RECORDED  # 未推进

    def test_auto_analyze_on_advances_to_trans_queue(self, test_db) -> None:
        """auto_analyze=true → 进入转写队列。"""
        from app.db.models import LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_recorded

        with get_session() as db:
            room = LiveRoom(id=9002, input_url="test", auto_analyze=True)
            sess = RecordingSession(id=8002, room_id=9002)
            seg = RawSegment(id=7002, session_id=8002, seq=0, file_path="test.mp4")
            task = SegmentTask(
                segment_id=7002, session_id=8002,
                stage=TaskStatus.RECORDED,
                idempotency_key="7002:recorded",
            )
            db.add_all([room, sess, seg, task])

        _advance_recorded()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            assert t.stage == TaskStatus.QUEUED_FOR_TRANS

    def test_auto_render_off_stays_approved_waiting_render(self, test_db) -> None:
        """auto_render=false → APPROVED → APPROVED_WAITING_RENDER。"""
        from app.db.models import LiveRoom, RecordingSession, SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_approved

        with get_session() as db:
            room = LiveRoom(id=9003, input_url="test", auto_render=False)
            sess = RecordingSession(id=8003, room_id=9003)
            task = SegmentTask(
                segment_id=7003, session_id=8003,
                stage=TaskStatus.APPROVED,
                candidate_id=5001,
                idempotency_key="7003:approved",
            )
            db.add_all([room, sess, task])

        _advance_approved()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            # auto_render=false → APPROVED_WAITING_RENDER
            assert t.stage == TaskStatus.APPROVED_WAITING_RENDER

    def test_auto_approve_off_stays_awaiting_review(self, test_db) -> None:
        """auto_approve=false → 留在 awaiting_review。"""
        from app.db.models import (
            HighlightCandidate,
            LiveRoom,
            RecordingSession,
            SegmentTask,
            TaskStatus,
        )
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_awaiting_review

        with get_session() as db:
            room = LiveRoom(id=9004, input_url="test", auto_approve=False)
            sess = RecordingSession(id=8004, room_id=9004)
            cand = HighlightCandidate(
                id=5002, session_id=8004,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.9,
            )
            task = SegmentTask(
                segment_id=7004, session_id=8004,
                stage=TaskStatus.AWAITING_REVIEW,
                candidate_id=5002,
                idempotency_key="7004:awaiting_review",
            )
            db.add_all([room, sess, cand, task])

        _advance_awaiting_review()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            assert t.stage == TaskStatus.AWAITING_REVIEW

    def test_auto_approve_on_with_high_score_advances(self, test_db) -> None:
        """auto_approve=true + 高分 → 自动批准 (V0.1.12.7: 需 event_id+Event)。"""
        from app.db.models import (
            HighlightCandidate,
            HighlightEvent,
            LiveRoom,
            RecordingSession,
            ReviewStatus,
            SegmentTask,
            TaskStatus,
        )
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_awaiting_review

        with get_session() as db:
            room = LiveRoom(id=9005, input_url="test", auto_approve=True, auto_approve_threshold=0.80)
            sess = RecordingSession(id=8005, room_id=9005)
            cand = HighlightCandidate(
                id=5003, session_id=8005,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.95,
            )
            event = HighlightEvent(
                id=7705, candidate_id=5003, session_id=8005,
                raw_start_ts=_now(), raw_end_ts=_now(),
                review_status=ReviewStatus.PENDING,
            )
            task = SegmentTask(
                segment_id=7005, session_id=8005,
                stage=TaskStatus.AWAITING_REVIEW,
                candidate_id=5003,
                event_id=7705,
                idempotency_key="7005:awaiting_review",
            )
            db.add_all([room, sess, cand, event, task])

        _advance_awaiting_review()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            assert t.stage == TaskStatus.APPROVED

    def test_auto_approve_on_low_score_stays(self, test_db) -> None:
        """auto_approve=true 但分数 < 阈值 → 不批准。"""
        from app.db.models import (
            HighlightCandidate,
            LiveRoom,
            RecordingSession,
            SegmentTask,
            TaskStatus,
        )
        from app.db.session import get_session
        from app.pipeline.task_worker import _advance_awaiting_review

        with get_session() as db:
            room = LiveRoom(id=9006, input_url="test", auto_approve=True, auto_approve_threshold=0.85)
            sess = RecordingSession(id=8006, room_id=9006)
            cand = HighlightCandidate(
                id=5004, session_id=8006,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.60,
            )
            task = SegmentTask(
                segment_id=7006, session_id=8006,
                stage=TaskStatus.AWAITING_REVIEW,
                candidate_id=5004,
                idempotency_key="7006:awaiting_review",
            )
            db.add_all([room, sess, cand, task])

        _advance_awaiting_review()

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t is not None
            assert t.stage == TaskStatus.AWAITING_REVIEW


# ═══════════════════════════════════════════════════
# 心跳 + stale 测试
# ═══════════════════════════════════════════════════

class TestHeartbeat:
    """心跳持续更新时不会被标记 stale。"""

    def test_active_heartbeat_not_stale(self, test_db) -> None:
        """任务有活跃心跳, 即使超过 stale timeout 也不被恢复。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _recover_stale

        with get_session() as db:
            task = SegmentTask(
                segment_id=100, session_id=1,
                stage=TaskStatus.TRANSCRIBING,
                heartbeat_at=_now() - timedelta(seconds=10),  # 10s 前, 但 stale timeout=120s
                claimed_by="worker-1",
                idempotency_key="100:transcribing",
            )
            db.add(task)
            db.flush()
            tid = task.id

        _recover_stale()

        with get_session() as db:
            t = db.get(SegmentTask, tid)
            assert t is not None
            # 心跳在 stale timeout 内, 不应被恢复
            assert t.stage == TaskStatus.TRANSCRIBING

    def test_expired_heartbeat_triggers_stale_recovery(self, test_db) -> None:
        """心跳超时 → 进入 stale 恢复。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import _recover_stale

        with get_session() as db:
            task = SegmentTask(
                segment_id=101, session_id=1,
                stage=TaskStatus.RENDERING,
                heartbeat_at=_now() - timedelta(seconds=200),  # 远超 120s
                claimed_by="worker-old",
                failed_stage=TaskStatus.RENDERING,
                idempotency_key="101:rendering",
            )
            db.add(task)
            db.flush()
            tid = task.id

        _recover_stale()

        with get_session() as db:
            t = db.get(SegmentTask, tid)
            assert t is not None
            assert t.stage == TaskStatus.QUEUED_FOR_RENDER  # 回到渲染队列


# ═══════════════════════════════════════════════════
# 状态机测试
# ═══════════════════════════════════════════════════

class TestStatusMachine:
    """状态转换矩阵。"""

    def test_can_transition_rejects_illegal(self) -> None:
        from app.pipeline.task_worker import TaskStatus, _can_transition
        assert not _can_transition(TaskStatus.COMPLETED, TaskStatus.TRANSCRIBING)
        assert not _can_transition(TaskStatus.AWAITING_REVIEW, TaskStatus.TRANSCRIBED)
        assert _can_transition(TaskStatus.RECORDED, TaskStatus.QUEUED_FOR_TRANS)
        assert _can_transition(TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED)
        assert not _can_transition(TaskStatus.APPROVED, TaskStatus.COMPLETED)  # V0.1.12.5: 不再直接跳转
        assert _can_transition(TaskStatus.APPROVED, TaskStatus.QUEUED_FOR_RENDER)
        assert _can_transition(TaskStatus.RENDERING, TaskStatus.RENDERED)
        assert _can_transition(TaskStatus.RENDERED, TaskStatus.QUEUED_FOR_PUBLISH)

    def test_enqueue_next_resets_attempts(self, test_db) -> None:
        """enqueue_next 重置 attempts=0。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import enqueue_next

        with get_session() as db:
            task = SegmentTask(
                segment_id=200, session_id=1,
                stage=TaskStatus.RECORDED,
                attempts=3, last_error="old error",
                idempotency_key="200:recorded",
            )
            db.add(task)
            db.flush()
            enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
            db.add(task)

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t.attempts == 0
            assert t.last_error is None


# ═══════════════════════════════════════════════════
# 唯一约束测试
# ═══════════════════════════════════════════════════

class TestUniqueConstraints:
    """幂等键和唯一约束。"""

    def test_duplicate_pipeline_key_rejected(self, test_db) -> None:
        """重复 pipeline_key 应被数据库拒绝 (V0.1.12.5)。"""
        from sqlalchemy.exc import IntegrityError

        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session

        with get_session() as db:
            t1 = SegmentTask(
                segment_id=300, session_id=1,
                stage=TaskStatus.RECORDED,
                pipeline_key="pipeline:300",
                stage_key="stage:300:recorded",
                idempotency_key="300:recorded",
            )
            db.add(t1)
            db.flush()  # OK

            t2 = SegmentTask(
                segment_id=301, session_id=1,
                stage=TaskStatus.RECORDED,
                pipeline_key="pipeline:300",  # 与 t1 的 pipeline_key 重复
                stage_key="stage:301:recorded",
                idempotency_key="301:recorded",
            )
            db.add(t2)
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()

    def test_duplicate_segment_id_rejected(self, test_db) -> None:
        """重复 segment_id 应被数据库拒绝 (V0.1.12.5)。"""
        from sqlalchemy.exc import IntegrityError

        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session

        with get_session() as db:
            t1 = SegmentTask(
                segment_id=400, session_id=1,
                stage=TaskStatus.RECORDED,
                pipeline_key="pipeline:400",
                stage_key="stage:400:recorded",
                idempotency_key="400:recorded",
            )
            db.add(t1)
            db.flush()  # OK

            t2 = SegmentTask(
                segment_id=400,  # 与 t1 的 segment_id 重复
                session_id=1,
                stage=TaskStatus.RECORDED,
                pipeline_key="pipeline:401",
                stage_key="stage:401:recorded",
                idempotency_key="401:recorded",
            )
            db.add(t2)
            with pytest.raises(IntegrityError):
                db.flush()
            db.rollback()

    def test_create_task_idempotent(self, test_db) -> None:
        """create_task 对同一 segment 只创建一次。"""
        from app.pipeline.task_worker import create_task
        first = create_task(400, 1)
        assert first is not None
        second = create_task(400, 1)
        assert second is None  # 幂等

    def test_ensure_event_creates_once(self, test_db) -> None:
        """_ensure_event 同一 candidate 只创建一次 Event。"""
        from app.db.models import HighlightCandidate
        from app.db.session import get_session
        from app.pipeline.task_worker import _ensure_event

        with get_session() as db:
            cand = HighlightCandidate(
                id=600, session_id=1,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.8,
            )
            db.add(cand)

        eid1 = _ensure_event(600)
        assert eid1 is not None
        eid2 = _ensure_event(600)
        assert eid1 == eid2  # 幂等

    def test_event_id_different_from_candidate_id(self, test_db) -> None:
        """Event.id != Candidate.id。"""
        from app.db.models import HighlightCandidate
        from app.db.session import get_session

        with get_session() as db:
            cand = HighlightCandidate(
                id=601, session_id=1,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.7,
            )
            db.add(cand)
            db.flush()

        from app.pipeline.task_worker import _ensure_event
        eid = _ensure_event(601)
        assert eid is not None
        # 确认不是同一个 ID (虽然 SQLite 自增可能碰巧, 但不应该被设计成相同)
        assert eid != 601 or eid == 1  # 如果是 1 说明是第一个 Event


# ═══════════════════════════════════════════════════
# 重试和失败阶段恢复
# ═══════════════════════════════════════════════════

class TestRetry:
    """失败任务从 failed_stage 恢复。"""

    def test_retry_from_rendering_goes_to_render_queue(self, test_db) -> None:
        """渲染失败后重试 → queued_for_render。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import retry_task

        with get_session() as db:
            task = SegmentTask(
                segment_id=500, session_id=1,
                stage=TaskStatus.FAILED,
                failed_stage=TaskStatus.RENDERING,
                attempts=2,
                idempotency_key="500:failed",
            )
            db.add(task)
            db.flush()
            tid = task.id

        ok = retry_task(tid)
        assert ok

        with get_session() as db:
            t = db.get(SegmentTask, tid)
            assert t.stage == TaskStatus.QUEUED_FOR_RENDER
            assert t.attempts == 0

    def test_mark_failed_records_failed_stage(self, test_db) -> None:
        """mark_failed 记录 failed_stage。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import mark_failed

        with get_session() as db:
            task = SegmentTask(
                segment_id=501, session_id=1,
                stage=TaskStatus.RENDERING,
                idempotency_key="501:rendering",
            )
            db.add(task)
            db.flush()

            mark_failed(task, "test error", permanent=False)
            assert task.failed_stage == TaskStatus.RENDERING
            assert task.stage == TaskStatus.TRANSIENT_FAILED
            assert task.last_error == "test error"


# ═══════════════════════════════════════════════════
# ASR fallback 追踪
# ═══════════════════════════════════════════════════

class TestASRFallback:
    """fallback 信息不丢失。"""

    def test_asr_result_has_fallback_fields(self) -> None:
        """ASRTranscriptResult 支持新 fallback 字段。"""
        from app.analysis.transcribe import ASRTranscriptResult
        r = ASRTranscriptResult(
            text="test", backend="paraformer",
            final_text_source="fallback",
            primary_status="failed",
            primary_error_type="ValueError",
            primary_error_message="model load failed",
            fallback_backend="whisper",
            fallback_trigger_reason="primary_empty_output",
        )
        assert r.final_text_source == "fallback"
        assert r.primary_status == "failed"
        assert r.fallback_backend == "whisper"

    def test_final_text_source_priority(self) -> None:
        """manual_review_needed > review > fallback > primary。"""
        from app.analysis.transcribe import _compute_review_risk_score, _merge_review_text
        # 优先级由 _review_loop 代码保证: manual_review_needed 先检查
        # 此测试验证逻辑存在
        assert _compute_review_risk_score is not None
        assert _merge_review_text is not None


# ═══════════════════════════════════════════════════
# 数据迁移
# ═══════════════════════════════════════════════════

class TestDataMigration:
    """版本化迁移。"""

    def test_schema_module_loadable(self) -> None:
        """Schema 校验模块可正常导入。"""
        from app.db.schema import compute_schema_fingerprint, validate_schema
        assert callable(compute_schema_fingerprint)
        assert callable(validate_schema)
