# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from loguru import logger
from sqlmodel import Session

from app.analysis import llm, llm_providers
from app.analysis.editorial_contracts import (
    CONTEXT_SECONDS,
    EVIDENCE_MAX_CHARS,
    EXCERPT_MAX_CHARS,
    EditorialReviewInputV1,
    FailureCode,
    ModelAttempt,
)
from app.analysis.editorial_evidence import EVIDENCE_POLICY_VERSION, EvidenceContextUnavailable, build_event_input
from app.analysis.editorial_explanation import EXPLANATION_POLICY_VERSION, EXPLANATION_PROMPT, GROUNDING_PROMPT, explain
from app.analysis.hotspot_lifecycle import resolve_merged_hotspot_event
from app.analysis.model_execution import ModelCallBudget
from app.analysis.timeline import datetime_epoch
from app.core.config import settings
from app.core.runtime_settings import configured_task
from app.db.entities import AppSetting, HotspotEvent, HotspotStatus, utcnow
from app.db.session import get_session
from app.models.capacity import capacity_identity

EVENT_ENRICHER_VERSION = 2
_RETRY_LIMIT = 3
_RETRY_DELAY_S = 300
_CATEGORIES = {"announcement", "audio_event", "discussion", "gameplay", "other", "performance", "reaction"}


class EventTargetUnavailableError(ValueError):
    """事件已删除、撤销或退出当前待评分状态。"""


class EventEvidenceChangedError(ValueError):
    """计算期间证据快照变化，必须重新计算而不能提交旧结果。"""


@dataclass(frozen=True, slots=True)
class EventEnricherConfig:
    """事件解释的集中输入与输出边界。"""

    context_s: float = CONTEXT_SECONDS
    max_evidence_chars: int = EVIDENCE_MAX_CHARS
    max_excerpt_chars: int = EXCERPT_MAX_CHARS
    max_entities: int = 12
    max_title_chars: int = 32
    max_summary_chars: int = 360

    def __post_init__(self) -> None:
        """拒绝无界或非有限的上下文和输出配置。"""
        if isinstance(self.context_s, bool) or not math.isfinite(self.context_s) or not 0 <= self.context_s <= 900:
            raise ValueError("context_s 必须在 0~900 秒之间")
        bounds = {
            "max_evidence_chars": (1000, EVIDENCE_MAX_CHARS),
            "max_excerpt_chars": (100, EXCERPT_MAX_CHARS),
            "max_entities": (1, 50),
            "max_title_chars": (12, 64),
            "max_summary_chars": (60, 1000),
        }
        for name, (lower, upper) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} 必须在 {lower}~{upper} 之间")
        if self.max_excerpt_chars > self.max_evidence_chars:
            raise ValueError("单条证据上限不能超过总证据上限")


@dataclass(frozen=True, slots=True)
class EventEvidence:
    """兼容现有成片评分读取，同时保留明确的证据时间与精度。"""

    evidence_id: str
    evidence_type: str
    state: str
    score: float | None
    excerpts: tuple[str, ...]
    detail: Mapping[str, object]
    start_s: float | None
    end_s: float | None
    timing_precision: str
    context_role: str

    def to_prompt_payload(self) -> dict[str, object]:
        """转换为不携带路径或异常正文的证据载荷。"""
        return {
            "evidence_id": self.evidence_id,
            "type": self.evidence_type,
            "state": self.state,
            "excerpts": list(self.excerpts),
            "detail": dict(self.detail),
            "start_s": self.start_s,
            "end_s": self.end_s,
            "timing_precision": self.timing_precision,
            "context_role": self.context_role,
        }


@dataclass(frozen=True, slots=True)
class EventEvidenceBundle:
    """内容与执行策略共同寻址的事件证据快照。"""

    event_id: int
    event_key: str
    session_id: int
    start_ts: str
    peak_ts: str
    end_ts: str
    heat_score: float
    detector_semantic_confidence: float
    evidence_coverage: float
    evidence: tuple[EventEvidence, ...]
    fingerprint: str
    source: EditorialReviewInputV1 | None

    @property
    def evidence_ids(self) -> frozenset[str]:
        """返回本快照允许引用的证据身份。"""
        return frozenset(item.evidence_id for item in self.evidence)

    def to_prompt_payload(self) -> dict[str, object]:
        """只输出经过完整范围和字节限制验证的模型输入。"""
        return self.source.model_dump(mode="json") if self.source is not None else {"error": "context_unavailable"}


@dataclass(frozen=True, slots=True)
class EventEnrichmentDraft:
    """事务外生成、事务内重新检查证据与策略后提交的解释。"""

    event_id: int
    bundle_fingerprint: str
    title: str
    summary: str
    category: str
    entities: tuple[str, ...]
    semantic_confidence: float
    evidence_ids: tuple[str, ...]
    source: str
    warnings: tuple[str, ...] = ()
    error: FailureCode | None = None
    attempts: tuple[ModelAttempt, ...] = ()
    elapsed_s: float = 0.0
    lexical_coverage: float | None = None


def _mapping(raw: str | None) -> dict[str, object]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("事件元数据必须是 JSON 对象")
    return value


def _detector_confidence(event: HotspotEvent) -> float:
    previous = _mapping(event.features_json).get("event_enrichment")
    value = previous.get("detector_semantic_confidence") if isinstance(previous, Mapping) else None
    if type(value) not in {int, float} or not math.isfinite(value):
        value = event.semantic_confidence
    return max(0.0, min(1.0, float(value)))


def build_event_evidence_bundle(
    db: Session, event_id: int, *, config: EventEnricherConfig | None = None
) -> EventEvidenceBundle:
    """读取主事件、连续录制上下文与当前策略，生成不含凭据的缓存身份。"""
    cfg = config or EventEnricherConfig()
    raw = db.get(HotspotEvent, event_id)
    if raw is None:
        raise EventTargetUnavailableError(f"HotspotEvent 不存在: event_id={event_id}")
    event = resolve_merged_hotspot_event(db, raw)
    if event.id is None or event.status == HotspotStatus.DISMISSED:
        raise EventTargetUnavailableError(f"事件不可用于解释: event_id={event_id}")
    try:
        source = build_event_input(
            db,
            event,
            context_s=cfg.context_s,
            max_evidence_chars=cfg.max_evidence_chars,
            max_excerpt_chars=cfg.max_excerpt_chars,
        )
    except EvidenceContextUnavailable:
        source = None
    evidence = (
        tuple(
            EventEvidence(
                evidence_id=item.evidence_id,
                evidence_type=item.type,
                state=item.state,
                score=item.detail.metrics.get("score"),
                excerpts=tuple(item.excerpts),
                detail=item.detail.model_dump(mode="json"),
                start_s=item.start_s,
                end_s=item.end_s,
                timing_precision=item.timing_precision,
                context_role=item.context_role,
            )
            for item in source.evidence
        )
        if source is not None
        else ()
    )
    revision = db.get(AppSetting, "configuration_revision")
    routes = [
        {
            "id": item.id,
            "connection_id": item.connection_id,
            "model": item.model,
            "max_output_tokens": item.max_output_tokens,
            "capacity": capacity_identity(item),
            "priority": item.priority,
        }
        for item in llm_providers.active_providers("analysis")
    ]
    snapshot = {
        "version": EVENT_ENRICHER_VERSION,
        "event_id": event.id,
        "event_key": event.event_key,
        "start": event.start_ts.isoformat(),
        "peak": event.peak_ts.isoformat(),
        "end": event.end_ts.isoformat(),
        "input": source.model_dump(mode="json") if source is not None else None,
        "policy": [EVIDENCE_POLICY_VERSION, EXPLANATION_POLICY_VERSION],
        "prompts": hashlib.sha256((EXPLANATION_PROMPT + GROUNDING_PROMPT).encode()).hexdigest(),
        "config": asdict(cfg),
        "routes": routes,
        "configuration_revision": revision.value if revision is not None else "0",
        "max_tokens": settings.hotspot_enrichment_llm_max_tokens,
        "evidence_coverage": event.evidence_coverage,
        "detector_semantic_confidence": _detector_confidence(event),
        "attempt_budget": settings.editorial_model_max_attempts,
        "deadline": settings.editorial_model_deadline_s,
        "request_timeout": settings.llm_request_timeout_s,
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EventEvidenceBundle(
        event_id=event.id,
        event_key=event.event_key,
        session_id=event.session_id,
        start_ts=event.start_ts.isoformat(),
        peak_ts=event.peak_ts.isoformat(),
        end_ts=event.end_ts.isoformat(),
        heat_score=event.heat_score,
        detector_semantic_confidence=_detector_confidence(event),
        evidence_coverage=event.evidence_coverage,
        evidence=evidence,
        fingerprint=fingerprint,
        source=source,
    )


def _enrichment_is_current(event: HotspotEvent, fingerprint: str) -> bool:
    metadata = _mapping(event.features_json).get("event_enrichment")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("version") != EVENT_ENRICHER_VERSION
        or metadata.get("bundle_fingerprint") != fingerprint
    ):
        return False
    if metadata.get("status") == "succeeded":
        return bool(event.title and event.summary and event.category)
    if metadata.get("error") in {"disabled", "unauthorized", "cancelled"}:
        return True
    failures = metadata.get("failure_count")
    if type(failures) is int and failures >= _RETRY_LIMIT:
        return True
    next_retry = metadata.get("next_retry_at")
    if not isinstance(next_retry, str):
        return False
    return datetime_epoch(utcnow()) < datetime_epoch(datetime.fromisoformat(next_retry))


def _confidence(bundle: EventEvidenceBundle, references: Sequence[str]) -> float:
    referenced = [item for item in bundle.evidence if item.evidence_id in references and item.excerpts]
    semantic = [item for item in referenced if item.evidence_type in {"asr", "context_asr", "trend", "visual"}]
    ceiling = 0.95 if any(item.state == "available" for item in semantic) else 0.45 if semantic else 0.4
    return min(ceiling, 0.25 + max(0.0, min(1.0, bundle.evidence_coverage)) * 0.75)


@configured_task
def compute_event_enrichment(
    event_id: int,
    *,
    force: bool = False,
    config: EventEnricherConfig | None = None,
    budget: ModelCallBudget | None = None,
) -> EventEnrichmentDraft | None:
    """在事务外生成与核验解释，缓存成功和有限失败重试分别处理。"""
    cfg = config or EventEnricherConfig()
    budget = budget or llm.new_editorial_budget()
    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id, config=cfg)
        event = db.get(HotspotEvent, bundle.event_id)
        if event is None:
            raise EventTargetUnavailableError("事件在读取时消失")
        if not force and _enrichment_is_current(event, bundle.fingerprint):
            return None
    if bundle.source is None or not any(
        item.excerpts for item in bundle.evidence if item.state in {"available", "degraded"}
    ):
        error: FailureCode = "context_unavailable"
        coverage = None
    else:
        result = explain(
            bundle.source,
            budget,
            max_tokens=settings.hotspot_enrichment_llm_max_tokens,
            max_title_chars=cfg.max_title_chars,
            max_summary_chars=cfg.max_summary_chars,
            max_entities=cfg.max_entities,
        )
        if result.output is not None:
            value = result.output
            return EventEnrichmentDraft(
                event_id=bundle.event_id,
                bundle_fingerprint=bundle.fingerprint,
                title=value.title,
                summary=value.summary,
                category=value.category,
                entities=tuple(value.entities),
                semantic_confidence=round(min(value.semantic_confidence, _confidence(bundle, value.evidence_ids)), 6),
                evidence_ids=tuple(value.evidence_ids),
                source="llm",
                attempts=budget.attempts,
                elapsed_s=budget.elapsed_s,
                lexical_coverage=result.lexical_coverage,
            )
        assert result.error is not None
        error, coverage = result.error, result.lexical_coverage
    description = (
        "自动解释未启用，尚无经过核验的内容摘要。" if error == "disabled" else "解释未完成核验，尚无可采用的内容摘要。"
    )
    return EventEnrichmentDraft(
        event_id=bundle.event_id,
        bundle_fingerprint=bundle.fingerprint,
        title="事件内容待复核",
        summary=description + "请查看原始证据并人工复核。",
        category="other",
        entities=(),
        semantic_confidence=0.0,
        evidence_ids=(),
        source="fallback",
        warnings=(error,),
        error=error,
        attempts=budget.attempts,
        elapsed_s=budget.elapsed_s,
        lexical_coverage=coverage,
    )


def commit_event_enrichment(
    db: Session, draft: EventEnrichmentDraft, *, config: EventEnricherConfig | None = None
) -> bool:
    """在写事务中检查最新身份并保存真实执行状态，不改热度及人工状态。"""
    if draft.error == "cancelled":
        return False
    cfg = config or EventEnricherConfig()
    try:
        current = build_event_evidence_bundle(db, draft.event_id, config=cfg)
    except EventTargetUnavailableError:
        return False
    if current.fingerprint != draft.bundle_fingerprint:
        logger.info("event_enrichment_stale: event_id={}", draft.event_id)
        return False
    if draft.source not in {"llm", "fallback"} or (draft.source == "llm") != (draft.error is None):
        raise ValueError("解释来源和执行状态不一致")
    if (
        draft.category not in _CATEGORIES
        or not draft.title.strip()
        or len(draft.title) > cfg.max_title_chars
        or not draft.summary.strip()
        or len(draft.summary) > cfg.max_summary_chars
    ):
        raise ValueError("解释正文或类别无效")
    if not math.isfinite(draft.semantic_confidence) or not 0 <= draft.semantic_confidence <= 1:
        raise ValueError("解释置信度无效")
    if not set(draft.evidence_ids).issubset(current.evidence_ids):
        raise ValueError("解释引用不在当前证据中")
    event = db.get(HotspotEvent, current.event_id)
    if event is None:
        return False
    features = _mapping(event.features_json)
    previous = features.get("event_enrichment")
    failures = (
        previous.get("failure_count", 0)
        if isinstance(previous, Mapping) and previous.get("bundle_fingerprint") == current.fingerprint
        else 0
    )
    failures = failures if type(failures) is int and failures >= 0 else 0
    features["event_enrichment"] = {
        "version": EVENT_ENRICHER_VERSION,
        "policy_version": EXPLANATION_POLICY_VERSION,
        "bundle_fingerprint": current.fingerprint,
        "source": draft.source,
        "status": "succeeded"
        if draft.error is None
        else draft.error
        if draft.error in {"disabled", "cancelled"}
        else "failed",
        "error": draft.error,
        "entities": list(draft.entities),
        "evidence_ids": list(draft.evidence_ids),
        "warnings": list(draft.warnings),
        "detector_semantic_confidence": current.detector_semantic_confidence,
        "attempts": [item.model_dump(mode="json") for item in draft.attempts],
        "elapsed_s": draft.elapsed_s,
        "lexical_coverage": draft.lexical_coverage,
        "enriched_at": utcnow().isoformat(),
        "failure_count": failures + 1 if draft.error is not None else 0,
        "next_retry_at": (utcnow() + timedelta(seconds=_RETRY_DELAY_S)).isoformat()
        if draft.error is not None
        else None,
    }
    event.title, event.summary, event.category = draft.title, draft.summary, draft.category
    event.semantic_confidence = draft.semantic_confidence
    event.features_json = json.dumps(features, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    event.updated_at = utcnow()
    db.add(event)
    logger.info("event_enrichment_committed: event_id={} source={} error={}", event.id, draft.source, draft.error)
    return True


@configured_task
def enrich_hotspot_event(
    event_id: int, *, force: bool = False, config: EventEnricherConfig | None = None
) -> EventEnrichmentDraft | None:
    """同步解释入口，同一预算覆盖生成和事实核验，写入前再次验证快照。"""
    draft = compute_event_enrichment(event_id, force=force, config=config, budget=llm.new_editorial_budget())
    if draft is None:
        return None
    with get_session() as db:
        connection = db.connection()
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        committed = commit_event_enrichment(db, draft, config=config)
    return draft if committed else None
