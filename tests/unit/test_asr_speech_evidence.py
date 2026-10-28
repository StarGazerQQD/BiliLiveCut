"""空语音必须有真实音频证据；SDK/VAD 仅在外部模型边界使用替身。"""

from __future__ import annotations

import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.analysis.transcription.audio_normalization import inspect_pcm_audio, normalized_asr_audio
from app.analysis.transcription.backends import FasterWhisperBackend, FunASRBackend
from app.analysis.transcription.models import ASRTranscriptResult, EmotionEvent
from app.analysis.transcription.pipeline import ASRPipeline
from app.analysis.transcription.speech_evidence import no_speech_result, require_final_result
from app.core.config import settings


@pytest.mark.parametrize("signal", ["silence", "music", "noise"])
def test_valid_non_speech_bypasses_whisper_and_preserves_evidence(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, signal: str
) -> None:
    count = 133376
    samples = np.zeros(count, dtype="<i2")
    if signal == "music":
        samples = (np.sin(np.arange(count) * 2 * np.pi * 440 / 16000) * 6000).astype("<i2")
    elif signal == "noise":
        samples = np.random.default_rng(42).integers(-2000, 2000, count, dtype="<i2")
    with wave.open(str(pcm_wav), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])

    def forbidden(_self: FasterWhisperBackend) -> object:
        pytest.fail("VAD 明确无语音时不能进入 Whisper 原生推理")

    monkeypatch.setattr(FasterWhisperBackend, "_load_model", forbidden)
    result = FasterWhisperBackend().transcribe(str(pcm_wav))
    assert result.result_type == "no_speech"
    assert result.audio_duration == 8.336
    assert result.metadata["audio_evidence"]["sample_count"] == count
    assert (result.metadata["audio_evidence"]["rms"] == 0) == (signal == "silence")
    require_final_result(result)


def test_valid_speech_with_empty_generator_remains_failed(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "whisper")
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [{"start": 0, "end": len(audio)}])
    model = SimpleNamespace(
        model=SimpleNamespace(device="cpu"),
        transcribe=lambda *args, **kwargs: (iter(()), SimpleNamespace(language="zh")),
    )
    monkeypatch.setattr(FasterWhisperBackend, "_load_model", lambda self: model)
    with pytest.raises(RuntimeError, match="ASREmptyOutput"):
        ASRPipeline().transcribe(str(pcm_wav))


def test_all_engines_empty_preserves_music_and_laughter(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])
    events = [EmotionEvent("music", 0, 8.336), EmotionEvent("laughter", 1, 2)]
    backend = SimpleNamespace(
        transcribe_funasr=lambda *args: ASRTranscriptResult(text="", backend="funasr-nano", emotions=events[:1]),
        transcribe=lambda *args: ASRTranscriptResult(text="", backend="paraformer", emotions=events[1:]),
    )
    result = ASRPipeline(primary_backend=backend).transcribe(str(pcm_wav))
    assert result.result_type == "no_speech"
    assert {event.event_type for event in result.emotions} == {"music", "laughter"}
    assert result.metadata["audio_evidence"]["duration_s"] == 8.336


def test_model_exception_cannot_be_hidden_as_silence(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "paraformer")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setattr("faster_whisper.vad.get_speech_timestamps", lambda audio: [])

    def broken(*args: object) -> ASRTranscriptResult:
        raise ValueError("invalid model weights")

    backend = SimpleNamespace(transcribe=broken, primary_revision="v2")
    with pytest.raises(RuntimeError, match="invalid model weights") as caught:
        ASRPipeline(primary_backend=backend).transcribe(str(pcm_wav))
    assert isinstance(caught.value.__cause__, ValueError)


def test_device_failure_preserves_real_vad_whisper_fallback(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_primary_device", "cuda")
    monkeypatch.setattr(settings, "asr_fallback_whisper", True)
    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(__version__="2.13.0+cpu", version=SimpleNamespace(cuda=None))
    )
    result = ASRPipeline().transcribe(str(pcm_wav))
    assert result.result_type == "no_speech" and result.fallback_backend == "whisper"
    assert result.primary_error_type == "ASRDeviceError"
    assert result.metadata["vad_speech_segments"] == 0
    assert result.metadata["audio_evidence"]["sample_count"] == 133376


def test_all_enabled_models_blocked_by_device_error_is_not_retryable(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.asr_failures import ASRUnavailableError

    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_primary_device", "cuda")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(__version__="2.13.0+cpu", version=SimpleNamespace(cuda=None))
    )
    pipeline = ASRPipeline()
    with pytest.raises(ASRUnavailableError):
        pipeline.transcribe(str(pcm_wav))
    assert pipeline._backend_errors == [] and pipeline._last_backend_error is None


@pytest.mark.parametrize("speech", [False, True])
@pytest.mark.parametrize("secondary_text", ["", "重复文本" * 80])
def test_completed_secondary_prevents_permanent_unavailability(
    pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, speech: bool, secondary_text: str
) -> None:
    from app.core.asr_failures import ASRUnavailableError

    monkeypatch.setattr(
        "faster_whisper.vad.get_speech_timestamps", lambda audio: [{"start": 0, "end": len(audio)}] if speech else []
    )
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_primary_device", "cuda")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    probes = 0

    def zeros(size: int, *, device: str) -> SimpleNamespace:
        nonlocal probes
        probes += 1
        if probes == 1:
            raise RuntimeError("no kernel image is available for execution on the device")
        return SimpleNamespace(add_=lambda value: None)

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            __version__="2.13.0+cu130",
            version=SimpleNamespace(cuda="13.0"),
            cuda=SimpleNamespace(is_available=lambda: True, device_count=lambda: 1, synchronize=lambda index: None),
            zeros=zeros,
        ),
    )
    module = SimpleNamespace(parameters=lambda: iter([SimpleNamespace(device="cuda:0")]), float=lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "funasr",
        SimpleNamespace(
            AutoModel=lambda **kwargs: SimpleNamespace(
                model=module, vad_model=module, punc_model=module, generate=lambda **kwargs: [{"text": secondary_text}]
            )
        ),
    )
    pipeline = ASRPipeline(primary_backend=FunASRBackend(sensevoice=False))
    if secondary_text:
        with pytest.raises(RuntimeError, match="ASRQualityError") as caught:
            pipeline.transcribe(str(pcm_wav))
        assert not isinstance(caught.value, ASRUnavailableError)
    elif speech:
        with pytest.raises(RuntimeError, match="ASREmptyOutput") as caught:
            pipeline.transcribe(str(pcm_wav))
        assert not isinstance(caught.value, ASRUnavailableError)
    else:
        result = pipeline.transcribe(str(pcm_wav))
        assert result.result_type == "no_speech"
        assert result.backend == result.fallback_backend == "paraformer"
        assert result.primary_error_type == "ASRDeviceError"
        assert result.metadata["vad_speech_segments"] == 0
    assert probes == 2 and pipeline._backend_completed is False


@pytest.mark.parametrize(
    "metadata", [{}, {"vad_speech_segments": False}, {"vad_speech_segments": 0, "audio_evidence": {}}]
)
def test_no_speech_requires_valid_evidence(metadata: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="ASRNoSpeechEvidence"):
        require_final_result(ASRTranscriptResult(text="", result_type="no_speech", metadata=metadata))


@pytest.mark.parametrize("corruption", ["zero_file", "zero_frames", "truncated", "missing"])
def test_invalid_input_is_not_no_speech(pcm_wav: Path, corruption: str) -> None:
    if corruption == "zero_file":
        pcm_wav.write_bytes(b"")
    elif corruption == "zero_frames":
        with wave.open(str(pcm_wav), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"")
    elif corruption == "truncated":
        pcm_wav.write_bytes(pcm_wav.read_bytes()[:-100])
    else:
        pcm_wav.unlink()
    with pytest.raises((ValueError, EOFError, wave.Error, FileNotFoundError)):
        no_speech_result(str(pcm_wav), backend="whisper")


def test_vad_decoder_mismatch_is_failure(pcm_wav: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("faster_whisper.audio.decode_audio", lambda *args, **kwargs: np.zeros(3))
    with pytest.raises(ValueError, match="不一致"):
        no_speech_result(str(pcm_wav), backend="whisper")


def test_pcm_scan_observes_cancellation(pcm_wav: Path) -> None:
    def cancelled() -> None:
        raise RuntimeError("cancelled scan")

    with pytest.raises(RuntimeError, match="cancelled scan"):
        inspect_pcm_audio(str(pcm_wav), check_cancelled=cancelled)
    with pytest.raises(RuntimeError, match="cancelled scan"):
        with normalized_asr_audio(str(pcm_wav), check_cancelled=cancelled):
            pytest.fail("不能返回已取消音频")
