"""把宿主人工审核决策转换为高光插件训练反馈。"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import cast

from sqlmodel import select

from app.db.models import (
    HighlightCandidate,
    HighlightEvent,
    RawSegment,
    RecordingSession,
    ReviewStatus,
)
from app.db.session import get_session
from app.plugins.highlight import HighlightFeedback, HighlightFeedbackDispatch, HighlightLabel
from app.plugins.manager import plugin_manager

_logger = logging.getLogger(__name__)
_PLUGIN_METADATA_KEY = "highlight_plugin"


def _decode_object(raw: str | None) -> dict[str, object] | None:
    """解析 JSON 对象；损坏、缺失或非对象值均返回 None。"""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        return None
    return cast(dict[str, object], payload)


def _label_for_decision(decision: str) -> HighlightLabel | None:
    """只把明确正例和明确内容负例纳入训练，其余决策撤销现有标签。"""
    if decision in ReviewStatus.POSITIVE:
        return 1
    if decision in {ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING}:
        return 0
    return None


def _feature_values(raw: object) -> dict[str, float | None] | None:
    """验证并规范化插件随预测保存的特征值。"""
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        return None
    values: dict[str, float | None] = {}
    for name, value in raw.items():
        if value is None:
            values[name] = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        else:
            values[name] = float(value)
    return values


def build_highlight_feedback(
    candidate_id: int,
    *,
    decision: str,
    reviewed_by: str,
    reviewed_at: datetime | None = None,
) -> HighlightFeedback | None:
    """从候选快照构建无 ORM 反馈；非插件候选或缺少审计特征时返回 None。"""
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if event is None or event.segment_id is None:
            return None
        recording = db.get(RecordingSession, candidate.session_id)
        if recording is None:
            return None
        segment = db.get(RawSegment, event.segment_id)

        candidate_metadata = _decode_object(candidate.features_json)
        if candidate_metadata is None:
            return None
        plugin_metadata = candidate_metadata.get(_PLUGIN_METADATA_KEY)
        if not isinstance(plugin_metadata, dict):
            return None
        plugin_id = plugin_metadata.get("plugin_id")
        prediction = plugin_metadata.get("prediction")
        if not isinstance(plugin_id, str) or not plugin_id.strip() or not isinstance(prediction, dict):
            return None
        values = _feature_values(prediction.get("feature_values"))
        schema_version = prediction.get("schema_version")
        schema_fingerprint = prediction.get("schema_fingerprint")
        if (
            values is None
            or not isinstance(schema_version, str)
            or not schema_version
            or not isinstance(schema_fingerprint, str)
            or not schema_fingerprint
        ):
            return None

        segment_start_ts = (
            segment.start_ts if segment is not None and segment.start_ts is not None else candidate.start_ts
        )
        return HighlightFeedback(
            plugin_id=plugin_id,
            sample_id=f"candidate:{candidate_id}",
            candidate_id=candidate_id,
            segment_id=event.segment_id,
            session_id=candidate.session_id,
            room_id=recording.room_id,
            segment_start_ts=segment_start_ts,
            label=_label_for_decision(decision),
            decision=decision,
            label_source=f"human:{reviewed_by}:{decision}",
            reviewed_at=reviewed_at or datetime.now(UTC),
            schema_version=schema_version,
            schema_fingerprint=schema_fingerprint,
            feature_values=values,
        )


def record_candidate_review_feedback(
    candidate_id: int,
    *,
    decision: str,
    reviewed_by: str,
    reviewed_at: datetime | None = None,
) -> HighlightFeedbackDispatch | None:
    """在审核事务提交后投递反馈；失败只记录日志，不回滚人工决策。"""
    try:
        feedback = build_highlight_feedback(
            candidate_id,
            decision=decision,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _logger.warning(
            "highlight_feedback_context_skipped candidate=%s decision=%s error=%s",
            candidate_id,
            decision,
            exc,
        )
        return None
    if feedback is None:
        return None
    dispatch = plugin_manager.record_highlight_feedback(feedback)
    if dispatch.error is not None:
        _logger.warning(
            "highlight_feedback_delivery_failed plugin=%s candidate=%s error=%s",
            dispatch.plugin_id,
            candidate_id,
            dispatch.error,
        )
    return dispatch


__all__ = ["build_highlight_feedback", "record_candidate_review_feedback"]
