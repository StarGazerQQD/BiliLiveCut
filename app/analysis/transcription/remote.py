"""使用 OpenAI 兼容的文件转写接口；音频与返回数据均有界。"""

from __future__ import annotations

import hashlib
import io
import time
import wave
from bisect import bisect_left
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analysis import asr_metrics
from app.analysis.transcription.audio_normalization import inspect_pcm_audio, normalized_asr_audio_window
from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult, Word
from app.analysis.transcription.progress import report_progress
from app.core.config import settings

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_RESPONSE_BYTES = 8 * 1024 * 1024


class RemoteTranscriptionError(RuntimeError):
    """远程请求或返回契约失败，不携带响应正文和请求凭据。"""


class _TimedText(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, hide_input_in_errors=True, strict=True)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(default="", max_length=100000)
    word: str = Field(default="", max_length=1000)


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, hide_input_in_errors=True, strict=True)
    text: str = Field(max_length=100000)
    language: str | None = Field(default=None, max_length=100)
    segments: list[_TimedText] = Field(default_factory=list, max_length=20000)
    words: list[_TimedText] = Field(default_factory=list, max_length=20000)


class RemoteChunk(BaseModel):
    """通过协议和音频校验的单块结果，尚不代表完整转写。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    index: int = Field(ge=1)
    offset: float = Field(ge=0)
    duration: float = Field(gt=0)
    audio_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_size: int = Field(gt=0, le=_MAX_RESPONSE_BYTES)
    response: _Response


@dataclass
class RemoteCheckpoint:
    """单次请求的已确认块和提交回调，不驻留在缓存模型实例中。"""

    chunks: dict[int, RemoteChunk]
    save: Callable[[RemoteChunk], None]


def _audio_chunks(path: str, chunk_s: int) -> Iterator[tuple[bytes, float, float]]:
    """逐块读取已验证的 PCM；每块最多 600 秒，低于 20 MB，附真实偏移与时长。"""
    with wave.open(path, "rb") as source:
        rate = source.getframerate()
        offset = 0
        while raw := source.readframes(chunk_s * rate):
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setparams(source.getparams())
                output.writeframes(raw)
            frames = len(raw) // 2
            yield buffer.getvalue(), offset / rate, frames / rate
            offset += frames


def _request_chunk(client: httpx.Client, audio: bytes, prompt: str | None) -> tuple[_Response, int]:
    fields: dict[str, str | list[str]] = {
        "model": settings.asr_remote_model,
        "response_format": settings.asr_remote_response_format,
    }
    if settings.asr_remote_language:
        fields["language"] = settings.asr_remote_language
    if prompt:
        fields["prompt"] = prompt[:4096]
    if settings.asr_remote_response_format == "verbose_json":
        fields["timestamp_granularities[]"] = ["word", "segment"]
    endpoint = settings.asr_remote_base_url.rstrip("/") + "/audio/transcriptions"
    try:
        with client.stream(
            "POST", endpoint, data=fields, files={"file": ("audio.wav", audio, "audio/wav")}
        ) as response:
            if response.status_code != 200:
                raise RemoteTranscriptionError(f"远程 ASR 请求失败，HTTP {response.status_code}")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise RemoteTranscriptionError("远程 ASR 返回了未请求的压缩响应")
            body = bytearray()
            for block in response.iter_raw(chunk_size=65536):
                body.extend(block)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise RemoteTranscriptionError("远程 ASR 响应超过 2 MiB 上限")
    except httpx.HTTPError as exc:
        raise RemoteTranscriptionError(f"远程 ASR 连接失败：{type(exc).__name__}") from exc
    try:
        return _Response.model_validate_json(body), len(body)
    except ValidationError as exc:
        # hide_input_in_errors 使异常链保留字段位置，不带服务端响应内容。
        raise RemoteTranscriptionError("远程 ASR 响应字段或 JSON 格式无效") from exc


def _chunk_has_speech(audio: bytes) -> bool:
    """在内存中对已验证的标准 PCM 调用本地 VAD，强制停机也不遗留临时文件。"""
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps

    with wave.open(io.BytesIO(audio), "rb") as source:
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    return bool(get_speech_timestamps(samples))


def _segments(response: _Response, offset: float, duration: float) -> list[ASRSegmentResult]:
    """只使用服务实际提供的时间戳，缺失时明确标记整块估时，不推断置信度。"""
    for collection, allow_overlap in ((response.segments, False), (response.words, True)):
        previous = -1.0
        for item in collection:
            if not previous <= item.start < min(item.end, duration) or item.end > duration + 0.05:
                raise RemoteTranscriptionError("远程 ASR 时间戳越界、逆序或为空")
            previous = item.start if allow_overlap else min(item.end, duration)
    words = [
        Word(item.word, offset + item.start, offset + min(item.end, duration))
        for item in response.words
        if item.word.strip()
    ]
    word_starts = [word.start for word in words]
    segments = [
        ASRSegmentResult(
            start=offset + item.start,
            end=offset + min(item.end, duration),
            text=item.text,
            language=response.language,
            words=words[bisect_left(word_starts, offset + item.start) : bisect_left(word_starts, offset + item.end)],
            metadata={"timestamp_source": "provider"},
        )
        for item in response.segments
        if item.text.strip()
    ]
    if segments:
        return segments
    if not response.text.strip():
        return []
    return [
        ASRSegmentResult(
            start=offset,
            end=offset + duration,
            text=response.text,
            words=words,
            language=response.language,
            metadata={"timestamp_source": "provider_words" if words else "estimated_chunk"},
        )
    ]


class OpenAICompatibleBackend:
    """远程主引擎；不隐式重试、不跟随重定向，也不预加载任何本地 ASR 模型。"""

    def transcribe(
        self,
        audio_path: str,
        initial_prompt: str | None = None,
        *,
        checkpoint: RemoteCheckpoint | None = None,
    ) -> ASRTranscriptResult:
        """上传标准化音频并合并分块；任一块失败即整体失败，不提交不完整转写。"""
        if not settings.asr_remote_base_url or not settings.asr_remote_model:
            raise ValueError("远程 ASR 需要配置服务基础地址和模型 ID")
        evidence = inspect_pcm_audio(audio_path)
        started = time.monotonic()
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if settings.asr_remote_api_key:
            headers["Authorization"] = f"Bearer {settings.asr_remote_api_key}"
        segments: list[ASRSegmentResult] = []
        texts: list[str] = []
        languages: set[str] = set()
        count = 0
        response_bytes = 0
        timeout = httpx.Timeout(settings.asr_remote_timeout_s, connect=min(10.0, settings.asr_remote_timeout_s))
        try:
            with httpx.Client(timeout=timeout, headers=headers, follow_redirects=False) as client:
                for audio, offset, duration in _audio_chunks(audio_path, settings.asr_remote_chunk_s):
                    count += 1
                    digest = hashlib.sha256(audio).hexdigest()
                    saved = checkpoint.chunks.get(count) if checkpoint else None
                    if saved is not None:
                        if (saved.audio_digest, saved.offset, saved.duration) != (digest, offset, duration):
                            raise RemoteTranscriptionError("远程 ASR 断点与实际音频块不一致")
                        response, size = saved.response, saved.response_size
                    else:
                        report_progress(
                            "remote_request",
                            backend="openai_compatible",
                            chunk=count,
                            offset_s=offset,
                            duration_s=duration,
                        )
                        response, size = _request_chunk(client, audio, initial_prompt)
                    response_bytes += size
                    if response_bytes > _MAX_TOTAL_RESPONSE_BYTES:
                        raise RemoteTranscriptionError("远程 ASR 本次任务累计响应超过 8 MiB 上限")
                    if not response.text.strip() and any(
                        item.text.strip() or item.word.strip() for item in response.segments + response.words
                    ):
                        raise RemoteTranscriptionError("远程 ASR 正文与时间戳文本不一致")
                    if not response.text.strip() and _chunk_has_speech(audio):
                        raise RemoteTranscriptionError("远程 ASR 对含语音的音频块返回空文本")
                    chunk_segments = _segments(response, offset, duration)
                    if checkpoint is not None and saved is None:
                        checkpoint.save(
                            RemoteChunk(
                                index=count,
                                offset=offset,
                                duration=duration,
                                audio_digest=digest,
                                response_size=size,
                                response=response,
                            )
                        )
                    segments.extend(chunk_segments)
                    if response.text.strip():
                        texts.append(response.text.strip())
                    if response.language:
                        languages.add(response.language)
                if checkpoint is not None and any(index > count for index in checkpoint.chunks):
                    raise RemoteTranscriptionError("远程 ASR 断点包含当前音频范围外的块")
        except (RemoteTranscriptionError, OSError, ValueError, wave.Error):
            asr_metrics.record_backend_call("openai_compatible", time.monotonic() - started, success=False)
            raise
        elapsed = time.monotonic() - started
        asr_metrics.record_backend_call("openai_compatible", elapsed, success=True)
        asr_metrics.record_rtf(elapsed / evidence.duration_s)
        text = "\n".join(texts)
        return ASRTranscriptResult(
            text=text,
            final_text=text,
            base_text=text,
            result_type="speech" if text else "no_speech",
            final_text_source="primary" if text else "none",
            segments=segments,
            backend="openai_compatible",
            model_id=settings.asr_remote_model,
            model_source="remote",
            loaded_from=settings.asr_remote_base_url,
            inference_duration=elapsed,
            audio_duration=evidence.duration_s,
            real_time_factor=elapsed / evidence.duration_s,
            language=next(iter(languages)) if len(languages) == 1 else None,
            primary_backend="openai_compatible",
            primary_status="success",
            metadata={
                "audio_evidence": asdict(evidence),
                "remote_chunks": count,
                **({"vad_speech_segments": 0} if not text else {}),
            },
        )

    def transcribe_segment(self, audio_path: str, start: float, end: float) -> ASRTranscriptResult:
        """转写指定窗口，返回相对该窗口的时间轴。"""
        with normalized_asr_audio_window(audio_path, start, end) as window:
            return self.transcribe(window)
