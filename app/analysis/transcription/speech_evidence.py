"""用实际采样与 Silero VAD 区分无语音和模型空输出故障。"""

from __future__ import annotations

import math
from dataclasses import asdict

from app.analysis.transcription.audio_normalization import inspect_pcm_audio
from app.analysis.transcription.models import ASRTranscriptResult
from app.analysis.transcription.progress import report_progress


def no_speech_result(audio_path: str, *, backend: str) -> ASRTranscriptResult | None:
    """仅在有效 PCM 经 VAD 确认没有语音时返回 no_speech，异常原样上抛。"""
    import numpy as np
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps

    evidence = inspect_pcm_audio(audio_path)
    report_progress("vad", backend=backend, duration_s=evidence.duration_s, samples=evidence.sample_count)
    audio = decode_audio(audio_path, sampling_rate=evidence.sample_rate)
    if len(audio) != evidence.sample_count or not np.isfinite(audio).all():
        raise ValueError("ASR VAD 解码样本与已校验 WAV 不一致")
    spans = get_speech_timestamps(audio)
    if spans:
        return None
    return ASRTranscriptResult(
        text="",
        result_type="no_speech",
        backend=backend,
        audio_duration=evidence.duration_s,
        final_text_source="none",
        primary_status="success",
        metadata={"audio_evidence": asdict(evidence), "vad_speech_segments": 0},
    )


def require_final_result(result: ASRTranscriptResult) -> ASRTranscriptResult:
    """没有显式无语音证据的空结果不得作为成功落库。"""
    if result.result_type == "no_speech" and not has_no_speech_evidence(result):
        raise RuntimeError("ASRNoSpeechEvidence: 无语音结果缺少有效 PCM 与 VAD 证据")
    if result.result_type != "no_speech" and not (result.final_text or result.text).strip():
        raise RuntimeError(
            f"ASREmptyOutput: backend={result.backend} "
            f"primary_error={result.primary_error_type}: {result.primary_error_message}"
        )
    return result


def has_no_speech_evidence(result: ASRTranscriptResult) -> bool:
    """合法无语音必须来自校验过的 PCM 和无语音 VAD 结果。"""
    evidence = result.metadata.get("audio_evidence")
    spans = result.metadata.get("vad_speech_segments")
    if result.result_type != "no_speech" or type(spans) is not int or spans != 0 or not isinstance(evidence, dict):
        return False
    samples, rate = evidence.get("sample_count"), evidence.get("sample_rate")
    duration, rms, peak = evidence.get("duration_s"), evidence.get("rms"), evidence.get("peak")
    return (
        type(samples) is int
        and samples > 0
        and type(rate) is int
        and rate == 16000
        and all(type(value) in (int, float) and math.isfinite(value) for value in (duration, rms, peak))
        and math.isclose(duration, samples / rate)
        and 0 <= rms <= peak <= 1
    )
