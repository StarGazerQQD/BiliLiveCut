from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlmodel import select

from app.analysis.transcription.isolated import IsolatedPipeline
from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult, EmotionEvent, Word
from app.analysis.transcription.process_pool import ASRProcessExitError, ASRProcessPool
from app.core.config import settings
from app.db.entities import AppSetting, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.models.configuration import asr_worker_settings
from app.pipeline.asr_checkpoint import ASRCheckpoint
from app.pipeline.claiming import pop_and_claim
from app.pipeline.lease import LeaseLostError, TaskLease
from app.pipeline.stale_recovery import recover_stale


def claim() -> TaskLease:
    task = pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
    assert task is not None and task.claimed_by and task.lease_token
    return TaskLease(task.id, task.claimed_by, task.lease_token, TaskStatus.TRANSCRIBING)


def expire(lease: TaskLease) -> TaskLease:
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task is not None
        task.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
        db.add(task)
    recover_stale()
    return claim()


def no_cancel() -> None:
    pass


def seed() -> TaskLease:
    with get_session() as db:
        db.add(SegmentTask(segment_id=1, session_id=1, stage=TaskStatus.QUEUED_FOR_TRANS))
    return claim()


def test_raw_checkpoint_preserves_fields_and_rejects_old_lease(temp_db: None, pcm_wav: Path) -> None:
    lease = seed()
    first = ASRCheckpoint(lease, str(pcm_wav), asr_worker_settings(), "prompt", None, no_cancel)
    result = ASRTranscriptResult(
        text="原始语音内容",
        backend="fixture",
        model_id="fixture",
        model_revision="r1",
        segments=[ASRSegmentResult(0, 1, "原始语音内容", words=[Word("内容", 0.5, 1)])],
        emotions=[EmotionEvent("music", 0, 1, 0.9)],
        review_triggered=True,
        review_reasons=["low_confidence"],
        fallback_backend="fixture-fallback",
        metadata={"source": "controlled input"},
    )
    first.save_raw(result)
    next_lease = expire(lease)
    resumed = ASRCheckpoint(next_lease, str(pcm_wav), asr_worker_settings(), "prompt", None, no_cancel)
    saved = resumed.raw()
    assert saved is not None and asdict(saved) == asdict(result)
    assert saved.segments[0].raw_confidence is None
    with pytest.raises(LeaseLostError):
        first.save_raw(result)
    with pytest.raises(LeaseLostError):
        first.raw()
    with get_session() as db:
        assert not db.exec(select(Transcript)).all()


@pytest.mark.parametrize("changed", ["prompt", "window", "model", "source", "samples"])
def test_raw_checkpoint_requires_actual_input_identity(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    import wave

    lease = seed()
    payload = asr_worker_settings()
    first = ASRCheckpoint(lease, str(pcm_wav), payload, "before", None, no_cancel)
    first.save_raw(ASRTranscriptResult(text="只属于初始输入的文本"))
    prompt, window = "before", None
    if changed == "prompt":
        prompt = "after"
    elif changed == "window":
        window = (0.0, 8.336)
    elif changed == "model":
        payload = {**payload, "asr_remote_model": "different-model"}
    elif changed == "source":
        monkeypatch.setenv("BLC_MODELS_DIR", str(pcm_wav.parent / "other-models"))
    else:
        with wave.open(str(pcm_wav), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x01\x00" * 133376)
    changed_checkpoint = ASRCheckpoint(lease, str(pcm_wav), payload, prompt, window, no_cancel)
    assert changed_checkpoint.raw() is None


def test_real_child_restart_only_requests_unconfirmed_chunks(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    pools: list[ASRProcessPool] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            calls.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            if len(calls) == 2:
                # 外部进程故障边界：真实退出发生在第二块响应返回之前。
                pools[-1]._slots[0].process.kill()
                return
            payload = json.dumps({"text": f"第{len(calls)}个请求返回了有效的语音文本。", "language": "zh"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_remote_base_url", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setattr(settings, "asr_remote_model", "local-recovery-fixture")
    monkeypatch.setattr(settings, "asr_remote_api_key", "never-persist-this-fixture-key")
    monkeypatch.setattr(settings, "asr_remote_chunk_s", 4)
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    try:
        lease = seed()
        pools.append(ASRProcessPool())
        with pytest.raises(ASRProcessExitError):
            IsolatedPipeline(lease=lease, pool=pools[-1]).transcribe(str(pcm_wav))
        pools[-1].shutdown(0)
        assert len(calls) == 2
        with get_session() as db:
            rows = db.exec(select(AppSetting).where(AppSetting.key.startswith("asr_checkpoint:"))).all()
            assert len(rows) == 1 and rows[0].key.endswith(":chunk:1")
            assert "never-persist-this-fixture-key" not in rows[0].value
        next_lease = expire(lease)
        pools.append(ASRProcessPool())
        pipeline = IsolatedPipeline(lease=next_lease, pool=pools[-1])
        result = pipeline.transcribe(str(pcm_wav))
        assert len(calls) == 4
        assert [part.start for part in result.segments] == [0, 4, 8]
        assert "第1个请求" in result.text and "第2个请求" not in result.text
        assert [part.raw_confidence for part in result.segments] == [None] * 3
        assert asdict(pipeline.transcribe(str(pcm_wav))) == asdict(result)
        assert len(calls) == 4
        # 后处理前恢复原始结果仍不能提前创建最终 Transcript。
        with get_session() as db:
            assert not db.exec(select(Transcript)).all()
    finally:
        for pool in pools:
            pool.shutdown(0)
        server.shutdown()
        thread.join(5)
        server.server_close()
        assert not thread.is_alive()


def test_same_model_path_uses_manifest_content_and_ignores_install_time(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BLC_MODELS_DIR", str(tmp_path))
    manifest = tmp_path / "engine-pack-installed.json"
    data = {
        "model_set_fingerprint": "set-a",
        "engines": {
            "whisper": {"identity": "fixed-model", "content_fingerprint": "content-a", "files": []},
        },
    }
    manifest.write_text(json.dumps(data), encoding="utf-8")
    lease = seed()
    before = ASRCheckpoint(lease, str(pcm_wav), asr_worker_settings(), None, None, no_cancel)
    before.save_raw(ASRTranscriptResult(text="由初始模型生成的内容"))
    data["installed_at"] = "later"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    same = ASRCheckpoint(lease, str(pcm_wav), asr_worker_settings(), None, None, no_cancel)
    assert same.raw() is not None
    data["engines"]["whisper"]["content_fingerprint"] = "content-b"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    changed = ASRCheckpoint(lease, str(pcm_wav), asr_worker_settings(), None, None, no_cancel)
    assert changed.raw() is None


def test_exhausted_postprocessing_recovery_keeps_raw_evidence(temp_db: None, pcm_wav: Path) -> None:
    lease = seed()
    checkpoint = ASRCheckpoint(lease, str(pcm_wav), asr_worker_settings(), None, None, no_cancel)
    checkpoint.save_raw(ASRTranscriptResult(text="已完成的原始识别"))
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task is not None
        task.max_retries = 1
        task.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
        db.add(task)
    recover_stale()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task is not None and task.stage == TaskStatus.FAILED
        assert "unavailable" not in (task.context_json or "")
        assert db.get(AppSetting, checkpoint.prefix + "raw") is not None
