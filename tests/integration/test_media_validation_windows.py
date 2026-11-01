from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, insert
from sqlmodel import Session

from app.analysis import editorial_review_sources as sources
from app.core.paths import raw_dir
from app.db.entities import LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session

_START = datetime(2026, 10, 25, tzinfo=UTC)


def _seed(count: int) -> list[Path]:
    paths = [raw_dir() / f"part-{index}.ts" for index in range(count)]
    for path in paths:
        path.write_bytes(b"media input")
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="window", room_id=1))
        db.add(RecordingSession(id=1, room_id=1, started_at=_START))
        db.execute(
            insert(RawSegment),
            [
                {
                    "id": index + 1,
                    "session_id": 1,
                    "seq": index,
                    "file_path": str(path),
                    "start_ts": _START + timedelta(seconds=index * 60),
                    "end_ts": _START + timedelta(seconds=(index + 1) * 60),
                }
                for index, path in enumerate(paths)
            ],
        )
    return paths


@pytest.mark.parametrize("history", [10, 1000])
def test_preflight_only_probes_requested_window_and_closes_database_before_io(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, history: int
) -> None:
    from app.db.session import engine

    paths = _seed(history)
    active_connections = 0
    probes: list[str] = []
    original = sources._probe_media
    loaded = 0

    def checkout(connection: object, record: object, proxy: object) -> None:
        nonlocal active_connections
        active_connections += 1

    def checkin(connection: object, record: object) -> None:
        nonlocal active_connections
        active_connections -= 1

    def load(db: Session, instance: object) -> None:
        nonlocal loaded
        loaded += isinstance(instance, RawSegment)

    def probe(path: str) -> sources._MediaState:
        assert active_connections == 0
        probes.append(path)
        return original(path)

    window = sources.MediaWindow(1, _START + timedelta(seconds=300), _START + timedelta(seconds=360))
    monkeypatch.setattr(sources, "_probe_media", probe)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    event.listen(Session, "loaded_as_persistent", load)
    try:
        with sources.media_validation_scope(load_windows=lambda db: [window]):
            with get_session() as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                assert sources.media_snapshot_is_current(db, 1, start=window.start, end=window.end)
                assert sources.segment_review_revision(db, 6)
                outside = db.get(RawSegment, history)
                assert outside is not None
                with pytest.raises(ValueError, match="媒体引用"):
                    sources.media_file_identity(outside)
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
        event.remove(Session, "loaded_as_persistent", load)
    assert probes == [str(paths[5])]
    assert loaded <= 5


def test_disjoint_windows_deduplicate_paths_and_preserve_unknown_boundaries(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _seed(12)
    with get_session() as db:
        other = db.get(RawSegment, 11)
        assert other is not None
        other.file_path = str(paths[1])
        db.add(other)
        db.add(RawSegment(id=13, session_id=1, seq=12, file_path=str(paths[1])))
    probes: list[str] = []
    original = sources._probe_media

    def probe(path: str) -> sources._MediaState:
        probes.append(path)
        return original(path)

    monkeypatch.setattr(sources, "_probe_media", probe)
    windows = [
        sources.MediaWindow(1, _START + timedelta(seconds=value), _START + timedelta(seconds=value + 60))
        for value in (60, 600)
    ]
    with sources.media_validation_scope(load_windows=lambda db: windows), get_session() as db:
        assert sources.segment_review_revision(db, 2)
        assert sources.segment_review_revision(db, 11)
        assert not sources.media_snapshot_is_current(db, 1, start=windows[0].start, end=windows[1].end)
        unknown = db.get(RawSegment, 13)
        assert unknown is not None and sources.media_file_identity(unknown)
    assert probes == [str(paths[1])]


@pytest.mark.parametrize("change", ["new_inside", "delete_inside", "move_inside", "new_outside"])
def test_reference_changes_are_checked_only_within_actual_windows(temp_db: None, change: str) -> None:
    paths = _seed(10)
    window = sources.MediaWindow(1, _START, _START + timedelta(seconds=60))
    with sources.media_validation_scope(load_windows=lambda db: [window]):
        with get_session() as db:
            row = db.get(RawSegment, 1)
            assert row is not None
            if change == "delete_inside":
                db.delete(row)
            elif change == "move_inside":
                row.file_path = str(paths[2])
                db.add(row)
            else:
                offset = 10 if change == "new_inside" else 900
                db.add(
                    RawSegment(
                        session_id=1,
                        seq=11,
                        file_path=str(paths[0]),
                        start_ts=_START + timedelta(seconds=offset),
                        end_ts=_START + timedelta(seconds=offset + 10),
                    )
                )
        with get_session() as db:
            assert sources.media_snapshot_is_current(db, 1, start=window.start, end=window.end) is (
                change == "new_outside"
            )


def test_sql_window_filter_preserves_utc_offset_meaning(temp_db: None) -> None:
    _seed(10)
    zone = timezone(timedelta(hours=8))
    window = sources.MediaWindow(1, _START.astimezone(zone), (_START + timedelta(seconds=60)).astimezone(zone))
    with sources.media_validation_scope(load_windows=lambda db: [window]), get_session() as db:
        assert sources.segment_review_revision(db, 1)
        assert sources.media_snapshot_is_current(db, 1, start=window.start, end=window.end)


def test_empty_plan_never_falls_back_to_file_access(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(1)

    def probe(path: str) -> sources._MediaState:
        raise AssertionError("未安排的媒体不得在提交事务探测")

    monkeypatch.setattr(sources, "_probe_media", probe)
    with sources.media_validation_scope(load_windows=lambda db: []), get_session() as db:
        assert not sources.media_snapshot_is_current(db, 1, start=_START, end=_START + timedelta(seconds=60))
        segment = db.get(RawSegment, 1)
        assert segment is not None
        with pytest.raises(ValueError, match="媒体引用"):
            sources.media_file_identity(segment)


@pytest.mark.parametrize("direction,expected_files", [("before", 11), ("after", 11), ("both", 21)])
def test_segment_review_preflights_actual_six_hundred_second_expansion(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, direction: str, expected_files: int
) -> None:
    from app.analysis.media_validation_windows import segment_result_windows
    from tests.editorial_fixtures import install_editorial_service
    from tests.unit.test_editorial_contracts import _payload
    from tests.unit.test_editorial_review import _budget

    _seed(32)
    with get_session() as db:
        for index in range(32):
            db.add(
                Transcript(
                    segment_id=index + 1,
                    final_text="主播解释比赛规则。",
                    words_json=json.dumps(
                        [
                            {"w": "主播解释比赛规则。", "start": 0.0, "end": 60.0},
                        ],
                        ensure_ascii=False,
                    ),
                )
            )
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                verdict="needs_review",
                standalone=None,
                boundary_status="needs_expansion",
                missing_context=direction,
                suggested_bounds=None,
                uncertainties=["缺少结果"],
                evidence_ids=["E1"],
            )
        ),
    )
    start, end = _START + timedelta(seconds=900), _START + timedelta(seconds=960)
    review = sources.compute_segment_review(
        16, start_ts=start, end_ts=end, peak_ts=start + timedelta(seconds=30), metrics={}, danmaku=[], budget=_budget(2)
    )
    assert len(calls) == 2 and review.expanded_context == direction
    snapshot = sources.SegmentReviewSnapshot(review, 16, start, end, start + timedelta(seconds=30), {}, [])
    probes: list[str] = []
    original = sources._probe_media

    def probe(path: str) -> sources._MediaState:
        probes.append(path)
        return original(path)

    monkeypatch.setattr(sources, "_probe_media", probe)
    with sources.media_validation_scope(
        load_windows=lambda db: segment_result_windows(db, {"editorial_outcomes": [snapshot]})
    ):
        with get_session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert snapshot.is_current(db)
    assert len(probes) == expected_files
