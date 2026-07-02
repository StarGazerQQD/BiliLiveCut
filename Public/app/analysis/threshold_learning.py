"""阈值自学习模块(V0.1.2)。

当用户对候选进行审批(approve)/拒绝(reject)操作时,记录评分与阈值快照。
收集足够样本后,自动计算推荐阈值并更新房间配置。

算法:收集该房间所有已审批候选的高光评分,取 P15 分位数作为新阈值,
确保 85% 以上的人工认可候选不会被遗漏,同时去掉低分噪音。
"""

from __future__ import annotations

from app.core.config import settings
from app.db.models import HighlightCandidate, LiveRoom, ThresholdFeedback
from app.db.session import get_session


def record_feedback(room_id: int, candidate_id: int, action: str) -> ThresholdFeedback:
    """记录一次审批反馈(用于后续阈值自学习)。

    :param room_id: 直播间 db id。
    :param candidate_id: 候选 id。
    :param action: ``"approved"`` 或 ``"rejected"``。
    :returns: 新建的 :class:`ThresholdFeedback`。
    """
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        old_threshold = room.highlight_threshold if room else 0.65
        cand = db.get(HighlightCandidate, candidate_id)
        highlight_score = cand.highlight_score if cand else 0.0

    record = ThresholdFeedback(
        room_id=room_id,
        candidate_id=candidate_id,
        action=action,
        old_threshold=old_threshold,
        highlight_score=highlight_score,
    )
    with get_session() as db:
        db.add(record)
        db.flush()
        db.refresh(record)
    return record


def _collect_scores(room_id: int, action: str = "approved") -> list[float]:
    """收集某房间指定动作(默认 approved)的所有高光评分。

    :param room_id: 直播间 db id。
    :param action: ``"approved"`` 或 ``"rejected"``。
    :returns: 评分列表(升序)。
    """
    from sqlmodel import select

    with get_session() as db:
        rows = db.exec(
            select(ThresholdFeedback.highlight_score).where(
                ThresholdFeedback.room_id == room_id,
                ThresholdFeedback.action == action,
            )
        ).all()
    return sorted(rows)


def compute_recommended_threshold(room_id: int) -> float | None:
    """基于历史审批数据计算推荐阈值。

    逻辑:
    - 收集该房间所有 approved 候选的评分
    - 取 P15 分位数(即 85% 的认可候选高于此值)
    - 如有 rejected 候选,其最高分作为上界(不高于被拒最高分)
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

    # P15 分位数:第 index = len * 0.15 个元素。
    idx = max(0, int(len(good_scores) * 0.15))
    new_threshold = good_scores[idx]

    # rejected 的最高分作为上界(不允许高于被拒的分数)。
    bad_scores = _collect_scores(room_id, "rejected")
    if bad_scores:
        upper = min(bad_scores)  # 被拒的最低分作为天花板
        new_threshold = min(new_threshold, upper - 0.005)

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

    if not good:
        return {
            "enabled": settings_store.get_bool("threshold_learning_enabled"),
            "samples": 0,
            "ready": False,
            "current_threshold": _current_threshold(room_id),
            "recommended": None,
            "approved_range": None,
            "rejected_range": None,
        }

    rec = compute_recommended_threshold(room_id)
    return {
        "enabled": settings_store.get_bool("threshold_learning_enabled"),
        "samples": len(good) + len(bad),
        "ready": len(good) >= settings.threshold_learning_min_samples,
        "min_samples": settings.threshold_learning_min_samples,
        "current_threshold": _current_threshold(room_id),
        "recommended": rec,
        "approved_range": [round(good[0], 3), round(good[-1], 3)],
        "rejected_range": [round(bad[0], 3), round(bad[-1], 3)] if bad else None,
    }


def _current_threshold(room_id: int) -> float:
    """读取房间当前阈值。"""
    from sqlmodel import select

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        return room.highlight_threshold if room else 0.65
