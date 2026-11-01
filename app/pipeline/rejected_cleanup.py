# ruff: noqa: D100
from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, func, or_, update
from sqlmodel import Session, select

from app.core.cancellation import cancellation_requested
from app.core.paths import storage_path_aliases
from app.db.entities import AppSetting, CandidateStatus, ClipStatus, FinalClip, HighlightCandidate
from app.db.session import get_session

_BATCH_SIZE = 32
_CURSOR_KEY = "rejected_cleanup:cursor"
_RECEIPT_PREFIX = "rejected_cleanup:clip:"


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: int = Field(default=0, ge=0)
    clip_id: int = Field(default=0, ge=0)


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str
    cover_path: str | None


def _snapshot() -> list[tuple[int, FinalClip | None]]:
    with get_session(operation="rejected_cleanup_snapshot") as db:
        stored = db.get(AppSetting, _CURSOR_KEY)
        cursor = _Cursor.model_validate_json(stored.value) if stored else _Cursor()
        rows = db.exec(
            select(HighlightCandidate.id, FinalClip)
            .outerjoin(FinalClip, FinalClip.candidate_id == HighlightCandidate.id)
            .where(
                HighlightCandidate.status == CandidateStatus.REJECTED,
                or_(
                    HighlightCandidate.id > cursor.candidate_id,
                    (HighlightCandidate.id == cursor.candidate_id) & (FinalClip.id > cursor.clip_id),
                ),
            )
            .order_by(HighlightCandidate.id, FinalClip.id)
            .limit(_BATCH_SIZE)
        ).all()
        return [(candidate_id, clip) for candidate_id, clip in rows]


def _can_delete(db: Session, candidate_id: int, snapshot: FinalClip, aliases: tuple[str, ...]) -> bool:
    from app.pipeline.storage_lifecycle import _clip_is_protected

    candidate = db.get(HighlightCandidate, candidate_id)
    current = db.get(FinalClip, snapshot.id)
    if (
        candidate is None
        or candidate.status != CandidateStatus.REJECTED
        or current is None
        or current.candidate_id != candidate_id
        or (current.file_path, current.cover_path) != (snapshot.file_path, snapshot.cover_path)
    ):
        return False
    stored = db.get(AppSetting, f"{_RECEIPT_PREFIX}{snapshot.id}")
    if stored is not None:
        receipt = _Receipt.model_validate_json(stored.value)
        if (receipt.file_path, receipt.cover_path) == (snapshot.file_path, snapshot.cover_path):
            return False
    return not _clip_is_protected(db, current, aliases=aliases)


def _save_result(cursor: _Cursor, successful: list[FinalClip], candidates: set[int]) -> None:
    with get_session(operation="rejected_cleanup_result") as db:
        for snapshot in successful:
            current = db.get(FinalClip, snapshot.id)
            if current is None or (current.file_path, current.cover_path) != (snapshot.file_path, snapshot.cover_path):
                continue
            db.merge(
                AppSetting(
                    key=f"{_RECEIPT_PREFIX}{snapshot.id}",
                    value=_Receipt(
                        file_path=snapshot.file_path,
                        cover_path=snapshot.cover_path,
                    ).model_dump_json(),
                )
            )
        db.merge(AppSetting(key=_CURSOR_KEY, value=cursor.model_dump_json()))
        db.flush()
        completed_scan = set(candidates)
        if (
            cursor.candidate_id in completed_scan
            and db.exec(
                select(FinalClip.id)
                .where(
                    FinalClip.candidate_id == cursor.candidate_id,
                    FinalClip.id > cursor.clip_id,
                )
                .limit(1)
            ).first()
            is not None
        ):
            completed_scan.remove(cursor.candidate_id)
        if not completed_scan:
            return
        # 旧批次的成功记录只证明其绑定路径已删除；新成片或新封面必须有自己的成功记录。
        pending = (
            select(FinalClip.id)
            .outerjoin(AppSetting, AppSetting.key == _RECEIPT_PREFIX + cast(FinalClip.id, String))
            .where(
                FinalClip.candidate_id == HighlightCandidate.id,
                or_(
                    AppSetting.key.is_(None),
                    func.json_extract(AppSetting.value, "$.file_path").is_distinct_from(FinalClip.file_path),
                    func.json_extract(AppSetting.value, "$.cover_path").is_distinct_from(FinalClip.cover_path),
                    FinalClip.status.in_([ClipStatus.PUBLISHED, ClipStatus.READY, ClipStatus.REVIEWING]),
                ),
            )
            .exists()
        )
        db.execute(
            update(HighlightCandidate)
            .where(
                HighlightCandidate.id.in_(completed_scan),
                HighlightCandidate.status == CandidateStatus.REJECTED,
                ~pending,
            )
            .values(status=CandidateStatus.CLEANED)
        )


def cleanup_batch(allowed_root: Path) -> int:
    """在调用方的独占媒体门内执行固定上限批次，文件操作不持有数据库连接。"""
    from app.pipeline.storage_lifecycle import _safe_unlink

    if cancellation_requested():
        return 0
    rows = _snapshot()
    if not rows:
        _save_result(_Cursor(), [], set())
        return 0
    aliases_by_clip: dict[int, tuple[str, ...]] = {}
    for _, clip in rows:
        if cancellation_requested():
            return 0
        if clip is not None:
            aliases_by_clip[clip.id] = tuple(
                alias for path in (clip.file_path, clip.cover_path) if path for alias in storage_path_aliases(path)
            )
    # 独占门内一次复核本批引用；关闭连接后才开始可能受慢存储阻塞的删除。
    with get_session(operation="rejected_cleanup_protection") as db:
        deletable_ids = {
            clip.id
            for candidate_id, clip in rows
            if clip is not None and _can_delete(db, candidate_id, clip, aliases_by_clip[clip.id])
        }
    successful: list[FinalClip] = []
    candidates: set[int] = set()
    cursor: _Cursor | None = None
    cleaned = 0
    for candidate_id, clip in rows:
        if cancellation_requested():
            break
        cursor = _Cursor(candidate_id=candidate_id, clip_id=clip.id if clip is not None else 0)
        candidates.add(candidate_id)
        if clip is None:
            continue
        if clip.id not in deletable_ids or cancellation_requested():
            continue
        removed = not clip.file_path or _safe_unlink(clip.file_path, allowed_root)
        if clip.file_path and removed:
            cleaned += 1
        if cancellation_requested():
            break
        cover_removed = not clip.cover_path or _safe_unlink(clip.cover_path, allowed_root)
        if removed and cover_removed:
            successful.append(clip)
    if cursor is not None:
        _save_result(cursor, successful, candidates)
    if cleaned:
        logger.info("本批清理 {} 个被拒绝候选的切片文件，后续维护按游标继续。", cleaned)
    return cleaned
