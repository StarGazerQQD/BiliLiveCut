"""测试 Candidate/Event 真正并发幂等 (多 Session, 多线程)。

覆盖:
- 两个 Worker 同时提交同一 Candidate (最终只有一个)。
- 两个 Worker 同时创建同一 Event (最终只有一个)。
- 租约失效时不创建 Candidate/Event。
- 重启重试不重复创建。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.db.entities.base import CandidateStatus, SegmentStatus
from app.db.entities.highlight import HighlightCandidate, HighlightEvent
from app.db.entities.recording import RawSegment
from app.db.entities.room import RecordingSession
from app.db.entities.task import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from app.pipeline.workers.analyze import (
    HighlightDecision,
    _get_or_create_candidate,
    _get_or_create_event,
    commit_highlight,
)

if TYPE_CHECKING:
    pass


@pytest.fixture
def _use_temp_db(temp_db: None) -> None:
    """依赖项目级 temp_db fixture。"""
    pass


def _make_session_record() -> int:
    """创建 RecordingSession 并返回 ID。"""
    with get_session() as db:
        s = RecordingSession(room_id=1, start_time=datetime.now(UTC), title="test")
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id


def _make_segment(session_id: int, start_offset: float = 0.0) -> int:
    """创建一个 RawSegment 并返回其 ID。"""
    now = datetime.now(UTC)
    with get_session() as db:
        seg = RawSegment(
            session_id=session_id,
            seq=0,
            file_path=f"test_seg_{start_offset}.ts",
            start_ts=now + timedelta(seconds=start_offset),
            end_ts=now + timedelta(seconds=start_offset + 30),
            duration_s=30.0,
            status=SegmentStatus.TRANSCRIBED,
        )
        db.add(seg)
        db.commit()
        db.refresh(seg)
        return seg.id


def _make_task(segment_id: int, session_id: int) -> SegmentTask:
    """创建一个待分析 Task 并返回。"""
    with get_session() as db:
        task = SegmentTask(
            segment_id=segment_id,
            session_id=session_id,
            stage=TaskStatus.QUEUED_FOR_ANALYSIS,
            attempts=0,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task


class TestCandidateConcurrency:
    """两个 Worker 并发创建同一 Candidate — 最终只有一个。"""

    def test_two_workers_create_one(self, _use_temp_db: None) -> None:
        """两个线程同时创建相同 dedup_hash 的 Candidate, 应只保留一条。"""
        dedup = hashlib.sha1(b"conc-cand-int").hexdigest()
        now = datetime.now(UTC)

        def worker_create() -> int:
            with get_session() as s:
                c = _get_or_create_candidate(
                    db=s,
                    dedup_hash=dedup,
                    session_id=1,
                    peak_ts=now,
                    start_ts=now,
                    end_ts=now,
                    rule_score=0.9,
                    llm_score=0.85,
                    highlight_score=0.92,
                    features_json="{}",
                    reason="test",
                    initial_status=CandidateStatus.PENDING,
                )
                s.commit()
                return c.id

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(worker_create), ex.submit(worker_create)
            r1, r2 = f1.result(), f2.result()

        assert r1 == r2
        with get_session() as s:
            all_cands = s.exec(select(HighlightCandidate).where(HighlightCandidate.dedup_hash == dedup)).all()
            assert len(all_cands) == 1

    def test_restart_retry_no_duplicate(self, _use_temp_db: None) -> None:
        """模拟重启重试: 首次创建后再次调用, 不应重复创建。"""
        dedup = hashlib.sha1(b"restart-int").hexdigest()
        now = datetime.now(UTC)

        args = dict(
            dedup_hash=dedup,
            session_id=1,
            peak_ts=now,
            start_ts=now,
            end_ts=now,
            rule_score=0.9,
            llm_score=0.85,
            highlight_score=0.92,
            features_json="{}",
            reason="restart",
            initial_status=CandidateStatus.PENDING,
        )

        with get_session() as s:
            c1 = _get_or_create_candidate(db=s, **args)
            s.commit()

        with get_session() as s:
            c2 = _get_or_create_candidate(db=s, **args)
            s.commit()

        assert c2.id == c1.id
        with get_session() as s:
            assert len(s.exec(select(HighlightCandidate).where(HighlightCandidate.dedup_hash == dedup)).all()) == 1


class TestEventConcurrency:
    """两个 Worker 并发创建同一 Event — 最终只有一个。"""

    def test_two_workers_create_one_event(self, _use_temp_db: None) -> None:
        """两个线程同时为同一 candidate_id 创建 Event。"""
        dedup = hashlib.sha1(b"conc-evt-int").hexdigest()
        now = datetime.now(UTC)

        with get_session() as s:
            cand = _get_or_create_candidate(
                db=s,
                dedup_hash=dedup,
                session_id=1,
                peak_ts=now,
                start_ts=now,
                end_ts=now,
                rule_score=0.9,
                llm_score=0.85,
                highlight_score=0.92,
                features_json="{}",
                reason="test",
                initial_status=CandidateStatus.PENDING,
            )
            s.commit()
            cid = cand.id

        def worker_create_event() -> int:
            with get_session() as s:
                eid = _get_or_create_event(
                    db=s,
                    candidate_id=cid,
                    session_id=1,
                    raw_start_ts=now,
                    raw_end_ts=now,
                    rule_score=0.9,
                    llm_score=0.85,
                    highlight_score=0.92,
                    features_json="{}",
                    reason="test",
                )
                s.commit()
                return eid

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(worker_create_event), ex.submit(worker_create_event)
            r1, r2 = f1.result(), f2.result()

        assert r1 == r2
        with get_session() as s:
            assert len(s.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == cid)).all()) == 1

    def test_lease_lost_no_create(self, _use_temp_db: None) -> None:
        """租约失效时 commit_highlight 不应创建 Candidate/Event。"""
        session_id = _make_session_record()
        seg_id = _make_segment(session_id)
        task = _make_task(seg_id, session_id)

        expired_lease = TaskLease(
            task_id=task.id,
            worker_id="expired-worker",
            lease_token="expired-token-does-not-match-db",
            expected_stage=TaskStatus.QUEUED_FOR_ANALYSIS,
        )

        dedup = hashlib.sha1(b"lease-lost-int").hexdigest()
        compute_result = {
            "decision": HighlightDecision.CANDIDATE,
            "segment_id": seg_id,
            "session_id": session_id,
            "peak_ts": datetime.now(UTC),
            "start_ts": datetime.now(UTC),
            "end_ts": datetime.now(UTC) + timedelta(minutes=1),
            "rule_score": 0.9,
            "llm_score": 0.85,
            "highlight_score": 0.92,
            "features_json": "{}",
            "reason": "test",
            "initial_status": CandidateStatus.PENDING,
            "dedup_hash": dedup,
        }

        commit_highlight(expired_lease, compute_result, 100)

        with get_session() as s:
            cands = s.exec(select(HighlightCandidate).where(HighlightCandidate.dedup_hash == dedup)).all()
            assert len(cands) == 0, "租约失效不应创建 Candidate"
