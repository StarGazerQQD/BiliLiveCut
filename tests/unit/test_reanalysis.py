"""场次重分析与人工转写纠错回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.analysis.reanalysis import (
    process_pending_session_reanalyses,
    queue_session_reanalysis,
    request_session_reanalysis,
)
from app.db.models import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session
from app.web.services.transcripts import _clean_alias_term, correct_transcript, derive_aliases_from_correction


def _seed_session() -> tuple[int, int, int, int]:
    """创建两段带自动/人工候选的录制会话。"""
    base = datetime(2026, 8, 5, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="1", room_id=1)
        db.add(room)
        db.flush()
        session = RecordingSession(
            room_id=room.id, status="stopped", started_at=base, ended_at=base + timedelta(minutes=10)
        )
        db.add(session)
        db.flush()
        segment_ids: list[int] = []
        candidate_ids: list[int] = []
        for seq in range(2):
            segment = RawSegment(
                session_id=session.id,
                seq=seq,
                file_path=f"{seq}.ts",
                start_ts=base + timedelta(minutes=5 * seq),
                end_ts=base + timedelta(minutes=5 * (seq + 1)),
                duration_s=300,
                status="transcribed",
            )
            db.add(segment)
            db.flush()
            transcript = Transcript(segment_id=segment.id, text=f"第{seq}段转写", final_text=f"第{seq}段转写")
            db.add(transcript)
            candidate = HighlightCandidate(
                session_id=session.id,
                peak_ts=segment.start_ts + timedelta(seconds=60),
                start_ts=segment.start_ts + timedelta(seconds=40),
                end_ts=segment.start_ts + timedelta(seconds=80),
                status=CandidateStatus.PENDING,
                dedup_hash=f"candidate-{seq}",
            )
            db.add(candidate)
            db.flush()
            event = HighlightEvent(
                candidate_id=candidate.id,
                session_id=session.id,
                segment_id=segment.id,
                raw_start_ts=candidate.start_ts,
                raw_end_ts=candidate.end_ts,
                review_status=ReviewStatus.PENDING,
                review_by="auto" if seq == 0 else "human-reviewer",
            )
            db.add(event)
            db.flush()
            task = SegmentTask(
                segment_id=segment.id,
                session_id=session.id,
                candidate_id=candidate.id,
                event_id=event.id,
                stage=TaskStatus.AWAITING_REVIEW,
                pipeline_key=f"pipeline:{segment.id}",
            )
            db.add(task)
            segment_ids.append(segment.id)
            candidate_ids.append(candidate.id)
        return session.id, segment_ids[0], candidate_ids[0], candidate_ids[1]


def test_reanalysis_replaces_auto_candidate_and_preserves_human_result(temp_db: None) -> None:
    """重分析只能清理自动候选，不能覆盖人工审核结果。"""
    session_id, first_segment_id, auto_candidate_id, human_candidate_id = _seed_session()

    result = queue_session_reanalysis(session_id, reason="threshold_changed")

    assert result.queued == 1
    assert result.preserved == 1
    with get_session() as db:
        assert db.get(HighlightCandidate, auto_candidate_id) is None
        assert db.get(HighlightCandidate, human_candidate_id) is not None
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        assert task.stage == TaskStatus.QUEUED_FOR_ANALYSIS
        assert task.candidate_id is None


def test_persisted_final_reanalysis_waits_for_old_pipeline_then_runs(temp_db: None) -> None:
    """下播收尾请求应跨轮询持久化，并在旧分析稳定后一次性重排。"""
    session_id, first_segment_id, auto_candidate_id, human_candidate_id = _seed_session()
    with get_session() as db:
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        task.stage = TaskStatus.ANALYZING
        task.claimed_by = "old-worker"
        db.add(task)

    assert request_session_reanalysis(session_id, reason="session_finalized") is True
    assert process_pending_session_reanalyses() == []
    with get_session() as db:
        assert db.get(AppSetting, f"session_reanalysis:{session_id}") is not None
        assert db.get(HighlightCandidate, auto_candidate_id) is not None
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        task.stage = TaskStatus.AWAITING_REVIEW
        task.claimed_by = None
        db.add(task)

    completed = process_pending_session_reanalyses()

    assert len(completed) == 1
    assert completed[0].queued == 1
    assert completed[0].preserved == 1
    with get_session() as db:
        assert db.get(AppSetting, f"session_reanalysis:{session_id}") is None
        assert db.get(HighlightCandidate, auto_candidate_id) is None
        assert db.get(HighlightCandidate, human_candidate_id) is not None


def test_reanalysis_waits_for_analysis_retry_but_not_render_retry(temp_db: None) -> None:
    """只有可能写入旧分析结果的失败重试才应阻塞最终重分析。"""
    session_id, first_segment_id, auto_candidate_id, _human_candidate_id = _seed_session()
    key = f"session_reanalysis:{session_id}"
    with get_session() as db:
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        task.stage = TaskStatus.TRANSIENT_FAILED
        task.failed_stage = TaskStatus.ANALYZING
        db.add(task)

    assert request_session_reanalysis(session_id, reason="model_changed") is True
    assert process_pending_session_reanalyses() == []
    with get_session() as db:
        assert db.get(AppSetting, key) is not None
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        task.failed_stage = TaskStatus.RENDERING
        db.add(task)

    completed = process_pending_session_reanalyses()

    assert len(completed) == 1
    with get_session() as db:
        assert db.get(AppSetting, key) is None
        assert db.get(HighlightCandidate, auto_candidate_id) is None


def test_reanalysis_never_partially_resets_when_any_task_is_active(temp_db: None) -> None:
    """存在活跃旧任务时必须整场等待，不能产生新旧分析版本混合。"""
    session_id, first_segment_id, auto_candidate_id, human_candidate_id = _seed_session()
    with get_session() as db:
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == first_segment_id)).one()
        task.stage = TaskStatus.ANALYZING
        db.add(task)

    result = queue_session_reanalysis(session_id, reason="dictionary_changed")

    assert result.queued == 0
    assert result.skipped_active == 1
    with get_session() as db:
        assert db.get(HighlightCandidate, auto_candidate_id) is not None
        assert db.get(HighlightCandidate, human_candidate_id) is not None


def test_manual_transcript_correction_learns_room_alias_and_requeues_analysis(temp_db: None) -> None:
    """人工纠错应绑定直播间词典，并保留人工文本只重跑分析。"""
    base = datetime(2026, 8, 5, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="2", room_id=2)
        db.add(room)
        db.flush()
        room_id = room.id
        session = RecordingSession(
            room_id=room.id, status="stopped", started_at=base, ended_at=base + timedelta(minutes=5)
        )
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="0.ts",
            start_ts=base,
            end_ts=base + timedelta(minutes=5),
            duration_s=300,
            status="transcribed",
        )
        db.add(segment)
        db.flush()
        transcript = Transcript(segment_id=segment.id, text="查里斯进房", final_text="查里斯进房")
        db.add(transcript)
        db.flush()
        transcript_id = transcript.id
        task = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            stage=TaskStatus.COMPLETED,
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(task)

    result = correct_transcript(transcript_id, "查理斯进房", actor="tester")

    assert result["learned_aliases"] == {"查里斯": "查理斯"}
    assert result["reanalysis"] == {"session_id": result["session_id"], "requested": True}
    completed = process_pending_session_reanalyses()
    assert len(completed) == 1
    with get_session() as db:
        transcript = db.get(Transcript, transcript_id)
        assert transcript is not None
        assert transcript.text == "查理斯进房"
        assert transcript.final_text_source == "manual"
        assert transcript.words_json is None
        room = db.get(LiveRoom, room_id)
        config = json.loads(room.room_config_json)
        assert config["aliases"]["查里斯"] == "查理斯"
        assert "查理斯" in config["hotwords"]
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == transcript.segment_id)).one()
        assert task.stage == TaskStatus.QUEUED_FOR_ANALYSIS


def test_reanalysis_without_transcript_returns_segment_to_transcription(temp_db: None) -> None:
    """缺失转写的分段不得被误标成已转写，必须回到 ASR 队列。"""
    base = datetime(2026, 8, 5, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="missing-transcript", room_id=22)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, status="stopped", started_at=base, ended_at=base)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="missing.ts",
            start_ts=base,
            end_ts=base + timedelta(minutes=5),
            duration_s=300,
            status="recorded",
        )
        db.add(segment)
        db.flush()
        segment_id = segment.id
        session_id = session.id

    result = queue_session_reanalysis(session_id, reason="model_changed")

    assert result.queued == 1
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == segment_id)).one()
    assert segment is not None and segment.status == "recorded"
    assert task.stage == TaskStatus.QUEUED_FOR_TRANS


def test_derive_aliases_ignores_wholesale_rewrites() -> None:
    """整段改写不应被误学成直播间纠错词。"""
    assert derive_aliases_from_correction("完全不同的一大段旧内容", "另一段毫无关系的新内容") == {}


def test_clean_alias_term_handles_long_untrusted_padding_linearly() -> None:
    """超长用户标点输入应在线性扫描中完成清理。"""
    padding = "_！？— " * 50_000

    assert _clean_alias_term(f"{padding}查理斯{padding}") == "查理斯"
