"""EventEnricher 证据束、结构校验、保守降级与快照提交测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.analysis.editorial_contracts import EVIDENCE_MAX_BYTES
from app.analysis.event_enricher import (
    EVENT_ENRICHER_VERSION,
    EventEnricherConfig,
    build_event_evidence_bundle,
    commit_event_enrichment,
    compute_event_enrichment,
    enrich_hotspot_event,
)
from app.db.entities import (
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SessionStatus,
    Transcript,
)
from app.db.session import get_session
from tests.editorial_fixtures import install_editorial_service

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_START = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)


def _evidence_item(
    evidence_id: str,
    evidence_type: str,
    *,
    excerpt: str | None = None,
    state: str = "available",
    score: float = 0.8,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": evidence_id,
        "type": evidence_type,
        "score": score,
        "detail": {
            "quality": {"state": state, "usable": state == "available"},
            "error": "不得进入提示词",
        },
        "metrics": {"score": score, "burst": 2.5},
        "excerpts": [excerpt] if excerpt else [],
    }
    return item


def _seed_event(
    *,
    evidence: list[dict[str, object]],
    transcript_text: str | None = None,
    representative: list[dict[str, object]] | None = None,
    semantic_confidence: float = 0.3,
    evidence_coverage: float = 0.8,
    context_text: str | None = None,
    context_words: list[dict[str, object]] | None = None,
) -> int:
    with get_session() as db:
        room = LiveRoom(input_url="event-enricher", room_id=18180, auto_analyze=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(
            room_id=room.id,
            status=SessionStatus.RECORDING,
            started_at=_START,
        )
        db.add(session)
        db.flush()
        assert session.id is not None
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="event-enricher.ts",
            start_ts=_START,
            end_ts=_START + timedelta(minutes=5),
            duration_s=300.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        if context_text is not None:
            db.add(
                Transcript(
                    segment_id=segment.id,
                    base_text=context_text,
                    final_text=context_text,
                    words_json=json.dumps(
                        context_words
                        if context_words is not None
                        else [{"w": context_text, "start": 90.0, "end": 130.0}],
                        ensure_ascii=False,
                    ),
                    primary_backend="paraformer",
                )
            )
        event = HotspotEvent(
            event_key=f"event-enricher:{session.id}",
            session_id=session.id,
            start_ts=_START + timedelta(seconds=90),
            peak_ts=_START + timedelta(seconds=110),
            end_ts=_START + timedelta(seconds=130),
            status=HotspotStatus.CONFIRMED,
            heat_score=0.91,
            clip_score=0.72,
            semantic_confidence=semantic_confidence,
            evidence_coverage=evidence_coverage,
            features_json=json.dumps({"detector_version": "test"}),
            evidence_json=json.dumps({"version": 1, "items": evidence}, ensure_ascii=False),
            representative_danmaku_json=json.dumps(representative or [], ensure_ascii=False),
            transcript_text=transcript_text,
        )
        db.add(event)
        db.flush()
        assert event.id is not None
        return event.id


def test_bundle_collects_context_and_redacts_runtime_error(temp_db: None) -> None:
    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        representative=[{"text": "原来是周末挑战", "count": 4, "role": "information"}],
        context_text="主播宣布周末挑战规则，完成后会抽取奖励。",
    )

    with get_session() as db:
        first = build_event_evidence_bundle(db, event_id)
        second = build_event_evidence_bundle(db, event_id)

    assert first.fingerprint == second.fingerprint
    assert {item.evidence_type for item in first.evidence} >= {"audio", "context_asr", "danmaku"}
    prompt = json.dumps(first.to_prompt_payload(), ensure_ascii=False)
    assert "主播宣布周末挑战规则" in prompt
    assert "原来是周末挑战" in prompt
    assert "不得进入提示词" not in prompt


def test_bundle_excludes_transcript_after_event_context_window(temp_db: None) -> None:
    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        context_text="当前挑战开始。之后主播讨论了完全无关的新游戏。",
        context_words=[
            {"w": "当前挑战开始", "start": 100.0, "end": 105.0},
            {"w": "之后主播讨论了完全无关的新游戏", "start": 220.0, "end": 230.0},
        ],
    )

    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id, config=EventEnricherConfig(context_s=30))

    context = next(item for item in bundle.evidence if item.evidence_type == "context_asr")
    assert context.excerpts == ("当前挑战开始",)
    assert context.state == "available"
    assert context.timing_precision == "word"
    assert (context.start_s, context.end_s) == (100.0, 105.0)
    assert "完全无关的新游戏" not in json.dumps(bundle.to_prompt_payload(), ensure_ascii=False)


def test_expanded_default_window_retains_earlier_and_later_word_evidence(temp_db: None) -> None:
    event_id = _seed_event(
        evidence=[],
        context_text="前文背景。事件内容。后续结果。",
        context_words=[
            {"w": "前文背景", "start": 10.0, "end": 15.0},
            {"w": "事件内容", "start": 100.0, "end": 105.0},
            {"w": "后续结果", "start": 260.0, "end": 265.0},
        ],
    )
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id).source
    assert source is not None and source.window.start_s == 0 and source.window.end_s == 300
    text = "".join(part for item in source.evidence for part in item.excerpts)
    assert all(part in text for part in ("前文背景", "事件内容", "后续结果"))


def test_valid_structured_llm_output_is_persisted_and_idempotent(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:

    event_id = _seed_event(
        evidence=[
            _evidence_item(
                "evidence:asr:1",
                "asr",
                excerpt="主播宣布周末挑战规则，挑战完成后抽取奖励。",
            )
        ]
    )
    calls: list[tuple[str, int]] = []

    def fake_call_text(prompt: str, max_tokens: int) -> str:
        calls.append((prompt, max_tokens))
        return json.dumps(
            {
                "title": "主播宣布周末挑战规则",
                "summary": "主播宣布周末挑战规则，挑战完成后抽取奖励。",
                "category": "announcement",
                "entities": ["周末挑战"],
                "semantic_confidence": 0.9,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        )

    requests = install_editorial_service(monkeypatch, fake_call_text, grounded=True)

    first = enrich_hotspot_event(event_id)
    second = enrich_hotspot_event(event_id)

    assert first is not None and first.source == "llm"
    assert second is None
    assert len(calls) == 1
    assert len(requests) == 2
    assert calls[0][1] == 65536
    assert "不能修改 heat_score 或 clip_score" in calls[0][0]
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        features = json.loads(event.features_json or "{}")

    assert event.title == "主播宣布周末挑战规则"
    assert event.summary == "主播宣布周末挑战规则，挑战完成后抽取奖励。"
    assert event.category == "announcement"
    assert event.semantic_confidence == 0.85
    assert event.heat_score == 0.91
    assert event.clip_score == 0.72
    assert event.status == HotspotStatus.CONFIRMED
    metadata = features["event_enrichment"]
    assert metadata["version"] == EVENT_ENRICHER_VERSION
    assert metadata["source"] == "llm"
    assert metadata["entities"] == ["周末挑战"]
    assert metadata["evidence_ids"] == ["evidence:asr:1"]
    assert metadata["detector_semantic_confidence"] == 0.3


def test_llm_hallucinated_claim_falls_back_to_grounded_summary(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:grounded", "asr", excerpt="主播介绍周末挑战规则。")])
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播意外摔倒受伤",
                "summary": "主播在挑战中意外摔倒并受伤，随后暂停直播。",
                "category": "reaction",
                "entities": [],
                "semantic_confidence": 0.99,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        ),
        grounded=False,
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert [item.purpose for item in result.attempts] == ["explanation", "grounding"]
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
    assert event is not None
    assert "摔倒" not in (event.title or "")
    assert "摔倒" not in (event.summary or "")
    assert "尚无可采用的内容摘要" in (event.summary or "")


def test_danmaku_only_claim_must_remain_attributed(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        representative=[{"text": "听说主播可能要退役", "count": 12, "role": "information"}],
    )
    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id)
    danmaku_id = next(f"E{index}" for index, item in enumerate(bundle.evidence, 1) if item.evidence_type == "danmaku")
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播正式宣布退役",
                "summary": "主播正式宣布退役。",
                "category": "announcement",
                "entities": [],
                "semantic_confidence": 0.95,
                "evidence_ids": [danmaku_id],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.semantic_confidence <= 0.4
    assert result.error == "invalid_output"
    assert "正式宣布退役" not in result.title + result.summary
    assert result.title == "事件内容待复核"
    assert "人工复核" in result.summary


def test_degraded_asr_uses_conservative_fallback_and_low_confidence(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        transcript_text="等一下我们先看看" * 20,
        semantic_confidence=0.8,
    )
    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.semantic_confidence <= 0.45
    assert result.title == "事件内容待复核"
    assert result.error == "provider_error"
    assert "等一下我们先看看" not in result.summary
    assert "人工复核" in result.summary


def test_degraded_llm_claim_without_uncertainty_is_rejected(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:

    event_id = _seed_event(
        evidence=[
            _evidence_item(
                "evidence:asr:degraded",
                "asr",
                excerpt="主播宣布周末挑战规则",
                state="degraded",
                score=0.35,
            )
        ]
    )
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播宣布周末挑战规则",
                "summary": "主播宣布周末挑战规则。",
                "category": "announcement",
                "entities": ["周末挑战"],
                "semantic_confidence": 0.8,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert "人工复核" in result.summary
    assert result.semantic_confidence <= 0.45


def test_unknown_evidence_reference_is_rejected(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:real", "asr", excerpt="主播介绍挑战规则。")])
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播介绍挑战规则",
                "summary": "主播介绍挑战规则。",
                "category": "announcement",
                "entities": ["挑战规则"],
                "semantic_confidence": 0.7,
                "evidence_ids": ["evidence:invented"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert result.evidence_ids == ()


def test_non_text_signal_cannot_support_specific_llm_fact(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio", excerpt="主播完成关键挑战现场反应明显")],
        semantic_confidence=0.6,
    )
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播完成关键挑战",
                "summary": "主播完成关键挑战，现场反应明显。",
                "category": "gameplay",
                "entities": [],
                "semantic_confidence": 0.9,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert result.semantic_confidence <= 0.3
    assert "尚无可采用的内容摘要" in result.summary


def test_llm_output_with_surrounding_text_is_rejected(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:strict", "asr", excerpt="主播介绍挑战规则。")])
    payload = json.dumps(
        {
            "title": "主播介绍挑战规则",
            "summary": "主播介绍挑战规则。",
            "category": "announcement",
            "entities": ["挑战规则"],
            "semantic_confidence": 0.7,
            "evidence_ids": ["E1"],
        },
        ensure_ascii=False,
    )
    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: f"结果如下：\n{payload}")

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert len(result.attempts) == 2


def test_commit_rejects_event_dismissed_during_llm_call(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:dismiss", "asr", excerpt="主播介绍挑战规则。")])
    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)
    draft = compute_event_enrichment(event_id)
    assert draft is not None
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        event.status = HotspotStatus.DISMISSED
        db.add(event)

    with get_session() as db:
        committed = commit_event_enrichment(db, draft)

    assert committed is False


def test_stale_bundle_is_rejected_before_commit(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:initial", "asr", excerpt="主播介绍挑战规则。")])
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播介绍挑战规则",
                "summary": "主播介绍挑战规则。",
                "category": "announcement",
                "entities": ["挑战规则"],
                "semantic_confidence": 0.7,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        ),
        grounded=True,
    )
    draft = compute_event_enrichment(event_id)
    assert draft is not None
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        payload = json.loads(event.evidence_json or "{}")
        payload["items"].append(_evidence_item("evidence:trend:new", "trend", excerpt="挑战规则"))
        event.evidence_json = json.dumps(payload, ensure_ascii=False)
        db.add(event)
    with get_session() as db:
        committed = commit_event_enrichment(db, draft)

    assert committed is False
    with get_session() as db:
        event = db.exec(select(HotspotEvent).where(HotspotEvent.id == event_id)).one()
    assert event.title is None
    assert event.summary is None


def test_number_without_evidence_keeps_conservative_fallback(temp_db: None, monkeypatch: MonkeyPatch) -> None:

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:price", "asr", excerpt="主播正在讨论本轮奖励。")])
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播正在讨论本轮奖励",
                "summary": "主播正在讨论本轮奖励，金额为 1650.86 元。",
                "category": "discussion",
                "entities": [],
                "semantic_confidence": 0.8,
                "evidence_ids": ["E1"],
            },
            ensure_ascii=False,
        ),
    )
    result = enrich_hotspot_event(event_id)
    assert result is not None and result.source == "fallback"
    assert result.error == "invalid_output"
    assert "1650.86" not in result.title + result.summary


def test_event_evidence_preserves_times_and_unknown_values(temp_db: None) -> None:
    timed = _evidence_item("timed", "danmaku", excerpt="观众讨论比赛")
    timed.update(
        start_ts=(_START + timedelta(seconds=95)).isoformat(), end_ts=(_START + timedelta(seconds=105)).isoformat()
    )
    event_id = _seed_event(
        evidence=[timed, _evidence_item("unknown", "asr", excerpt="比赛规则说明")],
        representative=[{"text": "刚才的操作怎么样"}],
    )
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id).source
    assert source is not None
    items = {item.evidence_id: item for item in source.evidence}
    assert (items["timed"].start_s, items["timed"].end_s, items["timed"].context_role) == (95.0, 105.0, "event")
    assert items["unknown"].start_s is None and items["unknown"].timing_precision == "unknown"
    representative = next(item for item in source.evidence if ":danmaku:" in item.evidence_id)
    assert representative.detail.count is None and representative.start_s is None


def test_context_window_stops_at_actual_recording_gap(temp_db: None) -> None:
    event_id = _seed_event(
        evidence=[_evidence_item("unknown", "asr", excerpt="缺口外的发言")],
        transcript_text="缺口两侧混合的转写",
        representative=[{"text": "缺口外的评论"}],
    )
    with get_session() as db:
        segment = db.exec(select(RawSegment)).one()
        segment.start_ts = _START + timedelta(seconds=100)
        segment.end_ts = _START + timedelta(seconds=140)
        db.add(segment)
        db.add(
            RawSegment(
                session_id=segment.session_id,
                seq=1,
                file_path="next.ts",
                start_ts=_START + timedelta(seconds=141),
                end_ts=_START + timedelta(seconds=200),
                duration_s=59,
            )
        )
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id).source
    assert source is not None and source.truncated
    assert (source.window.start_s, source.window.end_s) == (100.0, 140.0)
    assert len(source.recording_ranges) == 1
    assert "缺口" not in source.model_dump_json()


@pytest.mark.parametrize("text", ["", "主播介绍今天的活动规则以及比赛奖励安排。"])
def test_whole_segment_without_word_times_preserves_real_text(temp_db: None, text: str) -> None:
    event_id = _seed_event(evidence=[], context_text=text, context_words=[])
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id, config=EventEnricherConfig(context_s=300)).source
    assert source is not None
    item = next(item for item in source.evidence if item.type == "context_asr")
    assert item.state == ("available" if text else "missing")
    assert item.excerpts == ([text] if text else [])
    assert item.timing_precision == "segment"


def test_unrepresentable_metric_is_not_coerced_or_crashed(temp_db: None) -> None:
    item = _evidence_item("signal", "audio")
    item["metrics"] = {"too_large": 10**1000, "valid": 0.5, "invalid": float("inf")}
    event_id = _seed_event(evidence=[item])
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id).source
    assert source is not None and source.evidence[0].detail.metrics == {"valid": 0.5}


def test_missing_peak_recording_does_not_send_model_request(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")
    with get_session() as db:
        segment = db.exec(select(RawSegment)).one()
        segment.end_ts = _START + timedelta(seconds=100)
        db.add(segment)
    result = enrich_hotspot_event(event_id)
    assert result is not None and result.error == "context_unavailable"
    assert not requests and not result.attempts


def test_replayed_media_before_session_creation_keeps_explicit_time_origin(temp_db: None) -> None:
    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    with get_session() as db:
        segment = db.exec(select(RawSegment)).one()
        segment.start_ts = _START - timedelta(seconds=50)
        segment.end_ts = _START + timedelta(seconds=1)
        db.add(segment)
        event = db.get(HotspotEvent, event_id)
        event.start_ts = _START - timedelta(seconds=25)
        event.peak_ts = _START - timedelta(seconds=10)
        event.end_ts = _START + timedelta(seconds=1)
        db.add(event)
    with get_session() as db:
        source = build_event_evidence_bundle(db, event_id).source
        assert source is not None
        assert source.time_origin_epoch_s == (_START - timedelta(seconds=50)).timestamp()
        assert source.target.bounds.start_s == 25 and source.target.peak_s == 40
        assert source.window.start_s == 0 and source.recording_ranges[0].end_s == 51
        assert db.get(RecordingSession, source.target.session_id).started_at.replace(tzinfo=UTC) == _START


@pytest.mark.parametrize("words", [[], [{"w": "不合法时间", "start": "wrong", "end": 130}]])
def test_unlocalized_context_does_not_invent_proportional_text(temp_db: None, words: list[dict[str, object]]) -> None:
    event_id = _seed_event(
        evidence=[], context_text="开场说明。当前事件。很久以后才发生的其他事情。", context_words=words
    )
    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id, config=EventEnricherConfig(context_s=30))
    item = next(item for item in bundle.evidence if item.evidence_type == "context_asr")
    assert item.state == "missing" and item.excerpts == ()
    assert item.start_s is None and item.timing_precision == "unknown"
    assert "很久以后" not in str(bundle.to_prompt_payload())


def test_complete_json_budget_includes_metadata_and_marks_truncation(temp_db: None) -> None:
    evidence = []
    for index in range(150):
        item = _evidence_item(f"evidence:{index}", "asr", excerpt="可供核查的发言内容。" * 300)
        item["detail"]["backend"] = "引擎" * 3000
        item["metrics"] = {f"metric{number}": float(number) for number in range(100)}
        evidence.append(item)
    event_id = _seed_event(evidence=evidence)
    with get_session() as db:
        source = build_event_evidence_bundle(
            db, event_id, config=EventEnricherConfig(max_evidence_chars=100_000)
        ).source
    assert source is not None and source.truncated
    assert 65536 < len(source.model_dump_json().encode()) <= EVIDENCE_MAX_BYTES
    assert len(source.evidence) < 128
    assert all(len(item.detail.metrics) <= 24 and len(item.detail.backend or "") <= 128 for item in source.evidence)


def test_prompt_config_model_and_coverage_changes_invalidate_cached_identity(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.analysis import event_enricher
    from app.analysis.llm_providers import load_providers, save_providers

    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)
    with get_session() as db:
        first = build_event_evidence_bundle(db, event_id).fingerprint
        assert build_event_evidence_bundle(db, event_id, config=EventEnricherConfig(context_s=60)).fingerprint != first
    providers = load_providers()
    providers[0].model = "updated-model"
    save_providers(providers)
    with get_session() as db:
        second = build_event_evidence_bundle(db, event_id).fingerprint
        assert second != first
        event = db.get(HotspotEvent, event_id)
        event.evidence_coverage = 0.5
        db.add(event)
    with get_session() as db:
        third = build_event_evidence_bundle(db, event_id).fingerprint
        assert third != second
    monkeypatch.setattr(event_enricher, "EXPLANATION_PROMPT", event_enricher.EXPLANATION_PROMPT + "\n更新的核验规则")
    with get_session() as db:
        assert build_event_evidence_bundle(db, event_id).fingerprint != third


def test_failed_explanation_retries_are_delayed_and_bounded(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)
    clock = datetime(2026, 10, 26, 4, 0)
    monkeypatch.setattr(event_enricher, "utcnow", lambda: clock)
    for expected in (2, 4, 6):
        assert enrich_hotspot_event(event_id).error == "provider_error"
        assert enrich_hotspot_event(event_id) is None
        assert len(requests) == expected
        clock += timedelta(seconds=301)
    assert enrich_hotspot_event(event_id) is None and len(requests) == 6
    assert enrich_hotspot_event(event_id, force=True).error == "provider_error"
    assert len(requests) == 8


def test_fact_check_uses_same_attempt_budget_as_generation(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.model_execution import ModelCallBudget

    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    output = {
        "title": "挑战规则说明",
        "summary": "主播介绍挑战规则。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1"],
    }
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: json.dumps(output), grounded=True)
    result = compute_event_enrichment(event_id, budget=ModelCallBudget(max_attempts=1, max_elapsed_s=20))
    assert result is not None and result.error == "attempt_budget"
    assert result.source == "fallback" and len(result.attempts) == len(requests) == 1


@pytest.mark.parametrize("limits", [(32, 360, 12), (12, 60, 1)])
def test_explanation_request_schema_uses_actual_configured_limits(
    temp_db: None, monkeypatch: MonkeyPatch, limits: tuple[int, int, int]
) -> None:
    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    config = EventEnricherConfig(max_title_chars=limits[0], max_summary_chars=limits[1], max_entities=limits[2])
    output = {
        "title": "挑" * (limits[0] + 1),
        "summary": "主播介绍挑战规则。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1"],
    }
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: json.dumps(output))
    result = compute_event_enrichment(event_id, config=config)
    assert result is not None and result.error == "invalid_output" and result.source == "fallback"
    assert len(requests) == 2
    for request in requests:
        properties = request["response_format"]["json_schema"]["schema"]["properties"]
        assert (
            properties["title"]["maxLength"],
            properties["summary"]["maxLength"],
            properties["entities"]["maxItems"],
        ) == limits
    assert "解释字段超过配置上限" in requests[1]["messages"][0]["content"]


def test_semantic_paraphrase_is_accepted_after_grounding_despite_low_lexical_overlap(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    event_id = _seed_event(
        evidence=[_evidence_item("speech", "asr", excerpt="明天开始大家组队进行比赛，胜出者能够获得奖品。")]
    )
    output = {
        "title": "团队竞技安排揭晓",
        "summary": "直播介绍即将举行的集体竞赛及优胜奖励机制。",
        "category": "announcement",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1"],
    }
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: json.dumps(output), grounded=True)
    result = enrich_hotspot_event(event_id)
    assert result is not None and result.error is None and result.source == "llm"
    assert result.lexical_coverage < 0.65
    assert len(requests) == 2 and result.title == output["title"]


@pytest.mark.parametrize("fault", ["unknown_id", "invented_quote", "missing_summary", "empty_quotes"])
def test_grounding_service_cannot_forge_or_omit_evidence(temp_db: None, monkeypatch: MonkeyPatch, fault: str) -> None:
    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    output = {
        "title": "挑战规则说明",
        "summary": "主播介绍挑战规则。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1"],
    }

    def invalid_grounding(_payload: dict[str, object]) -> str:
        quote = {
            "evidence_id": "E999" if fault == "unknown_id" else "E1",
            "quote_id": "Q999" if fault == "invented_quote" else "Q1",
        }
        return json.dumps(
            {
                "judgements": [
                    {
                        "field": field,
                        "supported": True,
                        "reason": "受控错误响应",
                        "quotes": [] if fault == "empty_quotes" else [quote],
                    }
                    for field in ("title", "title" if fault == "missing_summary" else "summary")
                ]
            }
        )

    requests = install_editorial_service(
        monkeypatch, lambda *_args, **_kwargs: json.dumps(output), grounding_reply=invalid_grounding
    )
    result = enrich_hotspot_event(event_id)
    assert result is not None and result.error == "invalid_output"
    assert len(requests) == 3 and [item.purpose for item in result.attempts] == ["explanation", "grounding", "repair"]
    assert result.title != output["title"] and result.evidence_ids == ()


def test_cancelled_explanation_does_not_replace_existing_content(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.model_execution import ModelCallBudget

    event_id = _seed_event(evidence=[_evidence_item("speech", "asr", excerpt="主播介绍挑战规则")])
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.title = "保留已有标题"
        db.add(event)
    draft = compute_event_enrichment(
        event_id, budget=ModelCallBudget(max_attempts=4, max_elapsed_s=20, cancelled=lambda: True)
    )
    assert draft is not None and draft.error == "cancelled" and not requests
    with get_session() as db:
        assert commit_event_enrichment(db, draft) is False
        assert db.get(HotspotEvent, event_id).title == "保留已有标题"
