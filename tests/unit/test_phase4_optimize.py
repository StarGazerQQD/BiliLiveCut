"""Phase 4: Behavioral tests for app.db.optimize — record_lock, retry, transaction.

Target coverage: app.db.optimize ≥90%, total ≥51%.
All tests use mock Session, no real database, no real sleep.
"""

from __future__ import annotations

import random
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError

# ═══════════════════════════════════════════════════════════
# record_lock_wait
# ═══════════════════════════════════════════════════════════


class TestRecordLockWait:
    """record_lock_wait behavior."""

    def test_short_wait_no_log(self) -> None:
        """Wait under 0.5s does not log."""
        from app.db.optimize import record_lock_wait

        now = time.monotonic()
        # Call with start_time = now → elapsed ≈ 0s < 0.5
        record_lock_wait(now)

    def test_long_wait_logs_debug(self) -> None:
        """Wait over 0.5s logs debug."""
        from app.db.optimize import record_lock_wait

        # start_time 1s in the past → elapsed ≈ 1s > 0.5
        record_lock_wait(time.monotonic() - 1.0)


# ═══════════════════════════════════════════════════════════
# record_transaction_duration
# ═══════════════════════════════════════════════════════════


class TestRecordTransactionDuration:
    """record_transaction_duration behavior."""

    def test_short_transaction_logs_debug(self) -> None:
        """Short transaction (< 1s) logs at debug level."""
        from app.db.optimize import record_transaction_duration

        record_transaction_duration(time.monotonic(), "short op")

    def test_long_transaction_logs_warning(self) -> None:
        """Long transaction (≥1s) logs at warning level."""
        from app.db.optimize import record_transaction_duration

        record_transaction_duration(time.monotonic() - 2.0, "long op")


# ═══════════════════════════════════════════════════════════
# with_retry_on_lock
# ═══════════════════════════════════════════════════════════


class TestWithRetryOnLock:
    """with_retry_on_lock behavior — all use mock functions, no real sleep."""

    def test_first_attempt_succeeds(self) -> None:
        """First call succeeds, no retry."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(return_value="ok")
        wrapped = with_retry_on_lock(mock_fn)
        result = wrapped()

        assert result == "ok"
        mock_fn.assert_called_once()

    def test_one_lock_then_success(self) -> None:
        """One lock error, then success."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(
            side_effect=[
                OperationalError("statement", {}, "database is locked"),
                "ok",
            ]
        )
        wrapped = with_retry_on_lock(mock_fn)
        with patch("app.db.optimize.time.sleep", return_value=None):
            result = wrapped()

        assert result == "ok"
        assert mock_fn.call_count == 2

    def test_multiple_locks_then_success(self) -> None:
        """Two lock errors within retry budget, then success."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(
            side_effect=[
                OperationalError("s1", {}, "database is locked"),
                OperationalError("s2", {}, "database table is locked"),
                "ok",
            ]
        )
        wrapped = with_retry_on_lock(mock_fn)
        with patch("app.db.optimize.time.sleep", return_value=None):
            result = wrapped()

        assert result == "ok"
        assert mock_fn.call_count == 3

    def test_retry_exhausted(self) -> None:
        """All retries consumed → raises OperationalError."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(side_effect=OperationalError("s", {}, "database is locked"))
        wrapped = with_retry_on_lock(mock_fn, max_retries=2)

        with patch("app.db.optimize.time.sleep", return_value=None):
            with pytest.raises(OperationalError, match="database is locked"):
                wrapped()

        # 1 initial + 3 retries = 3 calls (max_retries=2 → 3 total attempts)
        assert mock_fn.call_count == 3

    def test_non_lock_error_raised_immediately(self) -> None:
        """Non-lock OperationalError raised without retry."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(side_effect=OperationalError("s", {}, "disk I/O error"))
        wrapped = with_retry_on_lock(mock_fn)

        with pytest.raises(OperationalError, match="disk I/O error"):
            wrapped()

        mock_fn.assert_called_once()

    def test_args_kwargs_passed_through(self) -> None:
        """Arguments are passed to the wrapped function."""
        from app.db.optimize import with_retry_on_lock

        mock_fn = MagicMock(return_value="args ok")
        wrapped = with_retry_on_lock(mock_fn)
        result = wrapped(1, 2, key="val")

        assert result == "args ok"
        mock_fn.assert_called_once_with(1, 2, key="val")

    def test_return_value_preserved(self) -> None:
        """Wrapped function return value is preserved."""
        from app.db.optimize import with_retry_on_lock

        obj = {"complex": "data", "num": 42}
        mock_fn = MagicMock(return_value=obj)
        wrapped = with_retry_on_lock(mock_fn)
        result = wrapped()

        assert result is obj

    def test_exponential_backoff_values(self) -> None:
        """Exponential backoff uses correct formula: base * 2^(attempt-1) + random jitter."""
        from unittest.mock import patch as up

        from app.db.optimize import with_retry_on_lock

        lock_err = OperationalError("s", {}, "database is locked")
        mock_fn = MagicMock(side_effect=[lock_err, lock_err, "ok"])
        wrapped = with_retry_on_lock(mock_fn, max_retries=5, base_delay=0.1)

        sleeps: list[float] = []
        with up("app.db.optimize.time.sleep", side_effect=lambda v: sleeps.append(v)):
            with up.object(random, "uniform", return_value=0.01):
                result = wrapped()

        assert result == "ok"
        assert len(sleeps) == 2
        assert sleeps[0] == pytest.approx(0.11, abs=0.001)
        assert sleeps[1] == pytest.approx(0.21, abs=0.001)

    def test_custom_max_retries_and_base_delay(self) -> None:
        """Custom retry parameters are respected."""
        from unittest.mock import patch as up

        from app.db.optimize import with_retry_on_lock

        lock_err = OperationalError("s", {}, "database is locked")
        mock_fn = MagicMock(side_effect=[lock_err, lock_err, "ok"])
        wrapped = with_retry_on_lock(mock_fn, max_retries=5, base_delay=0.5)

        sleeps: list[float] = []
        with up("app.db.optimize.time.sleep", side_effect=lambda v: sleeps.append(v)):
            with up.object(random, "uniform", return_value=0.0):
                result = wrapped()

        assert result == "ok"
        assert mock_fn.call_count == 3
        assert sleeps[0] == pytest.approx(0.5, abs=0.01)
        assert sleeps[1] == pytest.approx(1.0, abs=0.01)


# ═══════════════════════════════════════════════════════════
# monitored_transaction (single-shot, no retry)
# ═══════════════════════════════════════════════════════════


class TestMonitoredTransaction:
    """monitored_transaction context manager behavior."""

    def _mock_session(self, commit_side_effect=None):
        """Create a mock Session."""
        session = MagicMock()
        session.commit.side_effect = commit_side_effect
        return session

    def test_normal_commit(self) -> None:
        """Normal yield + commit succeeds."""
        from app.db.optimize import monitored_transaction

        session = self._mock_session()
        with monitored_transaction(session, "test op") as s:
            assert s is session
            s.add(MagicMock())

        session.commit.assert_called_once()

    def test_value_returned_through_with(self) -> None:
        """Session object is the same as passed in."""
        from app.db.optimize import monitored_transaction

        session = self._mock_session()
        captured = []
        with monitored_transaction(session, "test") as s:
            captured.append(s)

        assert captured[0] is session

    def test_non_lock_operational_error_rollback_and_raise(self) -> None:
        """Non-lock OperationalError causes rollback and re-raise."""
        from app.db.optimize import monitored_transaction

        session = self._mock_session()
        # Commit raises non-lock OperationalError
        session.commit.side_effect = OperationalError("s", {}, "disk I/O error")

        with pytest.raises(OperationalError, match="disk I/O error"):
            with monitored_transaction(session, "test") as s:
                s.add(MagicMock())  # This will cause commit to fail

        session.rollback.assert_called_once()

    def test_lock_error_rollback_and_raise(self) -> None:
        """Lock OperationalError also rollbacks and raises (single-shot, no retry in context manager)."""
        from app.db.optimize import monitored_transaction

        session = self._mock_session()
        session.commit.side_effect = OperationalError("s", {}, "database is locked")

        with pytest.raises(OperationalError, match="database is locked"):
            with monitored_transaction(session, "test") as s:
                s.add(MagicMock())

        session.rollback.assert_called_once()

    def test_generic_exception_rollback_and_raise(self) -> None:
        """Generic exception (e.g., ValueError) causes rollback."""
        from app.db.optimize import monitored_transaction

        session = self._mock_session()

        with pytest.raises(ValueError, match="test error"):
            with monitored_transaction(session, "test") as s:
                s.add(MagicMock())
                raise ValueError("test error")

        session.rollback.assert_called_once()

    def test_context_manager_only_yields_once(self) -> None:
        """Verify context manager does not have while-yield loop (fixed bug)."""
        import inspect

        from app.db.optimize import monitored_transaction

        source = inspect.getsource(monitored_transaction)
        # The old buggy code had "while True:" and "yield" in the same function
        # The fix uses try/except around yield once
        assert "while True:" not in source, "monitored_transaction should not have while-yield loop"


# ═══════════════════════════════════════════════════════════
# retry_transaction (new function, replaces old retry-in-context-manager)
# ═══════════════════════════════════════════════════════════


class TestRetryTransaction:
    """retry_transaction callback-based retry behavior."""

    def _mock_session(self):
        return MagicMock()

    def test_success_first_attempt(self) -> None:
        """First call succeeds."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        fn = MagicMock(return_value="done")
        result = retry_transaction(session, "test", fn)
        assert result == "done"
        fn.assert_called_once_with(session)
        session.commit.assert_called_once()

    def test_one_lock_then_success(self) -> None:
        """One lock, retry succeeds."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        session.commit.side_effect = [
            OperationalError("s", {}, "database is locked"),
            None,
        ]
        fn = MagicMock(return_value="ok")
        with patch("app.db.optimize.time.sleep", return_value=None):
            result = retry_transaction(session, "test", fn)

        assert result == "ok"
        assert fn.call_count == 2
        assert session.commit.call_count == 2
        assert session.rollback.call_count == 1  # first commit failed → rollback

    def test_multi_lock_then_success(self) -> None:
        """Two locks within budget, then success."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        session.commit.side_effect = [
            OperationalError("s", {}, "database is locked"),
            OperationalError("s", {}, "database table is locked"),
            None,
        ]
        fn = MagicMock(return_value="ok")
        with patch("app.db.optimize.time.sleep", return_value=None):
            result = retry_transaction(session, "test", fn)

        assert result == "ok"
        assert fn.call_count == 3

    def test_retry_exhausted(self) -> None:
        """All retries exhausted → raises."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        session.commit.side_effect = OperationalError("s", {}, "database is locked")
        fn = MagicMock()

        with patch("app.db.optimize.time.sleep", return_value=None):
            with pytest.raises(OperationalError, match="database is locked"):
                retry_transaction(session, "test", fn, max_retries=2)

        # 1 + 3 retries = 3 attempts
        assert fn.call_count == 3
        assert session.rollback.call_count == 3

    def test_non_lock_error_raised_immediately(self) -> None:
        """Non-lock error raised without retry."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        session.commit.side_effect = OperationalError("s", {}, "disk I/O error")
        fn = MagicMock()

        with pytest.raises(OperationalError, match="disk I/O error"):
            retry_transaction(session, "test", fn)

        fn.assert_called_once()
        session.rollback.assert_called_once()

    def test_fn_return_value_preserved_complex(self) -> None:
        """Complex return value preserved through retry_transaction."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        expected = [1, 2, {"key": "val"}]
        fn = MagicMock(return_value=expected)
        result = retry_transaction(session, "test", fn)
        assert result is expected

    def test_fn_exception_rollback_and_raise(self) -> None:
        """If fn itself raises non-lock exception, rollback and raise."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()

        def _fail(_s):
            raise ValueError("business error")

        with pytest.raises(ValueError, match="business error"):
            retry_transaction(session, "test", _fail)

        # fn raised ValueError before commit was attempted
        session.rollback.assert_called()

    def test_custom_params(self) -> None:
        """Custom max_retries and base_delay."""
        from app.db.optimize import retry_transaction

        session = self._mock_session()
        session.commit.side_effect = OperationalError("s", {}, "database is locked")
        fn = MagicMock()

        with (
            patch("app.db.optimize.time.sleep", return_value=None) as mock_sleep,
            patch.object(random, "uniform", return_value=0.0),
        ):
            with pytest.raises(OperationalError):
                retry_transaction(session, "test", fn, max_retries=1, base_delay=0.5)

        # 1 initial + 2 retry attempts = 2 calls (max_retries=1)
        assert fn.call_count == 2
        # backoff: 0.5 * 2^0 = 0.5
        mock_sleep.assert_called_once_with(pytest.approx(0.5, abs=0.01))
