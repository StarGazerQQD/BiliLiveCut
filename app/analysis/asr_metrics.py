"""ASR 可观测性指标收集 (V0.1.12.2)。

收集各后端的调用、耗时、复核、fallback 和资源指标,
供运维页面和日志使用。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class BackendStats:
    """单个后端的统计。"""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    total_duration: float = 0.0
    p50_duration: float = 0.0
    p95_duration: float = 0.0
    _durations: list[float] = field(default_factory=list)

    @property
    def avg_duration(self) -> float:
        return self.total_duration / self.calls if self.calls > 0 else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls > 0 else 0.0

    def record(self, duration: float, success: bool = True) -> None:
        self.calls += 1
        if success:
            self.successes += 1
        else:
            self.failures += 1
        self.total_duration += duration
        self._durations.append(duration)
        if len(self._durations) > 1000:
            self._durations = self._durations[-500:]

    def recompute_percentiles(self) -> None:
        if not self._durations:
            return
        sorted_d = sorted(self._durations)
        n = len(sorted_d)
        self.p50_duration = sorted_d[n // 2]
        self.p95_duration = sorted_d[int(n * 0.95)]


@dataclass
class ReviewStats:
    """复核统计。"""

    triggered: int = 0
    succeeded: int = 0
    failed: int = 0
    text_modified: int = 0       # final != base
    kept_base: int = 0           # 保留基础文本
    adopted_review: int = 0      # 采用复核文本
    manual_needed: int = 0       # 标记人工确认

    @property
    def trigger_rate(self) -> float:
        from .asr_metrics import _backend_stats
        total = _backend_stats.get("paraformer", BackendStats()).calls
        return self.triggered / total if total > 0 else 0.0

    @property
    def adoption_rate(self) -> float:
        return self.adopted_review / self.triggered if self.triggered > 0 else 0.0


_backend_stats: dict[str, BackendStats] = defaultdict(BackendStats)
_review_stats = ReviewStats()
_oom_count: int = 0
_rtf_samples: list[float] = []
_lock = threading.Lock()


def record_backend_call(backend: str, duration: float, success: bool = True) -> None:
    """记录一次后端调用。

    :param backend: 后端名 (paraformer/whisper/funasr-nano/sensevoice)。
    :param duration: 耗时秒。
    :param success: 是否成功。
    """
    with _lock:
        _backend_stats[backend].record(duration, success)
        _backend_stats[backend].recompute_percentiles()


def record_review(adopted: bool, kept_base: bool, manual_needed: bool) -> None:
    """记录一次复核结果。"""
    with _lock:
        _review_stats.triggered += 1
        if adopted:
            _review_stats.adopted_review += 1
        if kept_base:
            _review_stats.kept_base += 1
        if manual_needed:
            _review_stats.manual_needed += 1


def record_review_success() -> None:
    with _lock:
        _review_stats.succeeded += 1


def record_review_failure() -> None:
    with _lock:
        _review_stats.failed += 1


def record_fallback() -> None:
    """记录一次 Whisper fallback。"""


def record_oom() -> None:
    """记录一次 OOM。"""
    global _oom_count
    with _lock:
        _oom_count += 1


def record_rtf(rtf: float) -> None:
    """记录一次 RTF。"""
    with _lock:
        _rtf_samples.append(rtf)
        if len(_rtf_samples) > 1000:
            _rtf_samples[:] = _rtf_samples[-500:]


def get_snapshot() -> dict:
    """获取当前指标快照 (供运维页面)。"""
    with _lock:
        backends = {}
        for name, stats in _backend_stats.items():
            backends[name] = {
                "calls": stats.calls,
                "successes": stats.successes,
                "failures": stats.failures,
                "failure_rate": round(stats.failure_rate, 4),
                "avg_duration": round(stats.avg_duration, 3),
                "p50_duration": round(stats.p50_duration, 3),
                "p95_duration": round(stats.p95_duration, 3),
            }
        rtf_avg = sum(_rtf_samples) / len(_rtf_samples) if _rtf_samples else 0.0
        rtf_sorted = sorted(_rtf_samples) if _rtf_samples else [0.0]
        rtp_p95 = rtf_sorted[int(len(rtf_sorted) * 0.95)] if len(rtf_sorted) > 1 else rtf_avg

        return {
            "backends": backends,
            "review": {
                "triggered": _review_stats.triggered,
                "succeeded": _review_stats.succeeded,
                "failed": _review_stats.failed,
                "text_modified": _review_stats.text_modified,
                "kept_base": _review_stats.kept_base,
                "adopted_review": _review_stats.adopted_review,
                "manual_needed": _review_stats.manual_needed,
                "trigger_rate": round(_review_stats.trigger_rate, 4),
                "adoption_rate": round(_review_stats.adoption_rate, 4),
            },
            "oom_count": _oom_count,
            "rtf_avg": round(rtf_avg, 4),
            "rtf_p95": round(rtp_p95, 4),
            "rtf_samples": len(_rtf_samples),
        }
