# ruff: noqa: D100
from __future__ import annotations

from _thread import LockType
from collections import deque
from dataclasses import dataclass, field
from threading import Lock, current_thread
from time import monotonic
from weakref import WeakKeyDictionary

from loguru import logger
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import QueuePool

_SLOW_MS = 500.0
_ERROR_MS = 2000.0
_REGISTRY_LOCK = Lock()


@dataclass(slots=True)
class _Counters:
    lock: LockType = field(default_factory=Lock)
    connects: int = 0
    checkouts: int = 0
    checkins: int = 0
    transactions: int = 0
    slow_transactions: int = 0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=512))


@dataclass(slots=True)
class _Timing:
    engine: Engine
    started: float
    operation: str
    task_id: int | None
    commit_started: float | None = None
    write_requested: float | None = None
    write_acquired: float | None = None
    committed: bool = False


_COUNTERS: WeakKeyDictionary[Engine, _Counters] = WeakKeyDictionary()


def install_database_telemetry(engine: Engine) -> None:
    """在当前引擎登记无 SQL 的连接计数与显式写锁计时，不改变连接池参数。"""
    with _REGISTRY_LOCK:
        if engine in _COUNTERS:
            return
        counters = _Counters()
        _COUNTERS[engine] = counters

    def connected(_connection: object, _record: object) -> None:
        with counters.lock:
            counters.connects += 1

    def checked_out(_connection: object, _record: object, _proxy: object) -> None:
        with counters.lock:
            counters.checkouts += 1

    def checked_in(_connection: object, _record: object) -> None:
        with counters.lock:
            counters.checkins += 1

    def before_cursor(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        timing = connection.info.get("_blc_transaction_timing")
        if isinstance(timing, _Timing) and statement.strip().upper() == "BEGIN IMMEDIATE":
            timing.write_requested = monotonic()

    def after_cursor(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        timing = connection.info.get("_blc_transaction_timing")
        if isinstance(timing, _Timing) and statement.strip().upper() == "BEGIN IMMEDIATE":
            timing.write_acquired = monotonic()

    def transaction_finished(connection: Connection) -> None:
        connection.info.pop("_blc_transaction_timing", None)

    event.listen(engine, "connect", connected)
    event.listen(engine, "checkout", checked_out)
    event.listen(engine, "checkin", checked_in)
    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "after_cursor_execute", after_cursor)
    event.listen(engine, "commit", transaction_finished)
    event.listen(engine, "rollback", transaction_finished)


def database_telemetry_snapshot(engine: Engine) -> dict[str, int | float | None]:
    """读取当前引擎的连接计数与最近 512 次事务延迟，不探测数据库文件。"""
    with _REGISTRY_LOCK:
        counters = _COUNTERS[engine]
    with counters.lock:
        durations = sorted(counters.durations)
        result: dict[str, int | float | None] = {
            "connects": counters.connects,
            "checkouts": counters.checkouts,
            "checkins": counters.checkins,
            "transactions": counters.transactions,
            "slow_transactions": counters.slow_transactions,
            "transaction_p95_ms": durations[(len(durations) - 1) * 95 // 100] if durations else None,
            "transaction_max_ms": durations[-1] if durations else None,
        }
    result["pool_checkedout"] = engine.pool.checkedout() if isinstance(engine.pool, QueuePool) else None
    result["pool_overflow"] = engine.pool.overflow() if isinstance(engine.pool, QueuePool) else None
    return result


def observe_session_telemetry(session: Session, *, operation: str, task_id: int | None) -> None:
    """按根事务记录实际提交或回滚结束，排除嵌套 SAVEPOINT 的中间结果。"""
    timings: dict[SessionTransaction, _Timing] = {}
    commit_starts: dict[SessionTransaction, float] = {}

    def begun(_db: Session, transaction: SessionTransaction, connection: Connection) -> None:
        if transaction.parent is not None:
            return
        timing = _Timing(
            connection.engine, monotonic(), operation, task_id, commit_started=commit_starts.get(transaction)
        )
        timings[transaction] = timing
        connection.info["_blc_transaction_timing"] = timing

    def committing(db: Session) -> None:
        if db.get_nested_transaction() is not None:
            return
        transaction = db.get_transaction()
        if transaction is not None:
            started = monotonic()
            commit_starts[transaction] = started
            if transaction in timings:
                timings[transaction].commit_started = started

    def committed(db: Session) -> None:
        if db.get_nested_transaction() is not None:
            return
        timing = timings.get(db.get_transaction())
        if timing is not None:
            timing.committed = True

    def ended(_db: Session, transaction: SessionTransaction) -> None:
        commit_starts.pop(transaction, None)
        timing = timings.pop(transaction, None)
        if timing is None:
            return
        finished = monotonic()
        duration_ms = max(0.0, finished - timing.started) * 1000
        with _REGISTRY_LOCK:
            counters = _COUNTERS[timing.engine]
        with counters.lock:
            counters.transactions += 1
            counters.durations.append(duration_ms)
            counters.slow_transactions += int(duration_ms >= _SLOW_MS)
        if duration_ms < _SLOW_MS:
            return
        wait_ms = (
            max(0.0, timing.write_acquired - timing.write_requested) * 1000
            if timing.write_acquired is not None and timing.write_requested is not None
            else None
        )
        commit_ms = (
            max(0.0, finished - timing.commit_started) * 1000
            if timing.committed and timing.commit_started is not None
            else None
        )
        pressure = database_telemetry_snapshot(timing.engine)
        logger.bind(_skip_db=True, operation=operation, task_id=task_id).log(
            "ERROR" if duration_ms >= _ERROR_MS else "WARNING",
            "db_tx_slow duration_ms={:.2f} commit_ms={} write_lock_wait_ms={} outcome={} "
            "pool_checkedout={} pool_overflow={} transaction_p95_ms={} transaction_max_ms={} "
            "checkouts={} checkins={} thread_name={}",
            duration_ms,
            commit_ms,
            wait_ms,
            "committed" if timing.committed else "rolled_back",
            pressure["pool_checkedout"],
            pressure["pool_overflow"],
            pressure["transaction_p95_ms"],
            pressure["transaction_max_ms"],
            pressure["checkouts"],
            pressure["checkins"],
            current_thread().name,
        )

    event.listen(session, "after_begin", begun)
    event.listen(session, "before_commit", committing)
    event.listen(session, "after_commit", committed)
    event.listen(session, "after_transaction_end", ended)
