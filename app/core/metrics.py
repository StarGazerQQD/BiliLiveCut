"""核心运行指标收集。

轻量级指标收集, 供监控页面和告警使用。
不依赖 Prometheus, 全部内存聚合 + 定期清理。

追踪指标:
- 任务计数: queued/processing/completed/failed 各阶段任务数
- Worker 状态: 活跃 worker 数, 心跳时间
- 录制状态: 活跃录制数, 录制时长
- 磁盘使用: 原始/切片/上传目录大小
- 性能: ASR 平均耗时, 渲染平均耗时, 上传平均耗时
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class MetricsSnapshot:
    """指标快照。

    :param timestamp: Unix 时间戳。
    :param tasks_queued: 排队任务数。
    :param tasks_processing: 处理中任务数。
    :param tasks_completed: 已完成任务数。
    :param tasks_failed: 失败任务数。
    :param active_workers: 活跃 Worker 数。
    :param active_recordings: 活跃录制数。
    :param total_recording_hours: 累计录制时长 (小时)。
    :param asr_avg_ms: ASR 平均耗时 (毫秒)。
    :param render_avg_ms: 渲染平均耗时 (毫秒)。
    :param upload_avg_ms: 上传平均耗时 (毫秒)。
    :param disk_raw_gb: 原始文件大小 (GB)。
    :param disk_clips_gb: 切片文件大小 (GB)。
    :param disk_free_gb: 剩余磁盘空间 (GB)。
    :param db_lock_wait_avg_ms: 数据库锁平均等待时间 (毫秒)。
    """

    timestamp: float = field(default_factory=time.time)
    tasks_queued: int = 0
    tasks_processing: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    active_workers: int = 0
    active_recordings: int = 0
    total_recording_hours: float = 0.0
    asr_avg_ms: float = 0.0
    render_avg_ms: float = 0.0
    upload_avg_ms: float = 0.0
    disk_raw_gb: float = 0.0
    disk_clips_gb: float = 0.0
    disk_free_gb: float = 0.0
    db_lock_wait_avg_ms: float = 0.0


# 全局指标
_latest_snapshot = MetricsSnapshot()
_history: deque[MetricsSnapshot] = deque(maxlen=1440)  # 保留最近 24h (每分钟一条)

# 性能 trackers
_asr_times: deque[float] = deque(maxlen=100)
_render_times: deque[float] = deque(maxlen=100)
_upload_times: deque[float] = deque(maxlen=100)
_lock_waits: deque[float] = deque(maxlen=100)
_active_workers: int = 0
_active_recordings: int = 0
_recording_hours: float = 0.0


def set_task_counts(
    queued: int = 0,
    processing: int = 0,
    completed: int = 0,
    failed: int = 0,
) -> None:
    """更新任务计数。

    :param queued: 排队数。
    :param processing: 处理中数。
    :param completed: 已完成数。
    :param failed: 失败数。
    """
    _latest_snapshot.tasks_queued = queued
    _latest_snapshot.tasks_processing = processing
    _latest_snapshot.tasks_completed = completed
    _latest_snapshot.tasks_failed = failed


def record_asr_time(ms: float) -> None:
    """记录 ASR 耗时。

    :param ms: 耗时 (毫秒)。
    """
    _asr_times.append(ms)


def record_render_time(ms: float) -> None:
    """记录渲染耗时。

    :param ms: 耗时 (毫秒)。
    """
    _render_times.append(ms)


def record_upload_time(ms: float) -> None:
    """记录上传耗时。

    :param ms: 耗时 (毫秒)。
    """
    _upload_times.append(ms)


def record_lock_wait(ms: float) -> None:
    """记录数据库锁等待时间。

    :param ms: 等待时间 (毫秒)。
    """
    _lock_waits.append(ms)


def set_worker_count(count: int) -> None:
    """设置活跃 Worker 数。

    :param count: Worker 数量。
    """
    global _active_workers
    _active_workers = count


def set_recording_count(count: int) -> None:
    """设置活跃录制数。

    :param count: 录制数量。
    """
    global _active_recordings
    _active_recordings = count


def add_recording_hours(hours: float) -> None:
    """累加录制时长。

    :param hours: 时长 (小时)。
    """
    global _recording_hours
    _recording_hours += hours


def _avg(values: deque[float]) -> float:
    """计算 deque 平均值。

    :param values: 数值队列。
    :returns: 平均值。
    """
    if not values:
        return 0.0
    return sum(values) / len(values)


def snapshot() -> MetricsSnapshot:
    """生成当前指标快照。

    :returns: :class:`MetricsSnapshot`。
    """
    snap = MetricsSnapshot(timestamp=time.time())
    snap.tasks_queued = _latest_snapshot.tasks_queued
    snap.tasks_processing = _latest_snapshot.tasks_processing
    snap.tasks_completed = _latest_snapshot.tasks_completed
    snap.tasks_failed = _latest_snapshot.tasks_failed
    snap.active_workers = _active_workers
    snap.active_recordings = _active_recordings
    snap.total_recording_hours = round(_recording_hours, 1)
    snap.asr_avg_ms = round(_avg(_asr_times), 1)
    snap.render_avg_ms = round(_avg(_render_times), 1)
    snap.upload_avg_ms = round(_avg(_upload_times), 1)
    snap.db_lock_wait_avg_ms = round(_avg(_lock_waits), 1)

    # 磁盘信息
    try:
        from app.pipeline.storage_lifecycle import get_directory_size, get_disk_usage

        disk = get_disk_usage()
        snap.disk_free_gb = disk.get("free_gb", 0.0)
        from app.core.paths import clips_dir, raw_dir

        snap.disk_clips_gb = get_directory_size(clips_dir())
        snap.disk_raw_gb = get_directory_size(raw_dir())
    except Exception:
        pass

    # 入历史
    _history.append(snap)

    return snap


def start_metrics_collector(interval_s: float = 60.0) -> None:
    """启动后台指标采集线程,周期性调用 ``snapshot()`` 填充历史记录。

    在应用启动时调用一次即可。守护线程随进程退出自动终止。

    :param interval_s: 采集间隔 (秒),默认 60 秒。
    """
    import threading

    def _collect_loop() -> None:
        while True:
            try:
                snapshot()
            except Exception:
                pass
            time.sleep(interval_s)

    t = threading.Thread(target=_collect_loop, daemon=True, name="metrics-collector")
    t.start()
    logger.info("metrics collector started, interval={}s", interval_s)


def get_history(limit: int = 60) -> list[dict]:
    """获取历史指标列表。

    :param limit: 最多返回条数。
    :returns: 指标字典列表。
    """
    items = list(_history)[-limit:]
    return [
        {
            "timestamp": s.timestamp,
            "tasks": {
                "queued": s.tasks_queued,
                "processing": s.tasks_processing,
                "completed": s.tasks_completed,
                "failed": s.tasks_failed,
            },
            "workers": s.active_workers,
            "recordings": s.active_recordings,
            "recording_hours": s.total_recording_hours,
            "avg_times": {
                "asr_ms": s.asr_avg_ms,
                "render_ms": s.render_avg_ms,
                "upload_ms": s.upload_avg_ms,
            },
            "disk": {
                "free_gb": s.disk_free_gb,
                "raw_gb": s.disk_raw_gb,
                "clips_gb": s.disk_clips_gb,
            },
        }
        for s in items
    ]
