"""SQLite 性能优化工具: 事务监控与锁重试。

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

    :param start_time: 事务开始的时间戳 (monotonic)。
    """
    elapsed = time.monotonic() - start_time
    if elapsed > 0.5:
        logger.debug("Transaction lock wait: {:.2f}s", elapsed)


def record_transaction_duration(start_time: float, operation: str) -> None:
    """记录事务从开始到完成的耗时。

    :param start_time: 事务开始的时间戳 (monotonic)。
    :param operation: 操作描述。
    """
    elapsed = time.monotonic() - start_time
    if elapsed > 1.0:
        logger.warning("Long transaction [{}] {:.2f}s", operation, elapsed)
    else:
        logger.debug("Transaction [{}] {:.2f}s", operation, elapsed)


def with_retry_on_lock(
    func: _F,
    max_retries: int = 3,
    base_delay: float = 0.1,
) -> _F:
    """为可调用对象添加 ``database is locked`` 错误的重试机制。

    每次重试前指数退避 + 随机抖动。max_retries 次后仍失败则抛出异常。

    :param func: 需要重试保护的可调用对象。
    :param max_retries: 最大重试次数 (不含首次尝试)。
    :param base_delay: 基础退避秒数。
    :returns: 包装后的可调用对象。
    :raises OperationalError: 重试耗尽。
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
                        "DB lock retry exhausted (attempts={}/{}): {}",
                        attempt,
                        max_retries + 1,
                        exc,
                    )
                    raise
                sleep_s = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
                logger.warning(
                    "DB lock conflict, retry in {:.2f}s (attempt {}/{})",
                    sleep_s,
                    attempt,
                    max_retries + 1,
                )
                time.sleep(sleep_s)

    return wrapper  # type: ignore[return-value]


@contextmanager
def monitored_transaction(db: Session, operation: str) -> Iterator[Session]:
    """带监控的数据库事务上下文管理器 (单次, 不自动重试)。

    V0.1.14.11 fix: context manager 只能 yield 一次。
    需要锁重试请使用 ``retry_transaction`` 包装整个 with 块。

    用法::

        with monitored_transaction(db, "update") as session:
            session.add(clip)

    :param db: 数据库会话。
    :param operation: 操作描述。
    :yields: 传入的会话对象。
    """
    start = time.monotonic()
    record_lock_wait(start)
    try:
        yield db
        db.commit()
        record_transaction_duration(start, operation)
    except Exception:
        db.rollback()
        raise


def retry_transaction(
    db: Session,
    operation: str,
    fn: Callable[[Session], Any],
    max_retries: int = 3,
    base_delay: float = 0.1,
) -> Any:
    """在锁冲突时重试整个事务操作 (V0.1.14.11 新增)。

    用法::

        def do_work(session):
            session.add(clip)

        retry_transaction(db, "update", do_work)

    :param db: 数据库会话。
    :param operation: 操作描述。
    :param fn: 接收 Session 的回调函数。
    :param max_retries: 最大重试次数。
    :param base_delay: 基础退避秒数。
    :returns: fn 的返回值。
    :raises OperationalError: 重试耗尽。
    """
    start = time.monotonic()
    record_lock_wait(start)
    attempt: int = 0
    while True:
        try:
            result = fn(db)
            db.commit()
            record_transaction_duration(start, operation)
            return result
        except OperationalError as exc:
            db.rollback()
            error_msg = str(exc)
            if not any(kw in error_msg for kw in _LOCK_KEYWORDS):
                raise
            attempt += 1
            if attempt > max_retries:
                logger.error(
                    "retry_transaction [{}] exhausted ({}/{}): {}",
                    operation,
                    attempt,
                    max_retries + 1,
                    exc,
                )
                raise
            sleep_s = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05)
            logger.warning(
                "retry_transaction [{}] lock conflict, retry in {:.2f}s (attempt {}/{})",
                operation,
                sleep_s,
                attempt,
                max_retries + 1,
            )
            time.sleep(sleep_s)
        except Exception:
            db.rollback()
            raise
