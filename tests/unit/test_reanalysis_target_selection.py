from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlmodel import select

from app.analysis.reanalysis_targets import ReanalysisTarget, list_reanalysis_targets
from app.db.entities import AppSetting, HighlightCandidate, HighlightEvent, HotspotEvent, RawSegment, Transcript
from app.db.session import get_session
from tests.unit.test_reanalysis import _seed_session

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, ExecutionContext


def test_segment_selection_preserves_canonical_owner_deduplication_and_cross_segment_protection(temp_db: None) -> None:
    session_id, first_id, candidate_id, _ = _seed_session()
    with get_session() as db:
        first, second = db.exec(select(RawSegment).order_by(RawSegment.id)).all()
        linked = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).one()
        linked.segment_id = second.id
        db.add(linked)
        overlapping = RawSegment(
            session_id=session_id,
            seq=2,
            file_path="overlapping.ts",
            start_ts=first.start_ts,
            end_ts=first.end_ts,
            duration_s=300,
        )
        db.add(overlapping)
        db.flush()
        db.add(Transcript(segment_id=overlapping.id, final_text="重叠分段的原始文本"))
        hotspot = HotspotEvent(
            event_key="cross-segment",
            session_id=session_id,
            start_ts=first.start_ts + timedelta(seconds=100),
            peak_ts=first.start_ts + timedelta(seconds=120),
            end_ts=second.start_ts + timedelta(seconds=10),
            status="confirmed",
            candidate_id=candidate_id,
        )
        db.add(hotspot)
        db.flush()
        db.add(
            AppSetting(
                key=f"editorial_segment:{overlapping.id}",
                value=json.dumps(
                    {
                        "schema_version": "segment-editorial-v1",
                        "segment_id": overlapping.id,
                        "windows": [
                            {"peak_ts": (first.start_ts + timedelta(seconds=90)).isoformat(), "editorial_review": {}}
                        ],
                    }
                ),
            )
        )
        segment_ids = [first_id, second.id, overlapping.id]
        hotspot_key = f"hotspot:{hotspot.id}"
    with get_session() as db:
        full = list_reanalysis_targets(db, session_id)
        assert len(full) == 3
        selected_hotspot = next(item for item in full if item.key == hotspot_key)
        assert selected_hotspot.segment_id == first_id
        assert selected_hotspot.protection == "human_decision"
        assert not any(item.key == f"candidate:{candidate_id}" for item in full)
        for segment_id in segment_ids:
            selected = list_reanalysis_targets(db, session_id, segment_id=segment_id)
            assert selected == [item for item in full if item.segment_id == segment_id]


def _count_selects(session_id: int, segment_id: int | None) -> tuple[list[ReanalysisTarget], int]:
    count = 0

    def observe(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1

    with get_session() as db:
        connection = db.connection()
        event.listen(connection, "before_cursor_execute", observe)
        try:
            targets = list_reanalysis_targets(db, session_id, segment_id=segment_id)
        finally:
            event.remove(connection, "before_cursor_execute", observe)
    return targets, count


def test_segment_selection_does_not_query_fingerprints_for_unrelated_targets(temp_db: None) -> None:
    session_id, first_id, _, _ = _seed_session()
    original, before_count = _count_selects(session_id, first_id)
    with get_session() as db:
        first = db.get(RawSegment, first_id)
        for seq in range(2, 22):
            start = first.start_ts + timedelta(minutes=5 * seq)
            segment = RawSegment(
                session_id=session_id,
                seq=seq,
                file_path=f"extra-{seq}.ts",
                start_ts=start,
                end_ts=start + timedelta(minutes=5),
                duration_s=300,
            )
            db.add(segment)
            db.flush()
            db.add(Transcript(segment_id=segment.id, final_text=f"第{seq}段原文"))
            candidate = HighlightCandidate(
                session_id=session_id,
                peak_ts=start + timedelta(seconds=60),
                start_ts=start + timedelta(seconds=40),
                end_ts=start + timedelta(seconds=80),
                dedup_hash=f"extra-{seq}",
            )
            db.add(candidate)
            db.flush()
            db.add(
                HighlightEvent(
                    candidate_id=candidate.id,
                    session_id=session_id,
                    segment_id=segment.id,
                    raw_start_ts=candidate.start_ts,
                    raw_end_ts=candidate.end_ts,
                    review_by="auto",
                )
            )
    selected, after_count = _count_selects(session_id, first_id)
    full, full_count = _count_selects(session_id, None)
    assert selected == original == [item for item in full if item.segment_id == first_id]
    assert after_count == before_count
    assert full_count > 5 * after_count
    print(f"SELECT counts: selected before={before_count}, selected after={after_count}, full={full_count}")
