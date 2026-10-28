"""The optional real-model check fails clearly before any implicit model setup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult
from app.analysis.transcription.speech_evidence import no_speech_result
from scripts.check_local_asr import validate_result


@pytest.mark.parametrize("engine", ["whisper", "paraformer", "funasr_nano"])
def test_local_check_requires_the_selected_engine_to_produce_speech(engine: str) -> None:
    backend = "funasr-nano" if engine == "funasr_nano" else engine
    result = ASRTranscriptResult(
        text="本地语音",
        backend=backend,
        audio_duration=2,
        segments=[ASRSegmentResult(start=0, end=2, text="本地语音")],
    )
    assert validate_result(result, engine=engine, expect="speech")
    result.primary_backend = backend
    result.primary_status = "failed"
    result.fallback_backend = "whisper"
    with pytest.raises(AssertionError, match="did not complete primary inference"):
        validate_result(result, engine=engine, expect="speech")


def test_local_check_rejects_wrong_engine_and_invalid_timestamps() -> None:
    result = ASRTranscriptResult(text="本地语音", backend="whisper", audio_duration=2)
    with pytest.raises(AssertionError, match="did not complete primary inference"):
        validate_result(result, engine="paraformer", expect="speech")
    result.segments = [ASRSegmentResult(start=0, end=3, text="本地语音")]
    with pytest.raises(AssertionError, match="timestamps"):
        validate_result(result, engine="whisper", expect="speech")


def test_local_check_does_not_claim_vad_silence_verified_model_inference(pcm_wav: Path) -> None:
    result = no_speech_result(str(pcm_wav), backend="whisper")
    assert result is not None
    assert validate_result(result, engine="whisper", expect="no_speech") is False
    result.metadata.clear()
    with pytest.raises(RuntimeError, match="ASRNoSpeechEvidence"):
        validate_result(result, engine="whisper", expect="no_speech")


def test_local_asr_command_requires_existing_audio_and_installed_models(tmp_path: Path) -> None:
    command = [sys.executable, "scripts/check_local_asr.py", "--audio", str(tmp_path / "missing.wav")]
    result = subprocess.run(
        [*command, "--models-dir", str(tmp_path)], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 2 and "existing nonempty local file" in result.stderr
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"not yet decoded")
    result = subprocess.run(
        [sys.executable, "scripts/check_local_asr.py", "--audio", str(audio), "--models-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2 and "prepare the Engine Pack first" in result.stderr
