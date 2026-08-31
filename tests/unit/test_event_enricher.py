"""EventEnricher 证据束、结构校验、保守降级与快照提交测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import select

from app.analysis.event_enricher import (
    EVENT_ENRICHER_VERSION,
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
        bundle = build_event_evidence_bundle(db, event_id)

    context = next(item for item in bundle.evidence if item.evidence_type == "context_asr")
    assert context.excerpts == ("当前挑战开始",)
    assert context.state == "available"
    assert context.detail["precise"] is True
    assert "完全无关的新游戏" not in json.dumps(bundle.to_prompt_payload(), ensure_ascii=False)


def test_valid_structured_llm_output_is_persisted_and_idempotent(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import event_enricher

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
                "evidence_ids": ["evidence:asr:1"],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(event_enricher.llm, "call_text", fake_call_text)

    first = enrich_hotspot_event(event_id)
    second = enrich_hotspot_event(event_id)

    assert first is not None and first.source == "llm"
    assert second is None
    assert len(calls) == 1
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
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:grounded", "asr", excerpt="主播介绍周末挑战规则。")])
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播意外摔倒受伤",
                "summary": "主播在挑战中意外摔倒并受伤，随后暂停直播。",
                "category": "reaction",
                "entities": [],
                "semantic_confidence": 0.99,
                "evidence_ids": ["evidence:asr:grounded"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert any("叙述包含过多证据外语义" in warning for warning in result.warnings)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
    assert event is not None
    assert "摔倒" not in (event.title or "")
    assert "摔倒" not in (event.summary or "")
    assert "周末挑战规则" in (event.summary or "")


def test_danmaku_only_claim_must_remain_attributed(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        representative=[{"text": "听说主播可能要退役", "count": 12, "role": "information"}],
    )
    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id)
    danmaku_id = next(item.evidence_id for item in bundle.evidence if item.evidence_type == "danmaku")
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
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
    assert "弹幕" in result.title
    assert "弹幕集中讨论" in result.summary
    assert "结合画面确认" in result.summary


def test_degraded_asr_uses_conservative_fallback_and_low_confidence(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio")],
        transcript_text="等一下我们先看看" * 20,
        semantic_confidence=0.8,
    )
    monkeypatch.setattr(event_enricher.llm, "call_text", lambda *_args, **_kwargs: None)

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert result.semantic_confidence <= 0.45
    assert result.title == "语音热点内容待确认"
    assert "转写质量较低" in result.summary
    assert "仍需人工确认" in result.summary


def test_degraded_llm_claim_without_uncertainty_is_rejected(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import event_enricher

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
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播宣布周末挑战规则",
                "summary": "主播宣布周末挑战规则。",
                "category": "announcement",
                "entities": ["周末挑战"],
                "semantic_confidence": 0.8,
                "evidence_ids": ["evidence:asr:degraded"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert any("degraded 语义证据" in warning for warning in result.warnings)
    assert "转写质量较低" in result.summary
    assert result.semantic_confidence <= 0.45


def test_unknown_evidence_reference_is_rejected(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:real", "asr", excerpt="主播介绍挑战规则。")])
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
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
    assert any("未知 evidence_id" in warning for warning in result.warnings)
    assert result.evidence_ids[0] == "evidence:asr:real"


def test_non_text_signal_cannot_support_specific_llm_fact(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(
        evidence=[_evidence_item("signal:audio", "audio", excerpt="主播完成关键挑战现场反应明显")],
        semantic_confidence=0.6,
    )
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播完成关键挑战",
                "summary": "主播完成关键挑战，现场反应明显。",
                "category": "gameplay",
                "entities": [],
                "semantic_confidence": 0.9,
                "evidence_ids": ["signal:audio"],
            },
            ensure_ascii=False,
        ),
    )

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert any("非文本信号" in warning for warning in result.warnings)
    assert result.semantic_confidence <= 0.3
    assert "证据不足" in result.summary


def test_llm_output_with_surrounding_text_is_rejected(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:strict", "asr", excerpt="主播介绍挑战规则。")])
    payload = json.dumps(
        {
            "title": "主播介绍挑战规则",
            "summary": "主播介绍挑战规则。",
            "category": "announcement",
            "entities": ["挑战规则"],
            "semantic_confidence": 0.7,
            "evidence_ids": ["evidence:asr:strict"],
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(event_enricher.llm, "call_text", lambda *_args, **_kwargs: f"结果如下：\n{payload}")

    result = enrich_hotspot_event(event_id)

    assert result is not None and result.source == "fallback"
    assert "llm_output_not_json" in result.warnings


def test_commit_rejects_event_dismissed_during_llm_call(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:dismiss", "asr", excerpt="主播介绍挑战规则。")])
    monkeypatch.setattr(event_enricher.llm, "call_text", lambda *_args, **_kwargs: None)
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
    from app.analysis import event_enricher

    event_id = _seed_event(evidence=[_evidence_item("evidence:asr:initial", "asr", excerpt="主播介绍挑战规则。")])
    monkeypatch.setattr(
        event_enricher.llm,
        "call_text",
        lambda *_args, **_kwargs: json.dumps(
            {
                "title": "主播介绍挑战规则",
                "summary": "主播介绍挑战规则。",
                "category": "announcement",
                "entities": ["挑战规则"],
                "semantic_confidence": 0.7,
                "evidence_ids": ["evidence:asr:initial"],
            },
            ensure_ascii=False,
        ),
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
