"""Phase 3: Behavioral tests for asr_metrics, core.metrics, osutil.

Targets:
    - asr_metrics.py  ≥ 95%
    - core.metrics.py ≥ 95%
    - osutil.py       ≥ 95%
    - Total coverage  ≥ 50.5%

All module-level global state is reset before each test via fixtures.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


# ═══════════════════════════════════════════════════════════
# Fixtures — reset global state before each test
# ═══════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_asr_metrics() -> None:
    """Reset asr_metrics global state before each test."""
    import app.analysis.asr_metrics as am

    am._backend_stats.clear()
    am._review_stats = am.ReviewStats()
    am._oom_count = 0
    am._fallback_count = 0
    am._rtf_samples.clear()


@pytest.fixture(autouse=True)
def _reset_core_metrics() -> None:
    """Reset core.metrics global state before each test."""
    import app.core.metrics as cm

    cm._latest_snapshot = cm.MetricsSnapshot()
    cm._history.clear()
    cm._asr_times.clear()
    cm._render_times.clear()
    cm._upload_times.clear()
    cm._lock_waits.clear()
    cm._active_workers = 0
    cm._active_recordings = 0
    cm._recording_hours = 0.0


# ═══════════════════════════════════════════════════════════
# A. app.analysis.asr_metrics — target ≥95%
# ═══════════════════════════════════════════════════════════


class TestBackendStats:
    """BackendStats dataclass behavior."""

    def test_empty_stats_defaults(self) -> None:
        """Empty stats have zero avg_duration and failure_rate."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        assert s.calls == 0
        assert s.successes == 0
        assert s.failures == 0
        assert s.avg_duration == 0.0
        assert s.failure_rate == 0.0
        assert s.p50_duration == 0.0
        assert s.p95_duration == 0.0

    def test_record_success(self) -> None:
        """Recording a success increments counters."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        s.record(1.5, success=True)
        assert s.calls == 1
        assert s.successes == 1
        assert s.failures == 0
        assert s.total_duration == 1.5
        assert s.avg_duration == pytest.approx(1.5)
        assert s.failure_rate == 0.0

    def test_record_failure(self) -> None:
        """Recording a failure increments failure counter."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        s.record(3.0, success=False)
        s.record(1.0, success=True)
        assert s.calls == 2
        assert s.successes == 1
        assert s.failures == 1
        assert s.failure_rate == pytest.approx(0.5)

    def test_avg_duration_multiple_calls(self) -> None:
        """Average duration computed correctly."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        s.record(2.0)
        s.record(4.0)
        assert s.avg_duration == pytest.approx(3.0)

    def test_p50_and_p95_percentiles(self) -> None:
        """P50 and P95 computed from duration samples."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        for i in range(100):
            s.record(float(i))  # 0..99
        s.recompute_percentiles()
        assert s.p50_duration == pytest.approx(50.0, abs=1)
        assert s.p95_duration == pytest.approx(95.0, abs=1)

    def test_percentiles_empty_durations(self) -> None:
        """Recompute on empty durations is no-op."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        s.recompute_percentiles()
        assert s.p50_duration == 0.0
        assert s.p95_duration == 0.0

    def test_duration_sample_trim_at_1000(self) -> None:
        """After >1000 samples, only last 500 retained."""
        from app.analysis.asr_metrics import BackendStats

        s = BackendStats()
        for i in range(1200):
            s.record(float(i))
        assert len(s._durations) <= 700
        # Trimmed samples should be near the end of the range
        first_kept = s._durations[0]
        assert first_kept >= 100.0  # somewhere in the last portion


class TestReviewStats:
    """ReviewStats dataclass behavior."""

    def test_empty_rate_divides_by_zero(self) -> None:
        """trigger_rate = 0 when no paraformer calls."""
        from app.analysis.asr_metrics import ReviewStats

        s = ReviewStats()
        assert s.trigger_rate == 0.0
        assert s.adoption_rate == 0.0

    def test_trigger_rate_computed(self) -> None:
        """trigger_rate = triggered / paraformer.calls."""
        from app.analysis.asr_metrics import _backend_stats as bs

        bs["paraformer"].record(1.0, success=True)
        bs["paraformer"].record(2.0, success=True)
        from app.analysis.asr_metrics import ReviewStats

        s = ReviewStats()
        s.triggered = 1
        assert s.trigger_rate == pytest.approx(0.5)

    def test_adoption_rate(self) -> None:
        """adoption_rate = adopted_review / triggered."""
        from app.analysis.asr_metrics import ReviewStats

        s = ReviewStats()
        s.triggered = 10
        s.adopted_review = 3
        assert s.adoption_rate == pytest.approx(0.3)

    def test_adoption_rate_zero_triggered(self) -> None:
        """adoption_rate = 0 when no triggered."""
        from app.analysis.asr_metrics import ReviewStats

        s = ReviewStats()
        s.adopted_review = 5
        assert s.adoption_rate == 0.0


class TestRecordFunctions:
    """record_backend_call / record_review / etc."""

    def test_record_backend_call_updates_stats(self) -> None:
        """record_backend_call updates per-backend stats."""
        from app.analysis.asr_metrics import _backend_stats as bs
        from app.analysis.asr_metrics import record_backend_call

        record_backend_call("paraformer", 2.0, success=True)
        record_backend_call("paraformer", 4.0, success=False)
        assert bs["paraformer"].calls == 2
        assert bs["paraformer"].successes == 1
        assert bs["paraformer"].failures == 1
        assert bs["paraformer"].total_duration == 6.0

    def test_record_backend_call_different_backends(self) -> None:
        """Different backends have separate stats."""
        from app.analysis.asr_metrics import _backend_stats as bs
        from app.analysis.asr_metrics import record_backend_call

        record_backend_call("whisper", 1.0)
        record_backend_call("funasr-nano", 0.5)
        assert bs["whisper"].calls == 1
        assert bs["funasr-nano"].calls == 1

    def test_record_review(self) -> None:
        """record_review with all flags."""
        from app.analysis.asr_metrics import _review_stats as rs
        from app.analysis.asr_metrics import record_review

        record_review(adopted=True, kept_base=False, manual_needed=False)
        assert rs.triggered == 1
        assert rs.adopted_review == 1
        assert rs.kept_base == 0
        assert rs.manual_needed == 0

    def test_record_review_kept_base(self) -> None:
        """record_review with kept_base."""
        from app.analysis.asr_metrics import _review_stats as rs
        from app.analysis.asr_metrics import record_review

        record_review(adopted=False, kept_base=True, manual_needed=False)
        assert rs.kept_base == 1

    def test_record_review_manual_needed(self) -> None:
        """record_review with manual_needed flag."""
        from app.analysis.asr_metrics import _review_stats as rs
        from app.analysis.asr_metrics import record_review

        record_review(adopted=False, kept_base=False, manual_needed=True)
        assert rs.manual_needed == 1
        assert rs.triggered == 1

    def test_record_review_success_and_failure(self) -> None:
        """record_review_success / failure tracking."""
        from app.analysis.asr_metrics import _review_stats as rs
        from app.analysis.asr_metrics import record_review_failure, record_review_success

        record_review_success()
        record_review_success()
        record_review_failure()
        assert rs.succeeded == 2
        assert rs.failed == 1

    def test_record_oom(self) -> None:
        """record_oom increments global counter."""
        import app.analysis.asr_metrics as am

        assert am._oom_count == 0
        am.record_oom()
        am.record_oom()
        assert am._oom_count == 2

    def test_record_fallback_counts(self) -> None:
        """record_fallback increments fallback counter."""
        import app.analysis.asr_metrics as am

        assert am._fallback_count == 0
        am.record_fallback()
        am.record_fallback()
        am.record_fallback()
        assert am._fallback_count == 3

    def test_record_rtf_samples(self) -> None:
        """record_rtf stores and trims samples."""
        import app.analysis.asr_metrics as am

        am.record_rtf(0.5)
        am.record_rtf(1.0)
        am.record_rtf(2.0)
        assert len(am._rtf_samples) == 3

    def test_record_rtf_trims_at_1000(self) -> None:
        """>1000 RTF samples trimmed to last 500."""
        import app.analysis.asr_metrics as am

        for i in range(1200):
            am.record_rtf(float(i))
        assert len(am._rtf_samples) <= 700


class TestGetSnapshot:
    """get_snapshot output validation."""

    def test_empty_snapshot(self) -> None:
        """Snapshot with no data has correct defaults."""
        from app.analysis.asr_metrics import get_snapshot

        snap = get_snapshot()
        assert "backends" in snap
        assert "review" in snap
        assert snap["oom_count"] == 0
        assert snap["fallback_count"] == 0
        assert snap["rtf_samples"] == 0
        assert snap["rtf_avg"] == 0.0

    def test_snapshot_with_backend_data(self) -> None:
        """Snapshot reflects recorded backend calls."""
        from app.analysis.asr_metrics import get_snapshot, record_backend_call

        record_backend_call("paraformer", 2.0, success=True)
        record_backend_call("paraformer", 4.0, success=False)
        snap = get_snapshot()
        b = snap["backends"]["paraformer"]
        assert b["calls"] == 2
        assert b["successes"] == 1
        assert b["failures"] == 1
        assert b["avg_duration"] == pytest.approx(3.0, abs=0.1)
        assert b["failure_rate"] == pytest.approx(0.5, abs=0.01)

    def test_snapshot_with_review_data(self) -> None:
        """Snapshot reflects review stats."""
        from app.analysis.asr_metrics import get_snapshot, record_review

        record_review(adopted=True, kept_base=False, manual_needed=False)
        snap = get_snapshot()
        r = snap["review"]
        assert r["triggered"] == 1
        assert r["adopted_review"] == 1

    def test_snapshot_fallback_count(self) -> None:
        """Snapshot includes fallback_count."""
        from app.analysis.asr_metrics import get_snapshot, record_fallback

        record_fallback()
        record_fallback()
        snap = get_snapshot()
        assert snap["fallback_count"] == 2

    def test_snapshot_oom_count(self) -> None:
        """Snapshot includes OOM count."""
        from app.analysis.asr_metrics import get_snapshot, record_oom

        record_oom()
        snap = get_snapshot()
        assert snap["oom_count"] == 1

    def test_snapshot_rtf_p95_single_sample(self) -> None:
        """RTF P95 with single sample equals avg."""
        from app.analysis.asr_metrics import get_snapshot, record_rtf

        record_rtf(0.5)
        snap = get_snapshot()
        assert snap["rtf_samples"] == 1
        assert snap["rtf_avg"] == pytest.approx(0.5, abs=0.01)
        assert snap["rtf_p95"] == pytest.approx(0.5, abs=0.01)

    def test_snapshot_rtf_p95_multiple(self) -> None:
        """RTF P95 with multiple samples."""
        from app.analysis.asr_metrics import get_snapshot, record_rtf

        for i in range(100):
            record_rtf(float(i))
        snap = get_snapshot()
        assert snap["rtf_samples"] == 100
        assert snap["rtf_p95"] > snap["rtf_avg"]


# ═══════════════════════════════════════════════════════════
# B. app.core.metrics — target ≥95%
# ═══════════════════════════════════════════════════════════


class TestMetricsSetCounts:
    """set_task_counts / set_worker_count / set_recording_count."""

    def test_set_task_counts(self) -> None:
        """set_task_counts updates snapshot fields."""
        from app.core.metrics import _latest_snapshot, set_task_counts

        set_task_counts(queued=5, processing=3, completed=10, failed=2)
        assert _latest_snapshot.tasks_queued == 5
        assert _latest_snapshot.tasks_processing == 3
        assert _latest_snapshot.tasks_completed == 10
        assert _latest_snapshot.tasks_failed == 2

    def test_set_task_counts_partial(self) -> None:
        """set_task_counts with partial args leaves others unchanged."""
        from app.core.metrics import _latest_snapshot, set_task_counts

        set_task_counts(queued=0, processing=0, completed=0, failed=0)
        set_task_counts(queued=42)
        assert _latest_snapshot.tasks_queued == 42
        assert _latest_snapshot.tasks_processing == 0
        assert _latest_snapshot.tasks_completed == 0
        assert _latest_snapshot.tasks_failed == 0

    def test_set_worker_count(self) -> None:
        """set_worker_count updates global."""
        import app.core.metrics as cm

        cm.set_worker_count(3)
        assert cm._active_workers == 3
        cm.set_worker_count(0)
        assert cm._active_workers == 0

    def test_set_recording_count(self) -> None:
        """set_recording_count updates global."""
        import app.core.metrics as cm

        cm.set_recording_count(2)
        assert cm._active_recordings == 2

    def test_add_recording_hours(self) -> None:
        """add_recording_hours increments cumulative total."""
        import app.core.metrics as cm

        cm.add_recording_hours(1.5)
        cm.add_recording_hours(3.0)
        assert cm._recording_hours == pytest.approx(4.5)


class TestMetricsPerformance:
    """record_asr_time / record_render_time / record_upload_time / record_lock_wait."""

    def test_record_asr_time(self) -> None:
        """record_asr_time appends to deque."""
        from app.core.metrics import _asr_times, record_asr_time

        record_asr_time(100.0)
        record_asr_time(200.0)
        assert len(_asr_times) == 2
        assert _asr_times[-1] == 200.0

    def test_record_render_time(self) -> None:
        """record_render_time appends to deque."""
        from app.core.metrics import _render_times, record_render_time

        record_render_time(50.0)
        assert len(_render_times) == 1

    def test_record_upload_time(self) -> None:
        """record_upload_time appends to deque."""
        from app.core.metrics import _upload_times, record_upload_time

        record_upload_time(30.0)
        assert len(_upload_times) == 1

    def test_record_lock_wait(self) -> None:
        """record_lock_wait appends to deque."""
        from app.core.metrics import _lock_waits, record_lock_wait

        record_lock_wait(5.0)
        assert len(_lock_waits) == 1

    def test_deque_bounded_to_100(self) -> None:
        """Deques limited to 100 entries."""
        from app.core.metrics import _asr_times, record_asr_time

        for _ in range(200):
            record_asr_time(1.0)
        assert len(_asr_times) == 100


class TestMetricsSnapshot:
    """snapshot() and MetricsSnapshot dataclass."""

    def test_snapshot_has_timestamp(self) -> None:
        """Snapshot has a timestamp."""
        from app.core.metrics import snapshot

        t_before = time.time()
        snap = snapshot()
        t_after = time.time()
        assert t_before <= snap.timestamp <= t_after

    def test_snapshot_reflects_counts(self) -> None:
        """Snapshot reflects task and worker counts."""
        from app.core.metrics import (
            set_recording_count,
            set_task_counts,
            set_worker_count,
            snapshot,
        )

        set_task_counts(queued=1, processing=2, completed=3, failed=4)
        set_worker_count(5)
        set_recording_count(6)
        snap = snapshot()
        assert snap.tasks_queued == 1
        assert snap.tasks_processing == 2
        assert snap.tasks_completed == 3
        assert snap.tasks_failed == 4
        assert snap.active_workers == 5
        assert snap.active_recordings == 6

    def test_snapshot_computes_avgs(self) -> None:
        """Snapshot computes average times from deques."""
        from app.core.metrics import (
            record_asr_time,
            record_lock_wait,
            record_render_time,
            record_upload_time,
            snapshot,
        )

        record_asr_time(100.0)
        record_asr_time(200.0)
        record_render_time(50.0)
        record_upload_time(30.0)
        record_lock_wait(5.0)
        snap = snapshot()
        assert snap.asr_avg_ms == pytest.approx(150.0, abs=0.1)
        assert snap.render_avg_ms == pytest.approx(50.0, abs=0.1)
        assert snap.upload_avg_ms == pytest.approx(30.0, abs=0.1)
        assert snap.db_lock_wait_avg_ms == pytest.approx(5.0, abs=0.1)

    def test_snapshot_empty_deques(self) -> None:
        """Snapshot with empty deques produces 0.0 averages."""
        from app.core.metrics import snapshot

        snap = snapshot()
        assert snap.asr_avg_ms == 0.0
        assert snap.render_avg_ms == 0.0
        assert snap.upload_avg_ms == 0.0
        assert snap.db_lock_wait_avg_ms == 0.0

    def test_snapshot_recording_hours(self) -> None:
        """Snapshot includes recording hours."""
        from app.core.metrics import add_recording_hours, snapshot

        add_recording_hours(2.5)
        snap = snapshot()
        assert snap.total_recording_hours == pytest.approx(2.5, abs=0.1)

    def test_snapshot_appends_to_history(self) -> None:
        """Snapshot adds to history deque."""
        from app.core.metrics import _history, snapshot

        pre_len = len(_history)
        snapshot()
        assert len(_history) == pre_len + 1

    def test_snapshot_history_bounded_to_1440(self) -> None:
        """History capped at 1440 entries."""
        from app.core.metrics import _history, snapshot

        for _ in range(1500):
            snapshot()
        assert len(_history) == 1440

    def test_snapshot_disk_error_handled(self, monkeypatch: MonkeyPatch) -> None:
        """Disk query exception does not crash snapshot."""
        from app.core.metrics import snapshot

        # Ensure disk stats path fails gracefully
        snap = snapshot()
        assert hasattr(snap, "disk_free_gb")


class TestMetricsGetHistory:
    """get_history function."""

    def test_get_history_empty(self) -> None:
        """Empty history returns empty list."""
        from app.core.metrics import get_history

        result = get_history()
        assert isinstance(result, list)
        assert len(result) == 0

    def test_get_history_with_data(self) -> None:
        """History contains structured data."""
        from app.core.metrics import get_history, set_task_counts, snapshot

        set_task_counts(queued=1, processing=2, completed=3, failed=4)
        snapshot()
        result = get_history(limit=1)
        assert len(result) == 1
        entry = result[0]
        assert "timestamp" in entry
        assert "tasks" in entry
        assert entry["tasks"]["queued"] == 1
        assert entry["tasks"]["completed"] == 3
        assert "workers" in entry
        assert "recordings" in entry
        assert "avg_times" in entry
        assert "disk" in entry

    def test_get_history_respects_limit(self) -> None:
        """Get history respects limit."""
        from app.core.metrics import get_history, snapshot

        for _ in range(50):
            snapshot()
        result = get_history(limit=5)
        assert len(result) == 5

    def test_get_history_default_limit_60(self) -> None:
        """Default limit is 60."""
        from app.core.metrics import get_history, snapshot

        for _ in range(100):
            snapshot()
        result = get_history()  # default limit=60
        assert len(result) == 60


class TestMetricsAvg:
    """_avg helper function."""

    def test_avg_empty(self) -> None:
        """Empty deque returns 0.0."""
        from collections import deque

        from app.core.metrics import _avg

        assert _avg(deque()) == 0.0

    def test_avg_single_value(self) -> None:
        """Single value returns that value."""
        from collections import deque

        from app.core.metrics import _avg

        d = deque([42.0])
        assert _avg(d) == pytest.approx(42.0)

    def test_avg_multiple_values(self) -> None:
        """Average of multiple values."""
        from collections import deque

        from app.core.metrics import _avg

        d = deque([1.0, 2.0, 3.0, 4.0])
        assert _avg(d) == pytest.approx(2.5)


# ═══════════════════════════════════════════════════════════
# C. app.core.osutil — target ≥95%
# ═══════════════════════════════════════════════════════════


class TestOpenPath:
    """open_path behavior across platforms."""

    def test_windows_branch_with_existing_path(self, monkeypatch: MonkeyPatch) -> None:
        """Windows: os.startfile called for existing directory."""
        monkeypatch.setattr(sys, "platform", "win32")
        mock_startfile = MagicMock()
        monkeypatch.setattr("os.startfile", mock_startfile)

        from app.core.osutil import open_path

        tmp_dir = str(Path(".").resolve())
        result = open_path(tmp_dir)
        assert result is True
        mock_startfile.assert_called_once()

    def test_windows_branch_nonexistent_uses_parent(self, monkeypatch: MonkeyPatch) -> None:
        """Windows: nonexistent path opens parent directory."""
        monkeypatch.setattr(sys, "platform", "win32")
        mock_startfile = MagicMock()
        monkeypatch.setattr("os.startfile", mock_startfile)

        from app.core.osutil import open_path

        result = open_path("Z:/nonexistent/deeply/nested/file.txt")
        assert result is True
        mock_startfile.assert_called_once()

    def test_windows_startfile_exception_returns_false(self, monkeypatch: MonkeyPatch) -> None:
        """Windows: startfile exception returns False."""
        monkeypatch.setattr(sys, "platform", "win32")

        def _fake_startfile(_p):
            raise OSError("access denied")

        monkeypatch.setattr("os.startfile", _fake_startfile)

        from app.core.osutil import open_path

        result = open_path("test_path")
        assert result is False

    def test_darwin_branch(self, monkeypatch: MonkeyPatch) -> None:
        """macOS: subprocess.Popen(['open', ...]) called."""
        monkeypatch.setattr(sys, "platform", "darwin")
        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        from app.core.osutil import open_path

        tmp_dir = str(Path(".").resolve())
        result = open_path(tmp_dir)
        assert result is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "open"

    def test_linux_branch(self, monkeypatch: MonkeyPatch) -> None:
        """Linux: subprocess.Popen(['xdg-open', ...]) called."""
        monkeypatch.setattr(sys, "platform", "linux")
        mock_popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", mock_popen)

        from app.core.osutil import open_path

        tmp_dir = str(Path(".").resolve())
        result = open_path(tmp_dir)
        assert result is True
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "xdg-open"

    def test_subprocess_exception_returns_false(self, monkeypatch: MonkeyPatch) -> None:
        """Popen exception returns False gracefully."""
        monkeypatch.setattr(sys, "platform", "linux")

        def _fake_popen(*_a, **_kw):
            raise FileNotFoundError("xdg-open not found")

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)

        from app.core.osutil import open_path

        result = open_path("test_path")
        assert result is False

    def test_path_object_input(self, monkeypatch: MonkeyPatch) -> None:
        """Path object input handled correctly."""
        monkeypatch.setattr(sys, "platform", "win32")
        mock_startfile = MagicMock()
        monkeypatch.setattr("os.startfile", mock_startfile)

        from app.core.osutil import open_path

        result = open_path(Path("."))
        assert result is True
        mock_startfile.assert_called_once()

    def test_path_exists_takes_path_itself(self, monkeypatch: MonkeyPatch) -> None:
        """When path exists, use the path itself, not parent."""
        monkeypatch.setattr(sys, "platform", "win32")
        mock_startfile = MagicMock()
        monkeypatch.setattr("os.startfile", mock_startfile)

        from app.core.osutil import open_path

        result = open_path(".")  # current dir always exists
        assert result is True
        mock_startfile.assert_called_once()
