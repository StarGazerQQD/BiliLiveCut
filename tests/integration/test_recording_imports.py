"""离线录播导入的实际分段、原子发布、恢复和 API 边界。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlmodel import select

from app.db.entities import AppSetting, Danmaku, LiveRoom, RawSegment, RecordingSession, SegmentTask
from app.db.session import get_session
from app.recording.imports import ImportRequest, create_import, get_import, prepare_import, save_import, upload_path
from app.web.services.background_jobs import JobCancelled, JobContext


def _video(path: Path, *, audio: bool = True) -> None:
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
    command += ["-t", "12", "-c:v", "libx264", "-g", "20", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run([*command, str(path)], check=True, capture_output=True, timeout=30)


def _ready_import(monkeypatch: MonkeyPatch, *, comments: bool = True, audio: bool = True) -> str:
    from app.core.config import settings

    monkeypatch.setattr(settings, "segment_duration_s", 5)
    record = create_import(
        ImportRequest(title="已有录播", video_name="source.mp4", comments_name="comments.xml" if comments else None),
        "local-admin",
    )
    _video(upload_path(record, "video"), audio=audio)
    if comments:
        upload_path(record, "comments").write_text('<i><d p="1.25">第一条</d><d p="8">第二条</d></i>', encoding="utf-8")
    record.video_uploaded = record.sealed = True
    record.comments_uploaded = comments
    with get_session() as db:
        save_import(db, record)
    monkeypatch.setattr(JobContext, "report", lambda self, progress, message: None)
    return record.id


def _configure_llm(monkeypatch: MonkeyPatch, replies: list[str | None]) -> list[dict[str, object]]:
    """仅替换外部 SDK 边界，验证持久配置、请求构造和结果处理的实际链路。"""
    from app.analysis import llm, llm_providers

    llm_providers.save_providers(
        [
            llm_providers.LLMProvider(
                id="fixture",
                name="测试服务",
                base_url="https://example.invalid/v1",
                api_key="fixture-key",
                model="fixture-model",
            )
        ]
    )
    calls: list[dict[str, object]] = []

    def complete(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        assert replies, "出现了未预期的 LLM 请求"
        content = replies.pop(0)
        if content is None:
            raise RuntimeError("模拟服务暂不可用")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")], usage=None
        )

    def client(provider: llm_providers.LLMProvider) -> SimpleNamespace:
        assert provider.base_url == "https://example.invalid/v1"
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))

    monkeypatch.setattr(llm, "_get_client", client)
    return calls


def test_real_media_import_is_atomic_idempotent_and_aligned(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.hotspot_detector import build_segment_signal_buckets
    from app.analysis.source_policy import session_danmaku_lag_s, session_has_danmaku
    from app.analysis.timeline import representative_danmaku
    from app.pipeline.scheduler import advance_recorded
    from app.web.services.dashboard import dashboard_state

    import_id = _ready_import(monkeypatch)
    context = JobContext("test", threading.Event())
    result = prepare_import(context, import_id)
    assert prepare_import(context, import_id) == result
    record = get_import(import_id)
    assert record.segment_count == 3  # 目标切点 5、10 秒，实际按 2 秒关键帧边界切分。
    assert record.comment_count == 2
    with get_session() as db:
        sessions = db.exec(select(RecordingSession)).all()
        segments = db.exec(select(RawSegment).order_by(RawSegment.seq)).all()
        tasks = db.exec(select(SegmentTask)).all()
        comments = db.exec(select(Danmaku).order_by(Danmaku.ts)).all()
        room = db.exec(select(LiveRoom)).one()
        assert len(sessions) == 1 and len(tasks) == len(segments) == 3
        assert room.platform == "local" and room.room_id is None
        assert not any((room.enabled, room.auto_record, room.auto_render, room.auto_upload, room.auto_approve))
        assert room.auto_analyze
        assert room.room_config_json is None  # 导入不强制关闭用户配置的分析插件。
        assert segments[1].start_ts == segments[0].end_ts
        assert segments[0].start_ts == sessions[0].started_at
        assert segments[0].duration_s == pytest.approx(6, abs=0.01)
        assert segments[-1].duration_s == pytest.approx(2, abs=0.01)
        assert comments[0].ts - sessions[0].started_at == timedelta(seconds=1.25)
        assert all(Path(segment.file_path).is_file() for segment in segments)
        first_id = segments[0].id
        session_id = sessions[0].id
        start = sessions[0].started_at
    assert session_id is not None and first_id is not None
    assert session_danmaku_lag_s(session_id) == 0
    assert session_has_danmaku(session_id)
    assert representative_danmaku(session_id, start, start + timedelta(seconds=2))[0]["text"] == "第一条"
    buckets, _ = build_segment_signal_buckets(first_id)
    assert sum(bucket.danmaku_count or 0 for bucket in buckets) == 1
    assert dashboard_state()["rooms"] == []
    assert not upload_path(record, "video").exists()
    advance_recorded()
    with get_session() as db:
        assert all(task.stage == "queued_for_analysis" for task in db.exec(select(SegmentTask)).all())


def test_no_comments_is_unavailable_evidence(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.hotspot_detector import build_segment_signal_buckets

    import_id = _ready_import(monkeypatch, comments=False)
    prepare_import(JobContext("test", threading.Event()), import_id)
    with get_session() as db:
        segment = db.exec(select(RawSegment)).first()
        assert segment is not None and segment.id is not None
        buckets, _ = build_segment_signal_buckets(segment.id)
    assert all(bucket.danmaku_count is None for bucket in buckets)


def test_invalid_comments_and_audio_publish_no_tasks(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    import_id = _ready_import(monkeypatch)
    record = get_import(import_id)
    upload_path(record, "comments").write_text("<invalid>", encoding="utf-8")
    with pytest.raises(ValueError, match="XML"):
        prepare_import(JobContext("test", threading.Event()), import_id)
    with get_session() as db:
        assert db.exec(select(SegmentTask)).all() == []
        assert db.exec(select(RecordingSession)).all() == []
    assert upload_path(record, "video").is_file()
    silent_id = _ready_import(monkeypatch, comments=False, audio=False)
    with pytest.raises(ValueError, match="音频"):
        prepare_import(JobContext("test", threading.Event()), silent_id)


def test_cancelled_import_can_resume_without_duplicate_records(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    import_id = _ready_import(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(JobCancelled):
        prepare_import(JobContext("test", cancelled), import_id)
    assert get_import(import_id).session_id is None
    cancelled.clear()
    assert prepare_import(JobContext("test", cancelled), import_id)["session_id"] is not None


def test_publication_failure_rolls_back_all_entities(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    import app.recording.imports as module

    import_id = _ready_import(monkeypatch)
    original = module.save_import

    def fail_publication(db: object, record: object) -> None:
        raise RuntimeError("模拟提交失败")

    monkeypatch.setattr(module, "save_import", fail_publication)
    with pytest.raises(RuntimeError, match="提交失败"):
        prepare_import(JobContext("test", threading.Event()), import_id)
    with get_session() as db:
        for model in (LiveRoom, RecordingSession, RawSegment, Danmaku, SegmentTask):
            assert not db.exec(select(model)).all()
    assert not list((upload_path(get_import(import_id), "video").parent / "segments").glob("*.mkv"))
    monkeypatch.setattr(module, "save_import", original)
    assert prepare_import(JobContext("test", threading.Event()), import_id)["session_id"] is not None


def test_upload_limits_sealing_and_cleanup(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.web.main import _rate_buckets, app
    from app.web.routers import recording_imports as router

    _rate_buckets.clear()
    monkeypatch.setattr(router, "MAX_VIDEO_BYTES", 8)
    client = TestClient(app)  # 不启动直播/分析后台；此用例只测上传协议。
    response = client.post("/api/recording-imports", json={"title": "录像", "video_name": "../bad.mp4"})
    assert response.status_code == 400
    data = client.post("/api/recording-imports", json={"title": "录像", "video_name": "ok.mp4"}).json()
    import_id = data["id"]
    endpoint = f"/api/recording-imports/{import_id}"
    assert client.post(endpoint + "/start").status_code == 409
    assert client.put(endpoint + "/files/video", content=b"123456789").status_code == 413
    assert client.put(endpoint + "/files/video", content=b"1234").status_code == 200
    assert client.put(endpoint + "/files/video", content=b"1234").status_code == 409
    assert client.put(endpoint + "/files/comments", content=b"abc").status_code == 400
    assert client.delete(endpoint).status_code == 200
    assert not client.get("/api/recording-imports").json()["imports"]


def test_import_declaration_retries_are_idempotent_and_owner_bound(temp_db: None) -> None:
    request = ImportRequest(title="重试声明", video_name="source.mp4", request_id="a" * 32)
    first = create_import(request, "owner")
    assert create_import(request, "owner").id == first.id
    with pytest.raises(ValueError, match="已使用"):
        create_import(request, "someone-else")
    with pytest.raises(ValueError, match="已使用"):
        create_import(request.model_copy(update={"title": "不同内容"}), "owner")
    with get_session() as db:
        assert len(db.exec(select(AppSetting).where(AppSetting.key.startswith("recording_import:"))).all()) == 1


def test_import_page_progress_and_exact_session_results(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.db.entities import TaskStatus, Transcript
    from app.pipeline.task_context import update_event_first_context
    from app.web.main import _rate_buckets, app

    import_id = _ready_import(monkeypatch, comments=False)
    prepare_import(JobContext("test", threading.Event()), import_id)
    imported_session_id = get_import(import_id).session_id
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).order_by(SegmentTask.id)).all()
        tasks[0].stage = TaskStatus.TRANSIENT_FAILED
        tasks[0].failed_stage = TaskStatus.TRANSCRIBING
        tasks[1].stage = TaskStatus.FAILED
        tasks[1].failed_stage = TaskStatus.RENDERING
        tasks[2].stage = TaskStatus.COMPLETED
        tasks[2].context_json = update_event_first_context(None, asr_evidence_state="unavailable")
        for task in tasks:
            db.add(task)
        db.add(Transcript(segment_id=tasks[1].segment_id, final_text="本地转写"))
        # 足够多的新场次，确保导入结果链接不能只筛选最近 30 条的响应。
        session = db.get(RecordingSession, imported_session_id)
        assert session is not None and session.started_at is not None
        for index in range(35):
            db.add(RecordingSession(room_id=session.room_id, started_at=session.started_at + timedelta(days=index + 1)))
    _rate_buckets.clear()
    client = TestClient(app)
    page = client.get("/imports")
    assert page.status_code == 200
    assert '/?tab=models"' in page.text and "LLM 服务商" in page.text
    assert '/imports"' in client.get("/").text
    progress = client.get("/api/recording-imports").json()["imports"][0]["analysis"]
    assert progress == {"total": 3, "done": 2, "failed": 0, "retrying": 1, "asr_unavailable": 1, "transcripts": 1}
    timeline = client.get(f"/api/sessions/timeline?session_id={imported_session_id}").json()
    assert len(timeline) == 1 and timeline[0]["session_id"] == imported_session_id
    assert timeline[0]["session_title"] == "已有录播"
    assert timeline[0]["source_label"].startswith("本地录播")
    rows = client.get(f"/api/transcripts?session_id={imported_session_id}").json()
    assert len(rows) == 1 and rows[0]["source_file_name"].endswith(".mkv")
    assert rows[0]["source_mp4_available"] is False


def test_whisper_primary_does_not_require_fallback_switch(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.transcription.backends import FasterWhisperBackend
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.analysis.transcription.pipeline import ASRPipeline
    from app.core.config import settings

    import_id = _ready_import(monkeypatch, comments=False)
    monkeypatch.setattr(settings, "asr_primary", "whisper")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    calls: list[str] = []

    def recognize(
        self: FasterWhisperBackend, audio_path: str, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        assert Path(audio_path).is_file()
        calls.append(audio_path)
        return ASRTranscriptResult(text="本地主引擎结果", backend="whisper")

    monkeypatch.setattr(FasterWhisperBackend, "transcribe", recognize)
    result = ASRPipeline().transcribe(str(upload_path(get_import(import_id), "video")))
    assert result.text == "本地主引擎结果" and len(calls) == 1


def test_disguised_playlist_is_rejected_before_registration(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    import_id = _ready_import(monkeypatch, comments=False)
    record = get_import(import_id)
    upload_path(record, "video").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nfile:///private.ts\n#EXT-X-ENDLIST\n", encoding="utf-8"
    )
    with pytest.raises(subprocess.CalledProcessError):
        prepare_import(JobContext("test", threading.Event()), import_id)
    assert get_import(import_id).session_id is None


@pytest.mark.parametrize("provider_mode", ["enabled", "disabled", "unavailable"])
def test_local_asr_uses_configured_llm_for_refinement_and_full_summary(
    temp_db: None, monkeypatch: MonkeyPatch, provider_mode: str
) -> None:
    from app.analysis import llm_providers
    from app.analysis.session_summary import (
        build_session_timeline_summary,
        claim_pending_session_summary,
        ensure_session_timeline_summary_requested,
        execute_session_summary_claim,
        session_timeline_summary_view,
    )
    from app.analysis.transcription import pipeline
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.core import settings_store
    from app.db.entities import TaskStatus, Transcript
    from app.pipeline.workers.transcribe import transcribe_compute

    import_id = _ready_import(monkeypatch, comments=False)
    prepare_import(JobContext("test", threading.Event()), import_id)
    text = "今天我们来讨论这个游戏的操作方法。这一段需要先观察对手，再选择合适的位置。"
    cleaned = "今天讨论游戏操作方法。这一段需要先观察对手，再选择合适的位置。"
    expected_summary = "这场录播讲解了观察对手与选择位置的操作方法。"
    monkeypatch.setattr(settings_store, "transcript_llm_refine_enabled", lambda: True)
    replies = (
        [json.dumps({"clean_text": cleaned, "summary": "游戏操作方法"}), json.dumps({"summary": expected_summary})]
        if provider_mode == "enabled"
        else [None, None]
    )
    calls = _configure_llm(monkeypatch, replies)
    if provider_mode == "disabled":
        llm_providers.save_providers([])
    asr_calls: list[str] = []

    class LocalEngine:
        def transcribe(self, path: str, *, initial_prompt: str | None) -> ASRTranscriptResult:
            assert Path(path).is_file()
            asr_calls.append(path)
            return ASRTranscriptResult(text=text, backend="whisper", model_id="local-fixture", audio_duration=6)

    monkeypatch.setattr(pipeline, "get_task_pipeline", lambda: LocalEngine())
    with get_session() as db:
        tasks = db.exec(select(SegmentTask)).all()
        task_id = tasks[0].id
    assert task_id is not None
    result = transcribe_compute(task_id=task_id)
    assert result["final_text"] == text  # 带时间戳的原始转写保留，整理文本单独展示。
    assert result["text"] == (cleaned if provider_mode == "enabled" else text)
    refinement = json.loads(result["auxiliary_json"]).get("transcript_refinement")
    assert bool(refinement and refinement["applied"]) == (provider_mode == "enabled")
    assert len(asr_calls) == 1
    with get_session() as db:
        for task in db.exec(select(SegmentTask)).all():
            task.stage = TaskStatus.COMPLETED
            db.add(task)
            db.add(Transcript(segment_id=task.segment_id, final_text=f"分段{task.segment_id}：{text}"))
    session_id = get_import(import_id).session_id
    assert session_id is not None
    if provider_mode != "enabled":
        with pytest.raises(RuntimeError, match="整场 ASR 分析未返回有效 summary"):
            build_session_timeline_summary(session_id)
        assert len(calls) == (0 if provider_mode == "disabled" else 2)
        return
    summary = build_session_timeline_summary(session_id)
    assert summary["source"] == "llm" and summary["summary"] == expected_summary
    assert len(calls) == 2 and all(call["model"] == "fixture-model" for call in calls)
    assert text in str(calls[0]["messages"])
    assert all(f"分段{task.segment_id}" in str(calls[1]["messages"]) for task in tasks)

    # 已有转写摘录必须自动失效，用原有转写重新调用 LLM，无需重新导入/ASR。
    with get_session() as db:
        db.add(
            AppSetting(key=f"session_timeline_summary:{session_id}", value=json.dumps({**summary, "source": "local"}))
        )
    replies.append(json.dumps({"summary": expected_summary}))
    assert ensure_session_timeline_summary_requested(session_id)
    claim = claim_pending_session_summary()
    assert claim is not None and execute_session_summary_claim(claim)
    assert session_timeline_summary_view(session_id, processing_state="ready", ended=True)["source"] == "llm"
    assert len(asr_calls) == 1 and len(calls) == 3


def test_imported_hotspot_reanalysis_replaces_cached_fallback_with_llm(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.clip_scorer import (
        compute_hotspot_clip_draft,
        mark_event_clip_evaluated,
        pending_hotspot_event_ids,
    )
    from app.analysis.event_enricher import commit_event_enrichment, compute_event_enrichment
    from app.analysis.reanalysis import queue_session_reanalysis
    from app.db.entities import HotspotEvent, TaskStatus, Transcript
    from app.pipeline.scheduler import advance_recorded
    from app.pipeline.workers.analyze import analyze_compute

    import_id = _ready_import(monkeypatch, comments=False)
    prepare_import(JobContext("test", threading.Event()), import_id)
    advance_recorded()
    text = "主播宣布周末挑战规则，挑战完成后抽取奖励。"
    with get_session() as db:
        segment = db.exec(select(RawSegment).order_by(RawSegment.seq)).first()
        assert segment is not None and segment.start_ts is not None
        for task in db.exec(select(SegmentTask)).all():
            task.stage = TaskStatus.COMPLETED
            db.add(task)
            db.add(Transcript(segment_id=task.segment_id, final_text=text))
        task_id = db.exec(select(SegmentTask.id).where(SegmentTask.segment_id == segment.id)).one()
        session_id = segment.session_id
        event = HotspotEvent(
            session_id=session_id,
            event_key="imported-announcement",
            status="confirmed",
            start_ts=segment.start_ts + timedelta(seconds=1),
            peak_ts=segment.start_ts + timedelta(seconds=2),
            end_ts=segment.start_ts + timedelta(seconds=4),
            transcript_text=text,
            semantic_confidence=0.3,
            heat_score=0.8,
            clip_score=0.8,
            evidence_coverage=0.8,
            evidence_json=json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "id": "evidence:asr:import",
                            "type": "asr",
                            "score": 0.8,
                            "detail": {"quality": {"state": "available", "usable": True}},
                            "excerpts": [text],
                        }
                    ],
                }
            ),
        )
        db.add(event)
        db.flush()
        event_id = event.id
    assert event_id is not None and task_id is not None
    fallback = compute_event_enrichment(event_id)
    assert fallback is not None and fallback.source == "fallback"
    with get_session() as db:
        assert commit_event_enrichment(db, fallback)
    cached_score = compute_hotspot_clip_draft(event_id)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        mark_event_clip_evaluated(db, event, cached_score)
    assert event_id not in pending_hotspot_event_ids(session_id)

    calls = _configure_llm(
        monkeypatch,
        [
            json.dumps(
                {
                    "title": "主播宣布周末挑战规则",
                    "summary": text,
                    "category": "announcement",
                    "entities": ["周末挑战"],
                    "semantic_confidence": 0.8,
                    "evidence_ids": ["evidence:asr:import"],
                }
            )
        ],
    )
    assert queue_session_reanalysis(session_id, reason="llm_enabled").queued == 3
    result = analyze_compute(task_id)
    drafts = result["event_clip_results"]
    assert len(drafts) == 1
    enrichment = drafts[0]["enrichment"]
    assert enrichment is not None and enrichment.source == "llm"
    assert enrichment.title == "主播宣布周末挑战规则"
    assert len(calls) == 1 and text in str(calls[0]["messages"])
    with get_session() as db:
        assert commit_event_enrichment(db, enrichment)
        event = db.get(HotspotEvent, event_id)
        assert event is not None and event.title == enrichment.title


def test_job_restart_reuses_published_session(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.web.services.background_jobs import WebJobManager, get_job

    import_id = _ready_import(monkeypatch)
    # 先落库场次，模拟进程在作业结果写回前中断。
    result = prepare_import(JobContext("test", threading.Event()), import_id)

    async def run() -> None:
        manager = WebJobManager()
        job = await manager.enqueue("local_import", {"import_id": import_id}, label="导入", owner="local-admin")
        await asyncio.gather(*tuple(manager._tasks.values()))
        state = get_job(job["id"])
        assert state is not None and state["status"] == "succeeded"
        assert state["result"] == result
        await manager.stop()

    asyncio.run(run())
    with get_session() as db:
        assert len(db.exec(select(RecordingSession)).all()) == 1
        assert len(db.exec(select(SegmentTask)).all()) == 3
        source = db.get(AppSetting, f"local_source:{result['session_id']}")
        assert source is not None and json.loads(source.value)["has_comments"]


def test_imported_clip_and_collection_use_configured_llm(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.db.entities import FinalClip, HighlightCandidate, HighlightEvent, HighlightTopic, Topic, Transcript
    from app.publishing import collection_copywriter, copywriter

    import_id = _ready_import(monkeypatch, comments=False)
    prepare_import(JobContext("test", threading.Event()), import_id)
    with get_session() as db:
        segment = db.exec(select(RawSegment)).first()
        assert segment and segment.start_ts and segment.end_ts
        candidate = HighlightCandidate(
            session_id=segment.session_id,
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            peak_ts=segment.start_ts,
            dedup_hash="local-clip",
            reason="精彩操作",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id, session_id=segment.session_id, segment_id=segment.id, asr_text="这一段是精彩操作"
        )
        topic = Topic(title="本地合集", session_id=segment.session_id)
        clip = FinalClip(candidate_id=candidate.id, file_path="not-rendered.mp4")
        db.add(Transcript(segment_id=segment.id, final_text="这一段是精彩操作"))
        db.add(event)
        db.add(topic)
        db.add(clip)
        db.flush()
        db.add(HighlightTopic(event_id=event.id, topic_id=topic.id))
        clip_id, topic_id = clip.id, topic.id

    calls = _configure_llm(
        monkeypatch,
        [
            json.dumps(
                {
                    "title": "LLM 单片标题",
                    "description": "单片描述",
                    "tags": ["操作"],
                    "cover_suggestion": "操作瞬间",
                    "publish_suggestion": "可以发布",
                    "worth_publishing": True,
                }
            ),
            json.dumps(
                {
                    "title": "LLM 合集标题",
                    "description": "合集描述",
                    "summary": "操作汇总",
                    "tags": ["操作"],
                    "cover_title": "操作合集",
                    "chapters": [],
                }
            ),
        ],
    )
    assert clip_id is not None and topic_id is not None
    assert copywriter.generate_copy(clip_id).title == "LLM 单片标题"
    assert collection_copywriter.generate_copywriter_for_topic(topic_id)["title"] == "LLM 合集标题"
    assert len(calls) == 2
