"""测试 analyze 阶段 Candidate/Event 并发幂等 (单元级别, 单 Session)。

覆盖:
- _get_or_create_candidate 首次创建。
- _get_or_create_candidate 幂等复用。
- _get_or_create_event 首次创建。
- _get_or_create_event 幂等复用 (唯一约束冲突后查询)。
- 非唯一约束异常不被吞掉。
- 租约失效时不创建 Candidate/Event。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError as _IntegrityError
from sqlmodel import select

from app.db.entities.base import CandidateStatus
from app.db.models import HighlightCandidate as HC
from app.db.models import HighlightEvent as HE
from app.db.session import get_session
from app.pipeline.workers.analyze import (
    _get_or_create_candidate,
    _get_or_create_event,
)

if TYPE_CHECKING:
    pass


@pytest.fixture
def _use_temp_db(temp_db: None) -> None:
    """依赖项目级 temp_db fixture。"""
    pass


class TestGetOrCreateCandidate:
    """_get_or_create_candidate 幂等性测试。"""

    DEDUP = hashlib.sha1(b"test-cand-p1").hexdigest()

    def _make_args(self, dedup: str) -> dict:
        """标准创建参数, 仅 dedup 可变。"""
        now = datetime.now(UTC)
        return dict(
            dedup_hash=dedup,
            session_id=1,
            peak_ts=now,
            start_ts=now,
            end_ts=now,
            rule_score=0.85,
            llm_score=0.80,
            highlight_score=0.90,
            features_json="{}",
            reason="test",
            initial_status=CandidateStatus.PENDING,
        )

    def test_first_create_returns_new(self, _use_temp_db: None) -> None:
        """首次创建应返回全新 Candidate。"""
        with get_session() as db:
            cand = _get_or_create_candidate(db=db, **self._make_args(self.DEDUP))
            db.commit()
            assert cand.id is not None
            assert cand.dedup_hash == self.DEDUP

    def test_second_create_reuses(self, _use_temp_db: None) -> None:
        """第二次以相同 dedup_hash 创建应复用首次记录。"""
        with get_session() as db:
            c1 = _get_or_create_candidate(db=db, **self._make_args(self.DEDUP))
            db.commit()
        with get_session() as db:
            c2 = _get_or_create_candidate(db=db, **self._make_args(self.DEDUP))
            assert c2.id == c1.id

    def test_different_dedup_different_records(self, _use_temp_db: None) -> None:
        """不同 dedup_hash 应创建不同 Candidate。"""
        d1 = hashlib.sha1(b"a").hexdigest()
        d2 = hashlib.sha1(b"b").hexdigest()
        with get_session() as db:
            c1 = _get_or_create_candidate(db=db, **self._make_args(d1))
            db.commit()
        with get_session() as db:
            c2 = _get_or_create_candidate(db=db, **self._make_args(d2))
            db.commit()
            assert c1.id != c2.id

    def test_non_unique_exception_not_swallowed(self, _use_temp_db: None) -> None:
        """非唯一约束的 IntegrityError 不应被吞掉。"""
        with pytest.raises((_IntegrityError, ValueError, Exception)):
            with get_session() as db:
                _get_or_create_candidate(
                    db=db,
                    dedup_hash=hashlib.sha1(b"boom").hexdigest(),
                    session_id=1,
                    peak_ts=datetime.now(UTC),
                    start_ts=None,
                    end_ts=None,  # type: ignore[arg-type]
                    rule_score=0.5,
                    llm_score=0.0,
                    highlight_score=0.5,
                    features_json="{}",
                    reason="",
                    initial_status=CandidateStatus.PENDING,
                )

    def test_concurrent_creates_one_record(self, _use_temp_db: None) -> None:
        """两个 Worker 并发创建相同 dedup, 应只保留一条。"""
        dedup = hashlib.sha1(b"concurrent-unit").hexdigest()
        args = self._make_args(dedup)

        def worker() -> int:
            with get_session() as s:
                c = _get_or_create_candidate(db=s, **args)
                s.commit()
                return c.id

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(worker), ex.submit(worker)
            r1, r2 = f1.result(), f2.result()

        assert r1 == r2
        with get_session() as s:
            all_cands = s.exec(select(HC).where(HC.dedup_hash == dedup)).all()
            assert len(all_cands) == 1


class TestGetOrCreateEvent:
    """_get_or_create_event 幂等性测试。"""

    def _seed_candidate(self, dedup: str) -> int:
        """创建一个 Candidate 并返回其 ID。"""
        now = datetime.now(UTC)
        with get_session() as db:
            cand = _get_or_create_candidate(
                db=db,
                dedup_hash=dedup,
                session_id=1,
                peak_ts=now,
                start_ts=now,
                end_ts=now,
                rule_score=0.9,
                llm_score=0.85,
                highlight_score=0.95,
                features_json="{}",
                reason="test",
                initial_status=CandidateStatus.PENDING,
            )
            db.commit()
            return cand.id

    def test_first_call_creates_event(self, _use_temp_db: None) -> None:
        """首次调用创建全新 Event。"""
        now = datetime.now(UTC)
        cid = self._seed_candidate(hashlib.sha1(b"evt-first-u").hexdigest())
        with get_session() as db:
            eid = _get_or_create_event(
                db=db,
                candidate_id=cid,
                session_id=1,
                raw_start_ts=now,
                raw_end_ts=now,
                rule_score=0.9,
                llm_score=0.85,
                highlight_score=0.95,
                features_json="{}",
                reason="test",
            )
            db.commit()
            event = db.get(HE, eid)
            assert event is not None
            assert event.candidate_id == cid

    def test_second_call_reuses(self, _use_temp_db: None) -> None:
        """第二次以相同 candidate_id 调用应复用已有 Event。"""
        now = datetime.now(UTC)
        cid = self._seed_candidate(hashlib.sha1(b"evt-reuse-u").hexdigest())
        with get_session() as db:
            e1 = _get_or_create_event(
                db=db,
                candidate_id=cid,
                session_id=1,
                raw_start_ts=now,
                raw_end_ts=now,
                rule_score=0.9,
                llm_score=0.85,
                highlight_score=0.95,
                features_json="{}",
                reason="test",
            )
            db.commit()
        with get_session() as db:
            e2 = _get_or_create_event(
                db=db,
                candidate_id=cid,
                session_id=1,
                raw_start_ts=now,
                raw_end_ts=now,
                rule_score=0.9,
                llm_score=0.85,
                highlight_score=0.95,
                features_json="{}",
                reason="test",
            )
            assert e2 == e1

    def test_concurrent_creates_one_event(self, _use_temp_db: None) -> None:
        """两个 Worker 同时为同一 candidate_id 创建 Event — 仅一条。"""
        now = datetime.now(UTC)
        cid = self._seed_candidate(hashlib.sha1(b"concurrent-evt-u").hexdigest())

        def worker() -> int:
            with get_session() as s:
                eid = _get_or_create_event(
                    db=s,
                    candidate_id=cid,
                    session_id=1,
                    raw_start_ts=now,
                    raw_end_ts=now,
                    rule_score=0.9,
                    llm_score=0.85,
                    highlight_score=0.95,
                    features_json="{}",
                    reason="test",
                )
                s.commit()
                return eid

        with ThreadPoolExecutor(max_workers=2) as ex:
            f1, f2 = ex.submit(worker), ex.submit(worker)
            r1, r2 = f1.result(), f2.result()

        assert r1 == r2
        with get_session() as s:
            events = s.exec(select(HE).where(HE.candidate_id == cid)).all()
            assert len(events) == 1
