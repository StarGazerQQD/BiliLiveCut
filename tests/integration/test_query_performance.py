from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import select

from app.analysis.hotspot_detector import build_segment_signal_buckets
from app.analysis.source_policy import load_danmaku_coverage, session_has_danmaku
from app.analysis.topic_cluster import cluster_candidates
from app.db import job_store
from app.db.entities import (
    AppSetting,
    Danmaku,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    Topic,
    Transcript,
)
from app.db.session import get_session
from app.jobs.contracts import JobData, decode_job
from app.plugins.live_source import DanmakuStatus
from app.recording.danmaku import CaptureInterval, DanmakuEvidence
from app.web.services.dashboard import danmaku_overview


@contextmanager
def _queries() -> Iterator[list[str]]:
    from app.db.session import engine

    statements: list[str] = []

    def observe(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("event_text", [None, "", "事件留存正文"])
def test_cluster_reads_current_transcript_only_when_event_text_is_missing(
    temp_db: None, event_text: str | None
) -> None:
    start = datetime(2026, 10, 25)
    with get_session() as db:
        db.add(
            RawSegment(
                id=1, session_id=1, seq=0, file_path="unused.mp4", start_ts=start, end_ts=start + timedelta(seconds=60)
            )
        )
        db.add(Transcript(segment_id=1, final_text="正式转写正文"))
        for index in range(2):
            candidate = HighlightCandidate(
                session_id=1,
                start_ts=start,
                peak_ts=start,
                end_ts=start + timedelta(seconds=10),
                dedup_hash=f"query-{index}",
                features_json='{"keyword_hits":["共同"]}',
            )
            db.add(candidate)
            db.flush()
            db.add(HighlightEvent(candidate_id=candidate.id, session_id=1, asr_text=event_text))
    with _queries() as queries:
        result = cluster_candidates(1)
    assert len(result) == 1 and result[0]["event_count"] == 2
    assert len([query for query in queries if "FROM transcripts" in query]) == 1
    with get_session() as db:
        topic = db.exec(select(Topic)).one()
        text = event_text or "正式转写正文"
        assert topic.summary == f"{text} {text}"


@pytest.mark.parametrize("first_text", ["首个正式正文", ""])
def test_cluster_does_not_choose_later_nonempty_or_another_session_transcript(temp_db: None, first_text: str) -> None:
    start = datetime(2026, 10, 25)
    with get_session() as db:
        for segment_id, text, session_id in [(1, "其他场次", 2), (4, "后续正文", 1), (2, first_text, 1)]:
            db.add(
                RawSegment(
                    id=segment_id,
                    session_id=session_id,
                    seq=segment_id,
                    file_path="unused.mp4",
                    start_ts=start,
                    end_ts=start + timedelta(seconds=60),
                )
            )
            db.add(Transcript(segment_id=segment_id, final_text=text))
        for index in range(2):
            candidate = HighlightCandidate(
                session_id=1,
                start_ts=start,
                peak_ts=start,
                end_ts=start + timedelta(seconds=10),
                dedup_hash=f"first-{index}",
                features_json='{"keyword_hits":["共同"]}',
            )
            db.add(candidate)
            db.flush()
            db.add(HighlightEvent(candidate_id=candidate.id, session_id=1))
    with get_session() as db:
        original_rows = db.exec(
            select(Transcript).where(
                Transcript.segment_id.in_(
                    select(RawSegment.id).where(
                        RawSegment.session_id == 1,
                        RawSegment.start_ts <= start,
                        RawSegment.end_ts >= start + timedelta(seconds=10),
                    )
                )
            )
        ).all()
        assert original_rows[0].final_text == first_text
    result = cluster_candidates(1)
    if first_text:
        assert len(result) == 1
        with get_session() as db:
            assert db.get(Topic, result[0]["id"]).summary == f"{first_text} {first_text}"
    else:
        assert result == []


def _source(evidence: DanmakuEvidence, *, local: bool = False, has_comments: bool = False) -> None:
    with get_session() as db:
        db.add(
            LiveRoom(id=1, room_id=None if local else 123, platform="local" if local else "bilibili", input_url="123")
        )
        db.add(RecordingSession(id=1, room_id=1))
        db.add(AppSetting(key="session_danmaku:1", value=evidence.model_dump_json()))
        if local:
            db.add(AppSetting(key="local_source:1", value=json.dumps({"has_comments": has_comments})))


def test_coverage_snapshot_preserves_single_interval_and_timezones(temp_db: None) -> None:
    start = datetime(2026, 10, 25, tzinfo=UTC)
    evidence = DanmakuEvidence(
        status=DanmakuStatus.FAILED,
        lag_s=2.5,
        confirmed_until=start + timedelta(seconds=55),
        intervals=[
            CaptureInterval(start=start + timedelta(seconds=30), end=start + timedelta(seconds=40)),
            CaptureInterval(start=start, end=start + timedelta(seconds=10)),
            CaptureInterval(start=start + timedelta(seconds=10), end=start + timedelta(seconds=20)),
            CaptureInterval(start=start + timedelta(seconds=50)),
            CaptureInterval(start=start + timedelta(seconds=2), end=start + timedelta(seconds=8)),
        ],
    )
    _source(evidence)
    with get_session() as db:
        snapshot = load_danmaku_coverage(db, 1)
    assert snapshot.lag_s == 2.5
    for left, right in [
        (-1, 0),
        (0, 10),
        (5, 15),
        (10, 10),
        (10, 20),
        (20, 30),
        (30, 40),
        (50, 55),
        (50, 56),
        (19, 18),
    ]:
        a, b = start + timedelta(seconds=left), start + timedelta(seconds=right)
        assert snapshot.contains(a, b) == session_has_danmaku(1, a, b)
        assert snapshot.contains(
            a.replace(tzinfo=None), b.astimezone(timezone(timedelta(hours=8)))
        ) == snapshot.contains(a, b)
    assert not snapshot.contains(start + timedelta(seconds=5), start + timedelta(seconds=15))
    assert snapshot.contains() and snapshot.contains(start)
    with get_session() as db:
        row = db.get(AppSetting, "session_danmaku:1")
        row.value = DanmakuEvidence(status=DanmakuStatus.FAILED).model_dump_json()
        db.add(row)
    assert snapshot.contains(start, start + timedelta(seconds=10))
    with get_session() as db:
        assert not load_danmaku_coverage(db, 1).contains()


@pytest.mark.parametrize("has_comments", [True, False])
def test_local_coverage_keeps_explicit_import_evidence(temp_db: None, has_comments: bool) -> None:
    start = datetime(2026, 10, 25, tzinfo=UTC)
    _source(
        DanmakuEvidence(
            status=DanmakuStatus.AVAILABLE, intervals=[CaptureInterval(start=start, end=start + timedelta(days=1))]
        ),
        local=True,
        has_comments=has_comments,
    )
    with get_session() as db:
        snapshot = load_danmaku_coverage(db, 1)
    assert snapshot.contains() is has_comments
    assert snapshot.contains(start - timedelta(days=10), start) is has_comments
    assert snapshot.contains() == session_has_danmaku(1)


@pytest.mark.parametrize("seconds", [10, 3600])
def test_bucket_builder_reads_capture_evidence_once(temp_db: None, seconds: int) -> None:
    start = datetime(2026, 10, 25, tzinfo=UTC)
    end = start + timedelta(seconds=seconds)
    _source(DanmakuEvidence(status=DanmakuStatus.AVAILABLE, intervals=[CaptureInterval(start=start, end=end)]))
    with get_session() as db:
        db.add(RawSegment(id=1, session_id=1, seq=0, file_path="unused.mp4", start_ts=start, end_ts=end))
    with _queries() as queries:
        buckets, session_id = build_segment_signal_buckets(1)
    assert session_id == 1 and len(buckets) == seconds // 10
    assert all(bucket.danmaku_count == 0 for bucket in buckets)
    assert len([query for query in queries if "FROM app_settings" in query]) == 1


@pytest.mark.parametrize("limit", [0, 1, 50, -1])
@pytest.mark.parametrize("session_id", [None, 1])
def test_danmaku_overview_preserves_order_float_sum_and_missing_sources(
    temp_db: None, limit: int, session_id: int | None
) -> None:
    start = datetime(2026, 10, 25)
    with get_session() as db:
        for index, value in enumerate([1e16, 1.0, -1e16, 0.125, 0.375] * 12):
            db.add(
                Danmaku(
                    session_id=1 + index % 2,
                    room_id=1,
                    ts=start + timedelta(seconds=index // 3),
                    content=str(index),
                    value=value,
                )
            )
    with get_session() as db:
        recent = select(Danmaku).order_by(Danmaku.ts.desc())
        values = select(Danmaku.session_id, Danmaku.value)
        if session_id is not None:
            recent = recent.where(Danmaku.session_id == session_id)
            values = values.where(Danmaku.session_id == session_id)
        expected_recent = [row.content for row in db.exec(recent).all()[:limit]]
        totals: dict[int, tuple[int, float]] = {}
        for sid, value in db.exec(values).all():
            count, intensity = totals.get(sid, (0, 0.0))
            totals[sid] = count + 1, intensity + float(value)
    with _queries() as queries:
        result = danmaku_overview(limit, session_id)
    assert [row["content"] for row in result["recent"]] == expected_recent
    assert result["total"] == sum(count for count, _ in totals.values())
    assert not result["available"]
    assert {row["session_id"]: (row["count"], row["intensity"]) for row in result["sessions"]} == {
        sid: (count, round(value, 2)) for sid, (count, value) in totals.items()
    }
    ordered = [query for query in queries if "FROM danmaku" in query and "ORDER BY" in query]
    assert len(ordered) == 1
    if limit >= 0:
        assert "LIMIT" in ordered[0]


def _job(index: int) -> JobData:
    timestamp = (datetime(2026, 10, 25, tzinfo=UTC) + timedelta(seconds=index % 5)).isoformat()
    return JobData(
        version=1,
        id=f"job-{index}",
        type="query_test",
        label="查询回归",
        owner="alice" if index % 2 else "bob",
        payload={"value": index},
        dedup_key=None,
        cancellable_while_running=True,
        status="queued",
        progress=0,
        message="等待执行",
        result=None,
        error=None,
        attempt=1,
        created_at=timestamp,
        updated_at=timestamp,
        started_at=None,
        finished_at=None,
        recovered=False,
    )


@pytest.mark.parametrize("limit", [0, 1, 12, 500, 900])
@pytest.mark.parametrize("owner", [None, "alice", "missing"])
def test_streamed_jobs_preserve_owner_stable_ties_and_limit(temp_db: None, limit: int, owner: str | None) -> None:
    with get_session() as db:
        for index in reversed(range(530)):
            job = _job(index)
            db.add(AppSetting(key=f"web_job:{job['id']}", value=json.dumps(job)))
    with get_session() as db:
        expected = [
            decode_job(row.value)
            for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("web_job:"))).all()
        ]
    expected = [job for job in expected if owner is None or job["owner"] == owner]
    expected.sort(key=lambda job: job["updated_at"], reverse=True)
    assert job_store.list_jobs(limit, owner=owner) == expected[: max(1, min(limit, 500))]


def test_limited_job_list_still_rejects_corruption_outside_visible_page(temp_db: None) -> None:
    with get_session() as db:
        job = _job(1)
        db.add(AppSetting(key=f"web_job:{job['id']}", value=json.dumps(job)))
        db.add(AppSetting(key="web_job:bad", value='{"owner":"bob","updated_at":"1900-01-01"}'))
    with pytest.raises(ValueError, match="字段"):
        job_store.list_jobs(1, owner="alice")
