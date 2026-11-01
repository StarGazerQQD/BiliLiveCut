# ruff: noqa: D100
from __future__ import annotations

from datetime import UTC, datetime
from threading import Event, Thread

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.db import session as database
from app.db.entities import AppSetting, UploadAttempt, UploadTask
from app.publishing.execution import UploadExecution, read_execution

_POLL_S = 5.0


class UploadOwnershipLost(RuntimeError):
    """发送之前已失去上传占用，尚未调用远端上传器。"""


def heartbeat_key(attempt: UploadAttempt) -> str:
    """上传尝试独占的持久心跳键，不能借给下一次发布。"""
    return f"upload_heartbeat:v1:{attempt.attempt_token}:{attempt.publish_generation}"


def last_activity(db: Session, attempt: UploadAttempt) -> datetime | None:
    """使用同一次尝试已提交的最新心跳，旧记录保留原始开始时间。"""
    row = db.get(AppSetting, heartbeat_key(attempt))
    return row.updated_at if row is not None else attempt.started_at


class UploadHeartbeat:
    """远端上传仍在执行时续租，结束时等待最后一次数据库写入退出。"""

    def __init__(self, binding: UploadExecution) -> None:
        self.binding = binding
        self.token = binding.token
        self.generation = binding.generation
        self.engine = database.engine
        self.stop = Event()
        self.thread = Thread(target=self._run, name=f"upload-hb-{self.token}", daemon=True)

    def __enter__(self) -> UploadHeartbeat:
        """确认当前尝试仍有占用后启动独立心跳。"""
        if not self._renew():
            raise UploadOwnershipLost("上传尝试已失效，不能开始外部请求")
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        """停止续租，并等待在途数据库写入实际退出。"""
        self.stop.set()
        self.thread.join()

    def _renew(self) -> bool:
        with Session(self.engine) as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            attempt = db.exec(
                select(UploadAttempt).where(
                    UploadAttempt.attempt_token == self.token,
                    UploadAttempt.publish_generation == self.generation,
                )
            ).first()
            upload = db.get(UploadTask, attempt.upload_task_id) if attempt is not None else None
            if (
                attempt is None
                or attempt.status != "in_progress"
                or upload is None
                or not self.binding.matches(attempt, upload)
                or read_execution(db, attempt) != self.binding
            ):
                return False
            key = heartbeat_key(attempt)
            row = db.get(AppSetting, key) or AppSetting(key=key, value='{"version":1}')
            row.updated_at = datetime.now(UTC)
            db.add(row)
            db.commit()
            return True

    def _run(self) -> None:
        while not self.stop.wait(_POLL_S):
            try:
                if not self._renew():
                    return
            except SQLAlchemyError:
                logger.bind(_skip_db=True).exception("upload_heartbeat_failed attempt={}", self.token)
