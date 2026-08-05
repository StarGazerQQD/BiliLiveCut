"""阈值自学习模块。

当用户对候选进行审批(approve)/拒绝(reject)操作时,记录评分与阈值快照。
收集足够样本后,自动计算推荐阈值并更新房间配置。

算法:收集该房间所有已审批候选的高光评分,取 P15 分位数作为新阈值,
确保 85% 以上的人工认可候选不会被遗漏,同时去掉低分噪音。
"""

from __future__ import annotations

from sqlmodel import select

from app.core.config import settings
from app.db.models import HighlightCandidate, LiveRoom, RecordingSession, ReviewStatus, ThresholdFeedback
from app.db.session import get_session


def record_feedback(room_id: int, candidate_id: int, action: str) -> ThresholdFeedback:
    """记录一次审批反馈(用于后续阈值自学习)。

    :param room_id: 直播间 db id。
    :param candidate_id: 候选 id。
    :param action: ``"approved"`` 或 ``"rejected"``。
    :returns: 新建的 :class:`ThresholdFeedback`。
    """
    if action not in {"approved", "rejected"}:
        raise ValueError(f"无效的阈值反馈动作: {action}")
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        old_threshold = room.highlight_threshold if room else settings.highlight_threshold
        cand = db.get(HighlightCandidate, candidate_id)
        highlight_score = cand.highlight_score if cand else 0.0
        existing = db.exec(
            select(ThresholdFeedback)
            .where(ThresholdFeedback.candidate_id == candidate_id)
            .order_by(ThresholdFeedback.created_at.asc())
        ).all()
        record = (
            existing[0]
            if existing
            else ThresholdFeedback(
                room_id=room_id,
                candidate_id=candidate_id,
                action=action,
                old_threshold=old_threshold,
                highlight_score=highlight_score,
            )
        )
        record.room_id = room_id
        record.action = action
        record.old_threshold = old_threshold
        record.highlight_score = highlight_score
        db.add(record)
        for duplicate in existing[1:]:
            db.delete(duplicate)
        db.flush()
        db.refresh(record)
    return record


def _collect_scores(room_id: int, action: str = "approved") -> list[float]:
    """收集某房间指定动作(默认 approved)的所有高光评分。

    :param room_id: 直播间 db id。
    :param action: ``"approved"`` 或 ``"rejected"``。
    :returns: 评分列表(升序)。
    """
    with get_session() as db:
        rows = db.exec(
            select(ThresholdFeedback.highlight_score).where(
                ThresholdFeedback.room_id == room_id,
                ThresholdFeedback.action == action,
            )
        ).all()
    return sorted(_scalar_score(row) for row in rows)


def sync_candidate_feedback(
    candidate_id: int,
    *,
    decision: str,
) -> dict[str, object]:
    """同步一个候选的当前人工决策，并在启用时更新房间阈值。"""
    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        recording = db.get(RecordingSession, candidate.session_id)
        room = db.get(LiveRoom, recording.room_id) if recording is not None else None
        if room is None:
            raise ValueError(f"候选缺少有效直播间: id={candidate_id}")
        room_id = room.id
        enabled = bool(room.auto_threshold_enabled)

    action = _feedback_action(decision)
    if not enabled:
        return {"enabled": False, "action": action, "room_id": room_id, "threshold": None}
    if action is None:
        with get_session() as db:
            rows = db.exec(select(ThresholdFeedback).where(ThresholdFeedback.candidate_id == candidate_id)).all()
            for row in rows:
                db.delete(row)
        return {"enabled": True, "action": None, "room_id": room_id, "threshold": None}

    record_feedback(room_id, candidate_id, action)
    changed = apply_threshold_if_changed(room_id)
    return {"enabled": True, "action": action, "room_id": room_id, "threshold": changed}


def compute_recommended_threshold(room_id: int) -> float | None:
    """基于历史审批数据计算推荐阈值。

    逻辑:
    - 收集该房间所有 approved 候选的评分
    - 取 P15 分位数(即 85% 的认可候选高于此值)
    - 如 rejected 最高分低于 P15,取二者中点,在保留至少 85% 认可样本的同时排除已知拒绝样本
    - 如正负样本重叠,优先守住认可样本召回率,不让拒绝样本错误地下调阈值
    - 变化幅度受 ``threshold_learning_max_delta`` 限制

    :param room_id: 直播间 db id。
    :returns: 推荐的新阈值;样本不足或全局开关关闭时返回 ``None``。
    """
    from app.core import settings_store

    if not settings_store.get_bool("threshold_learning_enabled"):
        return None

    good_scores = _collect_scores(room_id, "approved")
    if len(good_scores) < settings.threshold_learning_min_samples:
        return None

    # P15 分位数:线性插值更精确。
    n = len(good_scores)
    k = (n - 1) * 0.15
    f = k - int(k)
    i = int(k)
    if i + 1 < n:
        new_threshold = good_scores[i] + f * (good_scores[i + 1] - good_scores[i])
    else:
        new_threshold = good_scores[i]

    # 仅在正负样本可分时用拒绝样本收窄安全区间。若两类样本重叠，
    # 保持 P15，避免为了迎合一个高分拒绝样本而漏掉大量人工认可片段。
    bad_scores = _collect_scores(room_id, "rejected")
    if bad_scores:
        rejected_ceiling = bad_scores[-1]
        if rejected_ceiling < new_threshold:
            new_threshold = (rejected_ceiling + new_threshold) / 2

    # 锁定安全区间。
    new_threshold = max(0.2, min(0.95, new_threshold))

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        if room is None:
            return None
        old = room.highlight_threshold
        delta = settings.threshold_learning_max_delta
        clamped = max(old - delta, min(old + delta, new_threshold))

    return round(clamped, 3)


def apply_threshold_if_changed(room_id: int) -> float | None:
    """计算推荐阈值;若与当前值差异 >= 0.005 则写入数据库。

    :param room_id: 直播间 db id。
    :returns: 新阈值;未变更或未计算时返回 ``None``。
    """
    recommended = compute_recommended_threshold(room_id)
    if recommended is None:
        return None

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        if room is None:
            return None
        if abs(room.highlight_threshold - recommended) < 0.005:
            return None
        old = room.highlight_threshold
        room.highlight_threshold = recommended
        db.add(room)

    from loguru import logger

    logger.info(
        "阈值自学习:房间 #{} 阈值 {:.3f} → {:.3f}(样本数≥{})",
        room_id,
        old,
        recommended,
        settings.threshold_learning_min_samples,
    )
    return recommended


def feedback_summary(room_id: int) -> dict:
    """返回某房间的阈值学习摘要(供前端展示)。

    :param room_id: 直播间 db id。
    :returns: 含分数分布、推荐阈值等信息的字典。
    """
    from app.core import settings_store

    good = _collect_scores(room_id, "approved")
    bad = _collect_scores(room_id, "rejected")

    if not good and not bad:
        return {
            "enabled": settings_store.get_bool("threshold_learning_enabled"),
            "samples": 0,
            "ready": False,
            "current_threshold": _current_threshold(room_id),
            "recommended": None,
            "approved_range": None,
            "rejected_range": None,
            "min_samples": settings.threshold_learning_min_samples,
        }

    rec = compute_recommended_threshold(room_id)
    return {
        "enabled": settings_store.get_bool("threshold_learning_enabled"),
        "samples": len(good) + len(bad),
        "ready": len(good) >= settings.threshold_learning_min_samples,
        "min_samples": settings.threshold_learning_min_samples,
        "current_threshold": _current_threshold(room_id),
        "recommended": rec,
        "approved_range": [round(good[0], 3), round(good[-1], 3)] if good else None,
        "rejected_range": [round(bad[0], 3), round(bad[-1], 3)] if bad else None,
    }


def _current_threshold(room_id: int) -> float:
    """读取房间当前阈值。"""
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        return room.highlight_threshold if room else settings.highlight_threshold


def _scalar_score(value: object) -> float:
    """兼容 SQLModel 单列查询返回标量或 Row 的不同版本。"""
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(value[0])  # type: ignore[index]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise TypeError(f"无法解析阈值反馈分数: {value!r}") from exc


def _feedback_action(decision: str) -> str | None:
    if decision in ReviewStatus.POSITIVE or decision == "approved":
        return "approved"
    if decision in {ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING, "rejected"}:
        return "rejected"
    return None
