"""发布阶段崩溃恢复 — 从 Durable Journal 回填远程成功结果。

重启时调用, 确保已发生但未持久化到 DB 的远程成功不丢失。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from app.core.media_usage import media_reference_scope
from app.db.entities import ClipStatus, FinalClip, UploadAttempt, UploadStatus, UploadTask
from app.db.session import get_session
from app.publishing.journal import mark_replayed, read_pending_entries

_logger = logging.getLogger(__name__)


@media_reference_scope()
def recover_publish_results() -> int:
    """从 Durable Journal 恢复远程成功结果到数据库。

    流程:
    1. 读取所有待回填 Journal 条目
    2. 按 attempt_token + publish_generation 查找对应 UploadAttempt
    3. 若 attempt 存在且非 SUCCESS → 更新为 SUCCESS + 更新 UploadTask
    4. 回填成功 → 从 Journal 中删除对应独立文件

    :returns: 恢复的条目数量。
    """
    entries = read_pending_entries()
    if not entries:
        return 0

    recovered = 0
    for entry in entries:
        attempt_token = entry.get("attempt_token")
        generation = entry.get("publish_generation")
        remote_id = entry.get("remote_id")
        remote_url = entry.get("remote_url")

        if not attempt_token or generation is None:
            _logger.warning("journal_entry_invalid: %s", entry)
            continue
        try:
            recorded_at = entry.get("journaled_at")
            if not isinstance(recorded_at, str):
                raise ValueError("成功回执缺少确认时间")
            confirmed_at = datetime.fromisoformat(recorded_at)
            if confirmed_at.tzinfo is None:
                raise ValueError("成功回执时间缺少 UTC 偏移")
            confirmed_at = confirmed_at.astimezone(UTC)
        except ValueError:
            _logger.error("journal_timestamp_invalid: token=%s gen=%s", attempt_token, generation)
            continue

        try:
            with get_session() as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                attempt = db.exec(
                    select(UploadAttempt).where(
                        UploadAttempt.attempt_token == attempt_token,
                        UploadAttempt.publish_generation == generation,
                    )
                ).first()

                if attempt is None:
                    _logger.warning("journal_attempt_not_found: token=%s gen=%s", attempt_token, generation)
                    continue

                if (
                    entry.get("upload_task_id") != attempt.upload_task_id
                    or entry.get("clip_id") != attempt.clip_id
                    or entry.get("platform") != attempt.platform
                    or entry.get("outcome") != "success"
                    or not isinstance(remote_id, str)
                    or not remote_id
                ):
                    _logger.error("journal_identity_mismatch: token=%s gen=%s", attempt_token, generation)
                    continue
                if attempt.status == UploadStatus.SUCCESS and (
                    attempt.remote_id != remote_id
                    or bool(attempt.remote_url and remote_url and attempt.remote_url != remote_url)
                ):
                    _logger.error("journal_receipt_conflict: token=%s gen=%s", attempt_token, generation)
                    continue
                newly_recovered = attempt.status != UploadStatus.SUCCESS

                # 回填
                attempt.status = UploadStatus.SUCCESS
                attempt.finished_at = confirmed_at if newly_recovered else attempt.finished_at or confirmed_at
                attempt.remote_id = remote_id
                attempt.remote_url = attempt.remote_url or remote_url
                attempt.error_type = attempt.error_message = None
                db.add(attempt)

                # 更新 UploadTask
                upload_task = db.get(UploadTask, attempt.upload_task_id)
                if upload_task is not None and upload_task.publish_generation == generation:
                    if upload_task.status != UploadStatus.SUCCESS or upload_task.remote_id != remote_id:
                        upload_task.updated_at = attempt.finished_at or datetime.now(UTC)
                    upload_task.status = UploadStatus.SUCCESS
                    upload_task.remote_id = remote_id
                    upload_task.last_error = None
                    upload_task.claimed_by = None
                    db.add(upload_task)
                clip = db.get(FinalClip, attempt.clip_id)
                if clip is not None:
                    clip.status = ClipStatus.PUBLISHED
                    db.add(clip)

                db.commit()
                recovered += int(newly_recovered)

                # 从 Journal 中删除
                mark_replayed(attempt_token, generation, remote_id=remote_id, remote_url=remote_url)

                _logger.info(
                    "journal_recovered: attempt=%s remote=%s gen=%s",
                    attempt_token,
                    remote_id,
                    generation,
                )

        except SQLAlchemyError:
            _logger.exception("journal_recovery_failed: token=%s", attempt_token)

    return recovered


def recover_publish_state() -> dict[str, int]:
    """先回放成功结果，再恢复过期请求，最后同步上传和流水线状态。"""
    from app.pipeline.stale_recovery import recover_stale_upload_attempts, sync_segment_task_from_attempt

    return {
        "journal": recover_publish_results(),
        "stale_attempts": recover_stale_upload_attempts(),
        "task_sync": sync_segment_task_from_attempt(),
    }
