# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, select

from app.core.cancellation import OperationCancelledError
from app.core.paths import storage_path_aliases
from app.core.process_control import ProcessCancelledError
from app.db.entities import CandidateStatus, ClipVariant, FinalClip, HighlightCandidate, HighlightEvent
from app.jobs.contracts import JobCancelled


@dataclass(slots=True)
class ClipDraft:
    """已完成计算、尚未正式发布的主片和派生版本。"""

    clip: FinalClip
    variants: list[ClipVariant]
    input_fingerprint: str
    artifacts: tuple[Path, ...] = ()


@contextmanager
def publication(artifacts: tuple[Path, ...]) -> Iterator[None]:
    """发布失败时清理本次未引用草稿，保留已经正式提交的文件。"""
    from app.db.session import get_session

    completed = False
    try:
        yield
        completed = True
    finally:
        if not completed and artifacts:
            interruption = sys.exception()
            try:
                with get_session() as db:
                    paths = [alias for path in artifacts for alias in storage_path_aliases(path)]
                    referenced = db.exec(
                        select(FinalClip.id).where(FinalClip.file_path.in_(paths) | FinalClip.cover_path.in_(paths))
                    ).first()
                    variant = db.exec(
                        select(ClipVariant.id).where(
                            ClipVariant.file_path.in_(paths) | ClipVariant.cover_path.in_(paths)
                        )
                    ).first()
                if referenced is None and variant is None:
                    for path in artifacts:
                        path.unlink(missing_ok=True)
            except (OSError, SQLAlchemyError) as cleanup_error:
                if isinstance(interruption, (OperationCancelledError, ProcessCancelledError, JobCancelled)):
                    logger.opt(exception=cleanup_error).error("出片取消后草稿清理失败，保留文件待清理")
                    raise interruption from cleanup_error
                raise


def _snapshot(model: SQLModel) -> dict[str, JsonValue]:
    values = model.model_dump(mode="json")
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, datetime):
            # SQLite 以 UTC-naive 读回时间，同一事实不能因对象是否刚写入而改变指纹。
            normalized = value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
            values[name] = normalized.isoformat(timespec="microseconds")
    return values


def input_fingerprint(db: Session, candidate_id: int) -> str:
    """记录领域输入快照，提交时拒绝人工修改后的迟到计算。"""
    candidate = db.get(HighlightCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"候选不存在: id={candidate_id}")
    event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
    data = {
        "candidate": _snapshot(candidate),
        "event": _snapshot(event) if event is not None else None,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def publish_draft(db: Session, draft: ClipDraft) -> FinalClip:
    """在调用方写事务内验证输入并登记全部正式结果，不自行提交。"""
    clip = draft.clip
    if input_fingerprint(db, clip.candidate_id) != draft.input_fingerprint:
        raise ValueError("候选或审核内容已改变，拒绝发布旧输入生成的成片")
    candidate = db.get(HighlightCandidate, clip.candidate_id)
    assert candidate is not None
    if candidate.status == CandidateStatus.REJECTED:
        raise ValueError("候选已被拒绝，不能发布成片")
    db.add(clip)
    db.flush()
    for variant in draft.variants:
        db.add(variant)
    candidate.status = CandidateStatus.CLIPPED
    db.add(candidate)
    db.flush()
    return clip
