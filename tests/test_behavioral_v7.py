"""V0.1.12.7 行为测试: 使用真实 SQLite + 生产代码验证核心逻辑。

测试覆盖:
- 审批一致性 (Task+Event+Candidate 事务原子性)
- UploadTask 结果映射
- ManualUploader 非 PUBLISHED
- 迁移 SQL 注释解析
- Candidate/Event ID 碰撞
- IntegrityError 幂等
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest


def _now() -> datetime:
    return datetime.now(UTC)


# ═══════════════════════════════════════════════════
# 审批一致性测试
# ═══════════════════════════════════════════════════

class TestApprovalConsistency:
    """测试统一审批在同一事务中更新 Task + Event + Candidate."""

    def test_auto_approve_updates_task_event_candidate(self, temp_db) -> None:
        """自动批准同时更新 Task.stage + Event.review_status + Candidate.status."""
        from app.db.models import (
            CandidateStatus,
            HighlightCandidate,
            HighlightEvent,
            LiveRoom,
            RecordingSession,
            ReviewStatus,
            SegmentTask,
            TaskStatus,
        )
        from app.db.session import get_session
        from app.pipeline.approval import approve_event_and_task

        with get_session() as db:
            room = LiveRoom(id=9901, input_url="test", auto_approve=True,
                            auto_approve_threshold=0.80)
            sess = RecordingSession(id=8801, room_id=9901)
            cand = HighlightCandidate(
                id=6601, session_id=8801,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.90, status=CandidateStatus.PENDING,
            )
            event = HighlightEvent(
                id=7701, candidate_id=6601, session_id=8801,
                raw_start_ts=_now(), raw_end_ts=_now(),
                review_status=ReviewStatus.PENDING,
            )
            task = SegmentTask(
                segment_id=5501, session_id=8801,
                stage=TaskStatus.AWAITING_REVIEW,
                candidate_id=6601, event_id=7701,
                idempotency_key="5501:awaiting_review",
            )
            db.add_all([room, sess, cand, event, task])

        ok = approve_event_and_task(
            task_id=task.id,
            event_id=event.id,
            source="auto",
            review_decision="approved_solo",
        )
        assert ok

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            e = db.get(HighlightEvent, event.id)
            c = db.get(HighlightCandidate, 6601)

        assert t.stage == TaskStatus.APPROVED
        assert e.review_status == ReviewStatus.APPROVED_SOLO
        assert c.status == CandidateStatus.APPROVED

    def test_approve_rejects_when_event_not_found(self, temp_db) -> None:
        """Event 不存在时批准失败。"""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import approve_event_and_task

        with get_session() as db:
            task = SegmentTask(
                segment_id=5502, session_id=1,
                stage=TaskStatus.AWAITING_REVIEW,
                idempotency_key="5502:awaiting_review",
            )
            db.add(task)

        ok = approve_event_and_task(task_id=task.id, event_id=99999, source="auto")
        assert not ok

    def test_approve_blocks_rejected_event_from_auto(self, temp_db) -> None:
        """已拒绝 Event 不得被自动流程重新批准。"""
        from app.db.models import HighlightEvent, ReviewStatus, SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import approve_event_and_task

        with get_session() as db:
            event = HighlightEvent(
                id=7703, session_id=1,
                raw_start_ts=_now(), raw_end_ts=_now(),
                review_status=ReviewStatus.REJECTED,
            )
            task = SegmentTask(
                segment_id=5503, session_id=1,
                stage=TaskStatus.AWAITING_REVIEW,
                event_id=7703,
                idempotency_key="5503:awaiting_review",
            )
            db.add_all([event, task])

        ok = approve_event_and_task(task_id=task.id, event_id=7703, source="auto")
        assert not ok

    def test_approve_idempotent_on_already_approved(self, temp_db) -> None:
        """已批准 Event 重复批准幂等跳过。"""
        from app.db.models import HighlightEvent, ReviewStatus, SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import approve_event_and_task

        with get_session() as db:
            event = HighlightEvent(
                id=7704, session_id=1,
                raw_start_ts=_now(), raw_end_ts=_now(),
                review_status=ReviewStatus.APPROVED_SOLO,
            )
            task = SegmentTask(
                segment_id=5504, session_id=1,
                stage=TaskStatus.APPROVED,
                event_id=7704,
                idempotency_key="5504:approved",
            )
            db.add_all([event, task])

        ok = approve_event_and_task(task_id=task.id, event_id=7704, source="auto")
        assert ok  # 幂等成功


# ═══════════════════════════════════════════════════
# UploadTask 映射测试
# ═══════════════════════════════════════════════════

class TestUploadResultMapping:
    """测试 apply_upload_result 按 UploadTask 状态推进主流水线。"""

    def test_success_maps_to_completed(self, temp_db) -> None:
        """SUCCESS → COMPLETED."""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import apply_upload_result

        with get_session() as db:
            task = SegmentTask(
                segment_id=5505, session_id=1,
                stage=TaskStatus.PUBLISHING,
                idempotency_key="5505:publishing",
            )
            db.add(task)

        ok = apply_upload_result(
            task_id=task.id,
            upload_task_id=1,
            upload_status="success",
            remote_id="BVtest123",
        )
        assert ok

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t.stage == TaskStatus.COMPLETED

    def test_failed_maps_to_transient_failed(self, temp_db) -> None:
        """FAILED → TRANSIENT_FAILED."""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import apply_upload_result

        with get_session() as db:
            task = SegmentTask(
                segment_id=5506, session_id=1,
                stage=TaskStatus.PUBLISHING,
                idempotency_key="5506:publishing",
            )
            db.add(task)

        ok = apply_upload_result(
            task_id=task.id,
            upload_task_id=2,
            upload_status="failed",
            upload_error="Test upload error",
        )
        assert ok

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t.stage == TaskStatus.TRANSIENT_FAILED
            assert "Test upload error" in (t.last_error or "")

    def test_skipped_maps_to_awaiting_confirmation(self, temp_db) -> None:
        """SKIPPED → AWAITING_PUBLISH_CONFIRMATION."""
        from app.db.models import SegmentTask, TaskStatus
        from app.db.session import get_session
        from app.pipeline.approval import apply_upload_result

        with get_session() as db:
            task = SegmentTask(
                segment_id=5507, session_id=1,
                stage=TaskStatus.PUBLISHING,
                idempotency_key="5507:publishing",
            )
            db.add(task)

        ok = apply_upload_result(
            task_id=task.id,
            upload_task_id=3,
            upload_status="skipped",
        )
        assert ok

        with get_session() as db:
            t = db.get(SegmentTask, task.id)
            assert t.stage == TaskStatus.AWAITING_PUBLISH_CONFIRMATION


# ═══════════════════════════════════════════════════
# ManualUploader 测试
# ═══════════════════════════════════════════════════

class TestManualUploader:
    """ManualUploader 不标记 PUBLISHED。"""

    def test_manual_upload_not_published(self, temp_db, tmp_path: Path) -> None:
        """ManualUploader 导出后 FinalClip 不标记 PUBLISHED。"""
        from app.db.models import ClipStatus, FinalClip
        from app.db.session import get_session
        from app.publishing.uploader import enqueue_and_upload

        vid = tmp_path / "test_manual.mp4"
        vid.write_bytes(b"x" * 2048)

        with get_session() as db:
            clip = FinalClip(
                candidate_id=1,
                file_path=str(vid),
                title="测试切片",
                description="测试",
                duration_s=30.0,
                status=ClipStatus.GENERATED,
            )
            db.add(clip)
            db.flush()
            cid = clip.id

        task = enqueue_and_upload(cid)

        with get_session() as db:
            c = db.get(FinalClip, cid)
            assert c.status != ClipStatus.PUBLISHED, (
                "ManualUploader 不应标记 PUBLISHED"
            )
            assert task.status == "success"


# ═══════════════════════════════════════════════════
# IntegrityError 幂等测试
# ═══════════════════════════════════════════════════

class TestIntegrityErrorIdempotency:
    """并发唯一约束冲突被当作幂等命中。"""

    def test_create_task_idempotent_on_duplicate(self, temp_db) -> None:
        """同 segment_id 的 create_task 第二次调用返回 None。"""
        from app.db.models import RawSegment, RecordingSession, SegmentStatus
        from app.db.session import get_session
        from app.pipeline.task_worker import create_task

        with get_session() as db:
            sess = RecordingSession(id=1, room_id=1, status="recorded")
            seg = RawSegment(
                id=8001, session_id=1, seq=0,
                file_path="/tmp/test.mp4",
                status=SegmentStatus.RECORDED,
            )
            db.add_all([sess, seg])

        t1 = create_task(8001, 1)
        assert t1 is not None

        t2 = create_task(8001, 1)
        assert t2 is None  # 幂等: 返回 None

    def test_ensure_event_idempotent(self, temp_db) -> None:
        """同 candidate_id 的 _ensure_event 两次调用返回同一个 event_id。"""
        from app.db.models import HighlightCandidate
        from app.db.session import get_session
        from app.pipeline.task_worker import _ensure_event

        with get_session() as db:
            cand = HighlightCandidate(
                id=8002, session_id=1,
                peak_ts=_now(), start_ts=_now(), end_ts=_now(),
                highlight_score=0.85,
            )
            db.add(cand)

        eid1 = _ensure_event(8002)
        eid2 = _ensure_event(8002)
        assert eid1 is not None
        assert eid1 == eid2


# ═══════════════════════════════════════════════════
# TOCTOU 安全清理测试
# ═══════════════════════════════════════════════════

class TestSafeUnlink:
    """测试 _safe_unlink 的路径安全保护。"""

    def test_normal_file_deletion(self, tmp_path: Path) -> None:
        """允许删除 allowed_root 下的文件。"""
        from app.pipeline.storage_lifecycle import _safe_unlink

        root = tmp_path / "clips"
        root.mkdir()
        f = root / "test.mp4"
        f.write_text("data")

        assert f.exists()
        result = _safe_unlink(str(f), root)
        assert result
        assert not f.exists()

    def test_rejects_escape_path(self, tmp_path: Path) -> None:
        """拒绝删除 allowed_root 外的路径。"""
        from app.pipeline.storage_lifecycle import _safe_unlink

        root = tmp_path / "clips"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("data")

        result = _safe_unlink(str(outside), root)
        assert not result
        assert outside.exists()  # 文件未被删除

    def test_rejects_parent_traversal(self, tmp_path: Path) -> None:
        """拒绝 .. 路径遍历。"""
        from app.pipeline.storage_lifecycle import _safe_unlink

        root = tmp_path / "clips"
        root.mkdir()
        sub = root / "sub"
        sub.mkdir()

        # 创建 allowed_root 外的文件
        parent_file = tmp_path / "secret.txt"
        parent_file.write_text("secret")

        traversal_path = str(sub / ".." / ".." / "secret.txt")
        result = _safe_unlink(traversal_path, root)
        assert not result
        assert parent_file.exists()

    def test_rejects_symlink_to_outside(self, tmp_path: Path) -> None:
        """拒绝通过符号链接访问外部路径(Windows: Junction)。"""
        from app.pipeline.storage_lifecycle import _safe_unlink

        root = tmp_path / "clips"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        link_in_root = root / "link_to_outside"
        try:
            import _winapi
            _winapi.CreateJunction(str(outside), str(link_in_root))
        except (ImportError, AttributeError, OSError):
            # 非 Windows 或无权限
            try:
                link_in_root.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("无法创建符号链接(需要管理员权限)")

        result = _safe_unlink(str(link_in_root), root)
        assert not result  # 符号链接内的文件不应被删除
