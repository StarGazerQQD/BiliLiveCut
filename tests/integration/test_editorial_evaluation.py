from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.analysis.editorial_evaluation import EvaluationLabels, EvaluationSnapshot, evaluate, snapshot_digest
from app.analysis.editorial_evaluation_io import export_snapshot, read_contract, write_result
from app.core.config import settings
from app.db.entities import (
    AppSetting,
    HotspotEvent,
    HotspotStatus,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.workers.analyze import score_hotspot_event
from tests.unit.test_editorial_review import START, _budget, _seed, _service

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("console_encoding", ["utf-8", "cp1252"])
def test_real_judgment_exports_readonly_with_frozen_policy_and_costs(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    console_encoding: str,
) -> None:
    hotspot_id, segment_id = _seed(pcm_wav)
    requests = _service(monkeypatch, "recommend")
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        session_id = segment.session_id
        recording = db.get(RecordingSession, session_id)
        recording.status = "stopped"
        recording.ended_at = START + timedelta(seconds=8.336)
        segment.status = SegmentStatus.SCORED
        db.add(recording)
        db.add(segment)
        db.add(SegmentTask(segment_id=segment_id, session_id=session_id, stage=TaskStatus.AWAITING_REVIEW))
        db.add(AppSetting(key="unrelated-secret", value="evaluation-must-not-export-this-secret"))
    database = tmp_path / "test.db"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    snapshot = export_snapshot(database, dataset_id="protocol-test", session_ids=[session_id])
    assert export_snapshot(database, dataset_id="protocol-test", session_ids=[session_id]) == snapshot
    assert len(requests) == 3
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert len(snapshot.records) == 1 and snapshot.sessions[0].analysis_complete
    record = snapshot.records[0]
    assert record.provenance is not None
    assert len(record.provenance.prior_attempts) == 2
    assert record.execution.result.verdict == "recommend"
    assert record.input.time_origin_epoch_s is not None
    assert str(tmp_path) not in snapshot.model_dump_json()
    assert "evaluation-must-not-export-this-secret" not in snapshot.model_dump_json()
    policy = record.provenance.policy
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 5)
    assert (
        export_snapshot(database, dataset_id="protocol-test", session_ids=[session_id]).records[0].provenance.policy
        == policy
    )
    labels = EvaluationLabels.model_validate(
        {
            "snapshot_sha256": snapshot_digest(snapshot),
            "annotation_version": "protocol-v1",
            "annotator": "test",
            "source": "synthetic",
            "sessions": [
                {
                    "session_id": session_id,
                    "split": "holdout",
                    "complete": True,
                    "watched_ranges": snapshot.sessions[0].recording_ranges,
                    "positives": [record.bounds],
                    "candidates": [],
                }
            ],
        }
    )
    result = evaluate(snapshot, labels, mode="full_session")["groups"]["holdout"]
    assert result["full_session_recall"] == 1.0
    assert result["cost"]["attempts"] == 3
    snapshot_path = tmp_path / "snapshot.json"
    label_path = tmp_path / "labels.json"
    report_path = tmp_path / "report.json"
    write_result(label_path, labels.model_dump(mode="json"))
    unintended = tmp_path / "must-not-create" / "application.db"
    env = dict(
        os.environ,
        DATABASE_URL=f"sqlite:///{unintended.as_posix()}",
        PYTHONUTF8="0",
        PYTHONIOENCODING=f"{console_encoding}:strict",
    )
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "scripts/evaluate_editorial.py")]
    exported = subprocess.run(
        command
        + [
            "export",
            "--database",
            str(database),
            "--dataset-id",
            "protocol-test",
            "--session",
            str(session_id),
            "--output",
            str(snapshot_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr
    assert read_contract(snapshot_path, EvaluationSnapshot) == snapshot
    reported = subprocess.run(
        command
        + [
            "report",
            "--snapshot",
            str(snapshot_path),
            "--labels",
            str(label_path),
            "--mode",
            "full_session",
            "--output",
            str(report_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert reported.returncode == 0, reported.stderr
    assert json.loads(report_path.read_text("utf-8"))["groups"]["holdout"]["cost"]["attempts"] == 3
    assert not unintended.parent.exists()


def test_rejected_hotspots_and_legacy_provenance_are_not_dropped(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    requests = _service(monkeypatch, "reject")
    assert score_hotspot_event(hotspot_id) is None
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        session_id = hotspot.session_id
        features = json.loads(hotspot.features_json)
        features["editorial_review"].pop("provenance")
        hotspot.features_json = json.dumps(features)
        db.add(hotspot)
    snapshot = export_snapshot(tmp_path / "test.db", dataset_id="legacy", session_ids=[session_id])
    assert len(snapshot.records) == 1
    assert snapshot.records[0].provenance is None
    assert snapshot.records[0].execution.result.verdict == "reject"
    assert not snapshot.sessions[0].analysis_complete
    assert len(requests) == 3


def test_export_merges_nested_ranges_and_preserves_gaps(temp_db: None, pcm_wav: Path, tmp_path: Path) -> None:
    _, segment_id = _seed(pcm_wav)
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        session_id = segment.session_id
        for seq, start, end in [(1, 2, 3), (2, 9, 12), (3, 10, 11)]:
            db.add(
                RawSegment(
                    session_id=session_id,
                    seq=seq,
                    file_path=str(pcm_wav),
                    start_ts=START + timedelta(seconds=start),
                    end_ts=START + timedelta(seconds=end),
                )
            )
    snapshot = export_snapshot(tmp_path / "test.db", dataset_id="ranges", session_ids=[session_id])
    assert [(r.start_s, r.end_s) for r in snapshot.sessions[0].recording_ranges] == [
        (0.0, pytest.approx(8.336)),
        (9.0, 12.0),
    ]
    with get_session() as db:
        segment = db.exec(select(RawSegment).where(RawSegment.id == segment_id)).one()
        segment.end_ts = None
        db.add(segment)
    with pytest.raises(ValueError, match="缺少真实"):
        export_snapshot(tmp_path / "test.db", dataset_id="ranges", session_ids=[session_id])


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "absent.db"
    with pytest.raises(ValueError, match="现有数据库"):
        export_snapshot(missing, dataset_id="missing", session_ids=[1])
    assert not missing.exists()


def test_legacy_rejected_window_is_exported_without_creating_candidate(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.analysis.editorial_review_sources import (
        SegmentReviewSnapshot,
        compute_segment_review,
        store_segment_reviews,
    )

    hotspot_id, segment_id = _seed(pcm_wav)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        hotspot.status = HotspotStatus.DISMISSED
        session_id = hotspot.session_id
        db.add(hotspot)
    requests = _service(monkeypatch, "reject")
    start, end, peak = (START + timedelta(seconds=n) for n in (2, 6, 4))
    review = compute_segment_review(
        segment_id, start_ts=start, end_ts=end, peak_ts=peak, metrics={}, danmaku=[], budget=_budget()
    )
    with get_session() as db:
        assert store_segment_reviews(
            db, segment_id, [SegmentReviewSnapshot(review, segment_id, start, end, peak, {}, [])]
        )
    snapshot = export_snapshot(tmp_path / "test.db", dataset_id="legacy-window", session_ids=[session_id])
    assert len(snapshot.records) == 1
    assert snapshot.records[0].key.startswith(f"segment:{segment_id}:")
    assert snapshot.records[0].execution.result.verdict == "reject"
    assert len(requests) == 1


def test_export_keeps_one_snapshot_when_database_changes_between_queries(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, segment_id = _seed(pcm_wav)
    with get_session() as db:
        session_id = db.get(RawSegment, segment_id).session_id
    database = tmp_path / "test.db"
    original_connect = sqlite3.connect
    writer = original_connect(database)
    inserted = False

    def trace(statement: str) -> None:
        nonlocal inserted
        if not inserted and "FROM raw_segments" in statement:
            inserted = True
            writer.execute(
                "INSERT INTO raw_segments (session_id, seq, file_path, start_ts, end_ts, status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    1,
                    str(pcm_wav),
                    (START + timedelta(seconds=9)).isoformat(" "),
                    (START + timedelta(seconds=12)).isoformat(" "),
                    "recorded",
                ),
            )
            writer.commit()

    def connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
        connection = original_connect(database, uri=uri)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        snapshot = export_snapshot(database, dataset_id="transaction", session_ids=[session_id])
        assert inserted
        assert len(snapshot.sessions[0].recording_ranges) == 1
        assert writer.execute("SELECT COUNT(*) FROM raw_segments").fetchone()[0] == 2
    finally:
        writer.close()
