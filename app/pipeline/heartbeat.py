"""任务心跳线程 — 周期更新 heartbeat_at, 检测租约丢失。"""

from __future__ import annotations

import threading

from sqlalchemy import text as sa_text

from app.db.models import SegmentTask
from app.db.session import get_session
from app.pipeline.lifecycle import _WORKER_ID, _shutting_down, now_utc
from app.pipeline.stage_result import mark_heartbeat

_HEARTBEAT_POLL_S: int = 5


def start_heartbeat_thread(
    task_id: int,
    lease_token: str | None = None,
    expected_stage: str | None = None,
) -> threading.Event:
    """启动后台心跳线程, 使用租约条件更新 heartbeat_at。

    周期性地以条件 UPDATE 更新 task.heartbeat_at。
    如果 rowcount==0, 说明租约已被接管, 立即退出。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌 (UUID hex)。
    :param expected_stage: 期望的阶段状态 (如 TRANSCRIBING)。
    :returns: stop Event, 调用 .set() 停止心跳。
    """
    import logging

    _logger = logging.getLogger(__name__)
    stop = threading.Event()

    def _beat() -> None:
        while not stop.is_set() and not _shutting_down:
            try:
                with get_session() as db:
                    if lease_token and expected_stage:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token
                                     AND stage = :expected_stage"""
                            ),
                            params={
                                "now": now_utc().isoformat(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                                "expected_stage": expected_stage,
                            },
                        )
                        if result.rowcount == 0:
                            _logger.warning("lease_lost: task=%s 心跳更新失败, 租约已被接管", task_id)
                            break
                    elif lease_token:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token"""
                            ),
                            params={
                                "now": now_utc().isoformat(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                            },
                        )
                        if result.rowcount == 0:
                            _logger.warning("lease_lost: task=%s 心跳更新失败, 租约已被接管", task_id)
                            break
                    else:
                        t = db.get(SegmentTask, task_id)
                        if t is not None:
                            mark_heartbeat(t)
                            db.add(t)
            except Exception:
                pass
            stop.wait(_HEARTBEAT_POLL_S)

    t = threading.Thread(target=_beat, daemon=True, name=f"hb-{task_id}")
    t.start()
    return stop


def clear_heartbeat_if_own(task_id: int, lease_token: str | None = None) -> None:
    """条件清除 heartbeat, 必须携带租约。

    在任务完成后将 heartbeat_at 设为 NULL, 防止被 stale recovery 误判。
    使用租约条件 UPDATE, 租约已转移时跳过。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌。
    """
    import logging

    _logger = logging.getLogger(__name__)
    try:
        with get_session() as db:
            if lease_token:
                result = db.exec(
                    sa_text(
                        """UPDATE segment_tasks
                           SET heartbeat_at = NULL
                           WHERE id = :task_id
                             AND claimed_by = :worker_id
                             AND lease_token = :lease_token"""
                    ),
                    params={
                        "task_id": task_id,
                        "worker_id": _WORKER_ID,
                        "lease_token": lease_token,
                    },
                )
                if result.rowcount == 0:
                    _logger.debug("clear_heartbeat_if_own: task=%s 租约已转移, 跳过清除", task_id)
                    return
                _logger.debug("clear_heartbeat_if_own: task=%s heartbeat 已清除", task_id)
            else:
                t = db.get(SegmentTask, task_id)
                if t is not None and t.stage not in ("COMPLETED", "FAILED"):
                    t.heartbeat_at = None
                    db.add(t)
    except Exception:
        pass
