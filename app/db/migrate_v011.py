"""V0.1.11-alpha 数据迁移:修复旧数据中 ClipVariant/HightlightTopic 的 event_id。

迁移流程:
1. 为每个 HighlightCandidate 查找/创建其 HighlightEvent。
2. 修复 ClipVariant.event_id (旧值=Candidate ID) -> 真实 Event ID。
3. 修复 HighlightTopic.event_id -> 真实 Event ID。
4. 输出统计,对无法转换的数据记录警告。
"""

from __future__ import annotations

from loguru import logger
from sqlmodel import select

from app.db.models import HighlightCandidate, HighlightEvent, ClipVariant, HighlightTopic
from app.db.session import get_session


def migrate_v011_1(db) -> dict[int, int]:
    """为所有 Candidate 创建/关联 HighlightEvent,返回 {candidate_id: event_id} 映射。"""
    mapping: dict[int, int] = {}
    candidates = db.exec(select(HighlightCandidate)).all()
    for cand in candidates:
        if cand.id is None:
            continue
        # 已有 Event?
        existing = db.exec(
            select(HighlightEvent).where(HighlightEvent.candidate_id == cand.id)
        ).first()
        if existing is not None:
            mapping[cand.id] = existing.id
        else:
            from app.db.models import ReviewStatus
            event = HighlightEvent(
                candidate_id=cand.id,
                session_id=cand.session_id,
                raw_start_ts=cand.start_ts,
                raw_end_ts=cand.end_ts,
                rule_score=cand.rule_score,
                llm_score=cand.llm_score,
                highlight_score=cand.highlight_score,
                features_json=cand.features_json,
                reason=cand.reason,
                review_status=ReviewStatus.PENDING,
                review_by="auto",
            )
            db.add(event)
            db.flush()
            db.refresh(event)
            if event.id is not None:
                mapping[cand.id] = event.id
    return mapping


def run_migration() -> dict:
    """执行 v0.1.11-alpha 数据迁移并输出统计。

    :returns: 迁移统计字典。
    """
    stats = {
        "events_created": 0,
        "clipvariants_fixed": 0,
        "clipvariants_skipped": 0,
        "topic_fixed": 0,
        "topic_skipped": 0,
    }

    with get_session() as db:
        # 步骤1:建立 Candidate -> Event 映射。
        mapping = migrate_v011_1(db)
        stats["events_created"] = len(mapping)
        logger.info("迁移:创建/关联 {} 个 HighlightEvent。", len(mapping))

        # 步骤2:修复 ClipVariant.event_id。
        variants = db.exec(select(ClipVariant)).all()
        for v in variants:
            old_eid = v.event_id
            # 如果 event_id 碰巧是 Candidate ID (旧数据特征)。
            cand = db.get(HighlightCandidate, old_eid)
            if cand is not None:
                real_eid = mapping.get(old_eid)
                if real_eid is not None and real_eid != old_eid:
                    v.event_id = real_eid
                    db.add(v)
                    stats["clipvariants_fixed"] += 1
                else:
                    stats["clipvariants_skipped"] += 1
            else:
                # event_id 可能已经是真实 Event ID,跳过。
                stats["clipvariants_skipped"] += 1

        logger.info(
            "迁移:修复 {} 个 ClipVariant.event_id,跳过 {} 个。",
            stats["clipvariants_fixed"], stats["clipvariants_skipped"],
        )

        # 步骤3:修复 HighlightTopic.event_id。
        members = db.exec(select(HighlightTopic)).all()
        for m in members:
            cand = db.get(HighlightCandidate, m.event_id)
            if cand is not None:
                real_eid = mapping.get(m.event_id)
                if real_eid is not None and real_eid != m.event_id:
                    m.event_id = real_eid
                    db.add(m)
                    stats["topic_fixed"] += 1
                else:
                    stats["topic_skipped"] += 1
            else:
                stats["topic_skipped"] += 1

        logger.info(
            "迁移:修复 {} 个 HighlightTopic.event_id,跳过 {} 个。",
            stats["topic_fixed"], stats["topic_skipped"],
        )

    return stats
