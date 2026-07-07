"""SQLite 性能优化工具:事务监控与锁重试。

提供:
- 事务等待/耗时日志记录;
- ``database is locked`` 指数退避重试;
- 带监控的事务上下文管理器。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

_F = TypeVar("_F", bound=Callable[..., Any])

# 可重试的 SQLite 锁错误关键词。
_LOCK_KEYWORDS = ("database is locked", "database table is locked")


def record_lock_wait(start_time: float) -> None:
    """记录事务因锁等待的时间。

    应在事务开始时调用,计算自 ``start_time`` 到当前时间的等待耗时并记录。

    :param start_time: 事务开始的时间戳(取自 ``time.monotonic()``)。
    """
    elapsed = time.monotonic() - start_time
    if elapsed > 0.5:
        logger.debug("事务锁等待: {:.2f}s", elapsed)


def record_transaction_duration(start_time: float, operation: str) -> None:
    """记录事务从开始到完成的耗时。

    :param start_time: 事务开始的时间戳(取自 ``time.monotonic()``)。
    :param operation: 操作描述,如 ``"插入高光候选"``。
    """
    elapsed = time.monotonic() - start_time
    if elapsed > 1.0:
        logger.warning("长事务 [{}] 耗时 {:.2f}s", operation, elapsed)
    else:
        logger.debug("事务 [{}] 耗时 {:.2f}s", operation, elapsed)


def with_retry_on_lock(
    func: _F,
    max_retries: int = 3,
    base_delay: float = 0.1,
) -> _F:
    """为可调用对象添加 ``database is locked`` 错误的重试机制。

    在每次重试前执行指数退避 + 随机抖动,避免锤击效应。
    若 ``max_retries`` 次后仍失败,则原样抛出异常。

    :param func: 需要重试保护的可调用对象。
    :param max_retries: 最大重试次数(不含首次尝试)。
    :param base_delay: 基础退避秒数。
    :returns: 包装后的可调用对象(签名同 func)。
    :raises OperationalError: 所有重试耗尽后仍因锁失败时。
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        attempt: int = 0
        while True:
            try:
                return func(*args, **kwargs)
            except OperationalError as exc:
                error_msg = str(exc)
                if not any(kw in error_msg for kw in _LOCK_KEYWORDS):
                    raise
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "数据库锁重试耗尽 (attempts={}/{}): {}",
                        attempt,
                        max_retries + 1,
                        exc,
                    )
                    raise
                sleep_s = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
                logger.warning(
                    "数据库锁冲突, {:.2f}s 后重试 (attempt {}/{})",
                    sleep_s,
                    attempt,
                    max_retries + 1,
                )
                time.sleep(sleep_s)

    return wrapper  # type: ignore[return-value]


@contextmanager
def monitored_transaction(db: Session, operation: str) -> Iterator[Session]:
    """带监控与锁重试保护的数据库事务上下文管理器。

    用法::

        with monitored_transaction(db, "更新切片状态") as session:
            session.add(clip)
            # 自动提交并在锁冲突时重试

    :param db: 数据库会话。
    :param operation: 操作描述,用于日志。
    :yields: 传入的会话对象。
    :raises OperationalError: 多次重试失败后抛出锁错误。
    """
    start = time.monotonic()
    record_lock_wait(start)

    attempt: int = 0
    max_retries: int = 3
    base_delay: float = 0.1
    while True:
        try:
            yield db
            db.commit()
            record_transaction_duration(start, operation)
            return
        except OperationalError as exc:
            error_msg = str(exc)
            if not any(kw in error_msg for kw in _LOCK_KEYWORDS):
                db.rollback()
                raise
            db.rollback()
            attempt += 1
            if attempt > max_retries:
                logger.error(
                    "monitored_transaction [{}] 重试耗尽 ({}/{}): {}",
                    operation,
                    attempt,
                    max_retries + 1,
                    exc,
                )
                raise
            sleep_s = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
            logger.warning(
                "monitored_transaction [{}] 锁冲突, {:.2f}s 后重试 (attempt {}/{})",
                operation,
                sleep_s,
                attempt,
                max_retries + 1,
            )
            time.sleep(sleep_s)
