"""多直播间流水线来源隔离回归测试。"""

from __future__ import annotations

import pytest


def test_create_task_rejects_missing_or_cross_session_segment(temp_db: None) -> None:
    """任务只能绑定已存在且属于同会话的原始分段。"""
    from app.db.models import RawSegment
    from app.db.session import get_session
    from app.pipeline.task_worker import create_task

    with get_session() as db:
        segment = RawSegment(session_id=11, seq=1, file_path="room-a.ts")
        db.add(segment)
        db.flush()
        segment_id = segment.id

    assert segment_id is not None
    with pytest.raises(ValueError, match="任务来源不一致"):
        create_task(segment_id, 22)
    with pytest.raises(ValueError, match="不存在的片段"):
        create_task(999999, 11)

    task = create_task(segment_id, 11)
    assert task is not None
    assert task.session_id == 11
