"""可恢复引擎失败与最终 ASR 结果的日志边界。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from loguru import logger

from app.analysis.transcription.models import ASRTranscriptResult
from app.analysis.transcription.pipeline import ASRPipeline
from app.analysis.transcription.progress import set_progress_sink
from app.core.config import settings

LogEntry = tuple[str, str, bool]
ProgressEntry = tuple[str, dict[str, object]]


@pytest.fixture()
def captured_logs() -> Iterator[tuple[list[LogEntry], list[ProgressEntry]]]:
    records: list[LogEntry] = []
    events: list[ProgressEntry] = []
    sink = logger.add(
        lambda message: records.append(
            (message.record["level"].name, message.record["message"], message.record["exception"] is not None)
        )
    )
    set_progress_sink(lambda stage, details: events.append((stage, details)))
    try:
        yield records, events
    finally:
        set_progress_sink(None)
        logger.remove(sink)


class MissingPrimary:
    """仅替换模型边界，复用真实回退与结果校验。"""

    nano_revision = primary_revision = "test-revision"

    def __init__(self, secondary_available: bool = False) -> None:
        self.secondary_available = secondary_available

    def transcribe_funasr(self, *args: object) -> ASRTranscriptResult:
        raise RuntimeError("主引擎不可用; token=test-private-value") from ModuleNotFoundError(
            "No module named 'funasr'"
        )

    def transcribe(self, *args: object) -> ASRTranscriptResult:
        if self.secondary_available:
            return ASRTranscriptResult(text="这是次级引擎识别得到的完整测试文本", backend="paraformer")
        raise RuntimeError("Paraformer 不可用")


class WhisperFallback:
    """固定输出或抛错，不加载模型。"""

    def __init__(self, outcome: str = "speech") -> None:
        self.outcome = outcome

    def transcribe(self, *args: object) -> ASRTranscriptResult:
        if self.outcome == "exception":
            raise RuntimeError("Whisper 推理失败")
        return ASRTranscriptResult(
            text="这是兜底引擎识别得到的完整测试文本" if self.outcome == "speech" else "",
            backend="whisper",
            result_type="no_speech" if self.outcome == "no_speech" else "speech",
        )


@pytest.mark.parametrize(
    ("primary", "fallback"),
    [("funasr_nano", "whisper"), ("funasr_nano", "paraformer"), ("paraformer", "whisper")],
)
def test_recovered_engine_failures_are_warning_and_completion_is_forwarded_as_info(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
    captured_logs: tuple[list[LogEntry], list[ProgressEntry]],
    primary: str,
    fallback: str,
) -> None:
    monkeypatch.setattr(settings, "asr_primary", primary)
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    pipeline = ASRPipeline(
        primary_backend=MissingPrimary(secondary_available=fallback == "paraformer"),  # type: ignore[arg-type]
        whisper_backend=WhisperFallback(),  # type: ignore[arg-type]
    )
    result = pipeline.transcribe(str(pcm_wav))
    records, events = captured_logs
    warnings = [entry for entry in records if entry[0] == "WARNING"]
    assert result.fallback_backend == fallback and result.primary_status == "failed"
    assert warnings and not any(level in {"ERROR", "CRITICAL"} for level, _, _ in records)
    assert all(not exception for _, _, exception in warnings)
    assert "test-private-value" not in "\n".join(message for _, message, _ in records)
    assert any(level == "INFO" and "fallback_completed" in message for level, message, _ in records)
    assert [details for stage, details in events if stage == "fallback_completed"] == [
        {
            "primary_backend": "funasr-nano" if primary == "funasr_nano" else "paraformer",
            "fallback_backend": fallback,
            "trigger_reason": "primary_exception",
            "result_type": "speech",
        }
    ]


@pytest.mark.parametrize("outcome", ["no_speech", "empty", "exception"])
def test_failed_fallback_does_not_report_success_or_hide_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
    captured_logs: tuple[list[LogEntry], list[ProgressEntry]],
    outcome: str,
) -> None:
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    pipeline = ASRPipeline(
        primary_backend=MissingPrimary(),  # type: ignore[arg-type]
        whisper_backend=WhisperFallback(outcome),  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError):
        pipeline.transcribe(str(pcm_wav))
    records, events = captured_logs
    assert not any(stage == "fallback_completed" for stage, _ in events)
    assert not any("fallback_completed" in message for _, message, _ in records)


def test_remote_fallback_also_reports_validated_completion(
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
    captured_logs: tuple[list[LogEntry], list[ProgressEntry]],
) -> None:
    requests: list[httpx.Request] = []
    client = httpx.Client

    def unavailable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/v1/audio/transcriptions"
        return httpx.Response(503, request=request)

    def factory(**kwargs: object) -> httpx.Client:
        return client(transport=httpx.MockTransport(unavailable), **kwargs)

    monkeypatch.setattr(settings, "asr_primary", "openai_compatible")
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    monkeypatch.setattr(settings, "asr_remote_base_url", "https://asr.example.test/v1")
    monkeypatch.setattr(settings, "asr_remote_model", "fixture-asr")
    monkeypatch.setattr(httpx, "Client", factory)
    result = ASRPipeline(whisper_backend=WhisperFallback()).transcribe(str(pcm_wav))  # type: ignore[arg-type]
    records, events = captured_logs
    assert result.primary_error_type == "RemoteTranscriptionError"
    assert len(requests) == 1
    assert not any(level == "ERROR" for level, _, _ in records)
    completed = [details for stage, details in events if stage == "fallback_completed"]
    assert len(completed) == 1 and completed[0]["primary_backend"] == "openai_compatible"


async def test_optional_preload_failure_is_warning(
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: tuple[list[LogEntry], list[ProgressEntry]],
) -> None:
    from app.pipeline.task_worker import TaskWorker

    def unavailable() -> None:
        raise RuntimeError("预加载模型不可用")

    monkeypatch.setattr("app.analysis.transcription.isolated.preload_isolated_models", unavailable)
    await TaskWorker()._preload_models()
    records, _ = captured_logs
    assert any(level == "WARNING" and "ASR 预加载失败" in message for level, message, _ in records)
    assert not any(level == "ERROR" for level, _, _ in records)
