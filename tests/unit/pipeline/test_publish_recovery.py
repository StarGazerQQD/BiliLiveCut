"""publish_recovery 模块和 journal 回填的单元测试。"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlmodel import select

from app.db.models import FinalClip, SegmentTask, TaskStatus, UploadAttempt, UploadStatus, UploadTask
from app.db.session import get_session
from app.pipeline.publish_recovery import recover_publish_results
from app.pipeline.stale_recovery import recover_stale_upload_attempts, sync_segment_task_from_attempt
from app.publishing.journal import mark_replayed, read_pending_entries, write_remote_success
from app.publishing.uploader import classify_upload_error


def _make_final_clip(clip_id: int, candidate_id: int = 1, title: str = "t") -> FinalClip:
    """Create a minimal FinalClip for tests."""
    return FinalClip(
        id=clip_id,
        candidate_id=candidate_id,
        file_path=f"/tmp/test_{clip_id}.mp4",
        title=title,
        file_size=1024,
    )


class TestJournalWriteRead:
    """Journal 写入和读取的基础测试。"""

    def test_write_then_read(self):
        journal_dir = Path(tempfile.mkdtemp())
        with patch("app.publishing.journal._JOURNAL_DIR", journal_dir):
            assert write_remote_success("test-token-abc", 3, 1, 10, "BV_test123")

            entries = read_pending_entries()
            found = [e for e in entries if e["attempt_token"] == "test-token-abc"]
            assert len(found) == 1
            assert found[0]["remote_id"] == "BV_test123"
            assert found[0]["publish_generation"] == 3

    def test_mark_replayed_removes_entry(self):
        journal_dir = Path(tempfile.mkdtemp())
        with patch("app.publishing.journal._JOURNAL_DIR", journal_dir):
            write_remote_success("replay-token", 1, 2, 20, "BV_replay")
            assert len(read_pending_entries()) >= 1

            mark_replayed("replay-token", 1)
            entries = read_pending_entries()
            found = [e for e in entries if e["attempt_token"] == "replay-token"]
            assert len(found) == 0

    def test_read_pending_handles_corrupt_lines(self):
        journal_dir = Path(tempfile.mkdtemp())
        path = journal_dir / "publish_journal_20200101.jsonl"
        path.write_text('{"invalid json\n', encoding="utf-8")
        with patch("app.publishing.journal._JOURNAL_DIR", journal_dir):
            write_remote_success("good", 1, 1, 1, "BV")
            entries = read_pending_entries()
            assert any(e["attempt_token"] == "good" for e in entries)


class TestPublishRecovery:
    """恢复器从 Journal 回填数据到 DB。"""

    def test_recover_empty_journal(self, temp_db):
        with patch("app.pipeline.publish_recovery.read_pending_entries") as mock_read:
            mock_read.return_value = []
            result = recover_publish_results()
            assert result == 0

    def test_recover_updates_attempt_and_task(self, temp_db):

        # 创建 FinalClip
        with get_session() as db:
            clip = _make_final_clip(100, candidate_id=100)
            db.add(clip)
            db.commit()

        # 创建 UploadTask
        with get_session() as db:
            task = UploadTask(id=200, clip_id=100, status=UploadStatus.UPLOADING, uploader="manual")
            db.add(task)
            db.commit()

        # 创建 UploadAttempt (非 SUCCESS 状态)
        with get_session() as db:
            attempt = UploadAttempt(
                upload_task_id=200,
                publish_generation=5,
                attempt_token="recover-123",
                platform="bilibili",
                clip_id=100,
                status="in_progress",
                started_at=datetime.now(UTC),
            )
            db.add(attempt)
            db.commit()

        entry = {
            "attempt_token": "recover-123",
            "publish_generation": 5,
            "upload_task_id": 200,
            "clip_id": 100,
            "outcome": "success",
            "remote_id": "BV_recovered",
        }
        with patch("app.pipeline.publish_recovery.read_pending_entries") as mock_read:
            mock_read.return_value = [entry]
            with patch("app.pipeline.publish_recovery.mark_replayed"):
                result = recover_publish_results()
                assert result == 1

        with get_session() as db:
            attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == "recover-123")).first()
            assert attempt is not None
            assert attempt.status == UploadStatus.SUCCESS
            assert attempt.remote_id == "BV_recovered"

            ut = db.get(UploadTask, 200)
            assert ut is not None
            assert ut.status == UploadStatus.SUCCESS

    def test_skips_already_success(self, temp_db):

        with get_session() as db:
            clip = _make_final_clip(101, candidate_id=101)
            db.add(clip)
            db.flush()  # ensure FinalClip exists before UploadTask
            task = UploadTask(id=201, clip_id=101, status=UploadStatus.SUCCESS, uploader="manual")
            db.add(task)
            db.flush()  # ensure UploadTask exists before UploadAttempt
            attempt = UploadAttempt(
                upload_task_id=201,
                publish_generation=2,
                attempt_token="already-ok",
                platform="bilibili",
                clip_id=101,
                status="success",
                remote_id="BV_existing",
            )
            db.add(attempt)
            db.commit()

        entry = {"attempt_token": "already-ok", "publish_generation": 2, "remote_id": "BV_journal"}
        with patch("app.pipeline.publish_recovery.read_pending_entries") as mock_read:
            mock_read.return_value = [entry]
            with patch("app.pipeline.publish_recovery.mark_replayed") as mock_mark:
                result = recover_publish_results()
                assert result == 0
                mock_mark.assert_called()


class TestStaleUploadAttemptRecovery:
    """stale_recovery 中 UploadAttempt 超时恢复测试。"""

    def test_in_progress_too_old_becomes_reconciliation(self, temp_db):

        # 创建关联实体
        stale_start = datetime.now(UTC) - timedelta(seconds=9999)
        with get_session() as db:
            clip = _make_final_clip(200, candidate_id=200)
            db.add(clip)
            db.commit()

        with get_session() as db:
            task = UploadTask(id=300, clip_id=200, uploader="manual", status=UploadStatus.UPLOADING)
            db.add(task)
            db.flush()  # ensure UploadTask exists before UploadAttempt
            attempt = UploadAttempt(
                upload_task_id=300,
                publish_generation=1,
                attempt_token="stale-test",
                platform="bilibili",
                clip_id=200,
                status="in_progress",
                started_at=stale_start,
            )
            db.add(attempt)
            db.commit()

        recovered = recover_stale_upload_attempts()
        assert recovered == 1

        with get_session() as db:
            attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == "stale-test")).first()
            assert attempt is not None
            assert attempt.status == UploadStatus.RECONCILIATION_REQUIRED


class TestSyncSegmentTaskFromAttempt:
    """根据 UploadAttempt 状态同步 SegmentTask。"""

    def test_success_attempt_syncs_task(self, temp_db):

        with get_session() as db:
            clip = _make_final_clip(500, candidate_id=500)
            db.add(clip)
            db.flush()  # ensure FinalClip exists
            task = SegmentTask(
                id=1000,
                segment_id=1,
                session_id=0,
                stage=TaskStatus.PUBLISHING,
                clip_id=500,
                worker_id="w1",
                claimed_by="w1",
                claimed_at=datetime.now(UTC),
            )
            db.add(task)
            ut = UploadTask(id=400, clip_id=500, uploader="manual", status=UploadStatus.SUCCESS)
            db.add(ut)
            db.flush()  # ensure UploadTask exists before UploadAttempt
            attempt = UploadAttempt(
                upload_task_id=400,
                publish_generation=1,
                attempt_token="sync-test",
                platform="bilibili",
                clip_id=500,
                status="success",
                remote_id="BV_synced",
            )
            db.add(attempt)
            db.commit()

        synced = sync_segment_task_from_attempt()
        assert synced == 1

        with get_session() as db:
            task = db.get(SegmentTask, 1000)
            assert task is not None
            assert task.stage == TaskStatus.COMPLETED


class TestClassifyUploadError:
    """classify_upload_error 异常分类测试。"""

    def test_dns_retryable(self):
        result = classify_upload_error(OSError("DNS lookup failed"))
        assert result.outcome == "failed_retryable"
        assert not result.request_may_have_been_sent

    def test_connection_refused_retryable(self):
        result = classify_upload_error(OSError("Connection refused errno 111"))
        assert result.outcome == "failed_retryable"
        assert not result.request_may_have_been_sent

    def test_timeout_unknown(self):
        result = classify_upload_error(TimeoutError("timed out after 30s"))
        assert result.outcome == "remote_result_unknown"
        assert result.request_may_have_been_sent

    def test_broken_pipe_unknown(self):
        result = classify_upload_error(OSError("broken pipe on write"))
        assert result.outcome == "remote_result_unknown"
        assert result.request_may_have_been_sent

    def test_permission_permanent(self):
        result = classify_upload_error(PermissionError("unauthorized access"))
        assert result.outcome == "failed_permanent"

    def test_unclassified_conservative(self):
        result = classify_upload_error(ValueError("unknown error"))
        assert result.outcome == "remote_result_unknown"
        assert result.request_may_have_been_sent
