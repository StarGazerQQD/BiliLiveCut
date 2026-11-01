# ruff: noqa: D100
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import event

from app.core.notifications import Change, notifications
from app.db.entities import AppSetting
from app.jobs.contracts import decode_job

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.orm import Session, SessionTransaction, UOWTransaction

_REVIEW_TABLES = {
    "highlight_candidates",
    "highlight_events",
    "highlight_topics",
    "final_clips",
    "clip_variants",
    "topics",
}


def observe_commits(session: Session) -> None:
    """仅在最外层事务提交后发布提示；嵌套回滚丢弃对应通知。"""
    if session.info.get("_blc_commit_observed"):
        return
    session.info["_blc_commit_observed"] = True
    pending: dict[SessionTransaction, set[Change]] = {}
    logs: dict[SessionTransaction, list[Callable[[], None]]] = session.info.setdefault("_blc_commit_logs", {})

    def before_flush(db: Session, _context: UOWTransaction, _instances: Sequence[object] | None) -> None:
        transaction = db.get_nested_transaction() or db.get_transaction()
        if transaction is None:
            raise RuntimeError("写入通知缺少事务")
        changes = pending.setdefault(transaction, set())
        for item in (*db.new, *db.dirty, *db.deleted):
            if item in db.dirty and not db.is_modified(item, include_collections=False):
                continue
            changes.add(Change("state"))
            if isinstance(item, AppSetting) and item.key.startswith("web_job:"):
                try:
                    owner = decode_job(item.value)["owner"]
                except ValueError:
                    # 损坏记录不能确定所属身份；不扩大发送范围，HTTP 仍报告原存储错误。
                    logger.warning("作业通知缺少有效身份，需通过 HTTP 核查持久记录")
                else:
                    changes.add(Change("jobs", owner))
            elif getattr(item, "__tablename__", "") in _REVIEW_TABLES:
                changes.add(Change("review"))

    def committed(db: Session) -> None:
        transaction = db.get_nested_transaction() or db.get_transaction()
        changes = pending.pop(transaction, set())
        callbacks = logs.pop(transaction, [])
        if transaction is not None and transaction.nested and transaction.parent is not None:
            pending.setdefault(transaction.parent, set()).update(changes)
            logs.setdefault(transaction.parent, []).extend(callbacks)
        else:
            notifications.publish(changes)
            pending.clear()
            logs.clear()
            for callback in callbacks:
                callback()

    def rolled_back(_db: Session, transaction: SessionTransaction) -> None:
        pending.pop(transaction, None)
        logs.pop(transaction, None)
        if transaction.parent is None:
            pending.clear()
            logs.clear()

    event.listen(session, "before_flush", before_flush)
    event.listen(session, "after_commit", committed)
    event.listen(session, "after_soft_rollback", rolled_back)


def log_after_commit(session: Session, callback: Callable[[], None]) -> None:
    """把成功日志归属到当前事务，外层提交后输出、回滚时丢弃。"""
    observe_commits(session)
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        raise RuntimeError("成功日志缺少所属事务")
    logs: dict[SessionTransaction, list[Callable[[], None]]] = session.info["_blc_commit_logs"]
    logs.setdefault(transaction, []).append(callback)
