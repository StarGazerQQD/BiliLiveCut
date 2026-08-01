"""转写阶段提交与幂等回归测试。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import select

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_claimed_task() -> tuple[int, int]:
    """创建一个持有有效租约的转写任务，返回 ``(task_id, segment_id)``。"""
    from app.db.models import RawSegment, SegmentTask, TaskStatus
    from app.db.session import get_session

    with get_session() as db:
        segment = RawSegment(session_id=1, seq=0, file_path="test.ts")
        db.add(segment)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=1,
            stage=TaskStatus.TRANSCRIBING,
            claimed_by="worker-test",
            lease_token="lease-test",
        )
        db.add(task)
        db.flush()
        return task.id, segment.id


def _lease(task_id: int):  # noqa: ANN202
    """构造与 ``_seed_claimed_task`` 匹配的租约。"""
    from app.db.models import TaskStatus
    from app.pipeline.lease import TaskLease

    return TaskLease(
        task_id=task_id,
        worker_id="worker-test",
        lease_token="lease-test",
        expected_stage=TaskStatus.TRANSCRIBING,
    )


def test_transcribe_compute_does_not_read_undefined_settings(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    """ASR 成功后应返回可提交结果，不读取不存在的转写版本配置。"""
    from app.analysis.transcription import pipeline as pipeline_module
    from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult, Word
    from app.pipeline.workers.transcribe import transcribe_compute

    class FakePipeline:
        """返回最小合法主引擎结果的测试管线。"""

        def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
            assert audio_path == "test.ts"
            assert initial_prompt is None
            return ASRTranscriptResult(
                text="测试转写",
                segments=[
                    ASRSegmentResult(
                        start=0.0,
                        end=1.0,
                        text="测试转写",
                        words=[Word(word="测试", start=0.0, end=1.0)],
                    )
                ],
                backend="paraformer",
                model_id="paraformer-zh",
                language="zh",
                final_text="测试转写",
            )

    task_id, segment_id = _seed_claimed_task()
    monkeypatch.setattr(pipeline_module, "get_default_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_refine_transcript_for_storage", lambda _text: None)

    result = transcribe_compute(task_id)

    assert result["transcribed"] is True
    assert result["segment_id"] == segment_id
    assert result["text"] == "测试转写"
    assert result["primary_backend"] == "paraformer"
    assert "text_version" not in result


def test_transcribe_compute_stores_clean_text_summary_and_raw_asr(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """LLM 整理正文供分析使用，同时保留原始 ASR 并把摘要写入辅助元数据。"""
    import json

    from app.analysis import llm
    from app.analysis.transcription import pipeline as pipeline_module
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.pipeline.workers.transcribe import transcribe_compute

    class FakePipeline:
        def transcribe(self, _audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
            assert initial_prompt is None
            return ASRTranscriptResult(
                text="原始没有标点的转写",
                final_text="原始没有标点的转写",
                base_text="原始没有标点的转写",
                backend="funasr-nano",
            )

    task_id, _segment_id = _seed_claimed_task()
    monkeypatch.setattr(pipeline_module, "get_default_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(
        pipeline_module,
        "_refine_transcript_for_storage",
        lambda _text: llm.TranscriptRefinement(clean_text="整理后的可读转写。", summary="片段摘要"),
    )

    result = transcribe_compute(task_id)
    auxiliary = json.loads(result["auxiliary_json"])

    assert result["text"] == "整理后的可读转写。"
    assert result["final_text"] == "原始没有标点的转写"
    assert auxiliary["transcript_refinement"] == {"applied": True, "summary": "片段摘要"}


def test_transcribe_compute_rejects_degenerate_text_before_llm(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """所有 ASR 都退化时不得调用 LLM，也不得返回可落库的转写。"""
    from app.analysis.transcription import pipeline as pipeline_module
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.pipeline.workers.transcribe import transcribe_compute

    class FakePipeline:
        def transcribe(self, _audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
            assert initial_prompt is None
            repeated = "等一下我们先看看" * 20
            return ASRTranscriptResult(text=repeated, final_text=repeated, backend="whisper")

    def fail_refine(_text: str) -> None:
        raise AssertionError("退化文本不得进入 LLM")

    task_id, segment_id = _seed_claimed_task()
    monkeypatch.setattr(pipeline_module, "get_default_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_refine_transcript_for_storage", fail_refine)

    result = transcribe_compute(task_id)

    assert result == {"error": "ASR 输出质量不合格: degenerate_repetition", "segment_id": segment_id}


def test_commit_transcript_advances_without_nonexistent_task_field(temp_db: None) -> None:
    """新转写应落库并推进任务，不依赖不存在的 ``task.transcript_id``。"""
    from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id = _seed_claimed_task()
    commit_transcript(
        _lease(task_id),
        {
            "segment_id": segment_id,
            "text": "测试转写",
            "final_text": "测试转写",
            "language": "zh",
            "words_json": "[]",
        },
        12,
    )

    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        task = db.get(SegmentTask, task_id)
        segment = db.get(RawSegment, segment_id)
        assert transcript.text == "测试转写"
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        assert segment is not None and segment.status == SegmentStatus.TRANSCRIBED


def test_commit_transcript_reuses_existing_transcript(temp_db: None) -> None:
    """幂等重试应复用已有转写并修复片段、任务状态。"""
    from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
    from app.db.session import get_session
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id = _seed_claimed_task()
    with get_session() as db:
        db.add(Transcript(segment_id=segment_id, text="已有转写"))

    commit_transcript(_lease(task_id), {"segment_id": segment_id, "text": "不应重复写入"}, 8)

    with get_session() as db:
        transcripts = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).all()
        task = db.get(SegmentTask, task_id)
        segment = db.get(RawSegment, segment_id)
        assert [item.text for item in transcripts] == ["已有转写"]
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        assert segment is not None and segment.status == SegmentStatus.TRANSCRIBED
