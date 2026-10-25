"""父进程的 ASR 门面：可取消预处理、音频预算和纯模型子进程请求。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Protocol

from app.analysis.transcription.audio_normalization import (
    inspect_pcm_audio,
    normalized_asr_audio,
    normalized_asr_audio_window,
)
from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult, EmotionEvent, Word
from app.analysis.transcription.process_pool import ASRProcessPool, get_process_pool
from app.core.runtime_settings import effective_settings
from app.models.configuration import asr_call_authorizer, asr_worker_settings
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease


class ASRExecutor(Protocol):
    """父流程只依赖统一的全片段及窗口推理接口。"""

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
        """识别整个音频并返回带终态分类的结果。"""
        ...

    def transcribe_window(
        self, audio_path: str, start_s: float, end_s: float, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        """识别指定时间窗口，时间戳相对于窗口起点。"""
        ...


def _noop() -> None:
    pass


def _settings_payload() -> dict[str, object]:
    """只传 ASR 配置和工具路径，不向子进程传播数据库或 LLM 凭据。"""
    return asr_worker_settings()


class IsolatedPipeline:
    """任务取消与租约失效均由父进程监管，不依赖原生库协作退出。"""

    def __init__(
        self,
        *,
        lease: TaskLease | None = None,
        context: dict[str, object] | None = None,
        pool: ASRProcessPool | None = None,
        check_cancelled: Callable[[], None] = _noop,
    ) -> None:
        self._lease = lease
        self._context = dict(context or {})
        if lease is not None:
            self._context.update(asdict(lease))
        self._pool = pool if pool is not None else get_process_pool()
        self._external_check = check_cancelled
        self._last_check = 0.0

    def check_cancelled(self) -> None:
        """周期读取真实租约；提交时仍需在写事务内再次校验。"""
        self._external_check()
        self._pool.check_shutdown()
        if self._lease is None or time.monotonic() - self._last_check < 0.5:
            return
        from app.db.session import get_session

        with get_session() as db:
            if not still_owns_lease(db, self._lease):
                raise LeaseLostError(
                    f"ASR cancelled or lease changed task={self._lease.task_id} lease={self._lease.lease_token}"
                )
        self._last_check = time.monotonic()

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
        """标准化整个媒体；临时 WAV 在模型进程确认完成或退出后才清理。"""
        self.check_cancelled()
        with (
            self._pool.operation(self._context),
            normalized_asr_audio(audio_path, check_cancelled=self.check_cancelled) as normalized,
        ):
            return self._infer(normalized, initial_prompt)

    def transcribe_window(
        self, audio_path: str, start_s: float, end_s: float, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        """热点窗口与后台转写共用同一个父进程资源配额。"""
        self.check_cancelled()
        with (
            self._pool.operation(self._context),
            normalized_asr_audio_window(audio_path, start_s, end_s, check_cancelled=self.check_cancelled) as normalized,
        ):
            return self._infer(normalized, initial_prompt)

    def _infer(self, audio_path: str, initial_prompt: str | None) -> ASRTranscriptResult:
        evidence = inspect_pcm_audio(audio_path, check_cancelled=self.check_cancelled)
        config = effective_settings()
        budget = min(
            config.asr_inference_max_timeout_s,
            config.asr_inference_base_timeout_s + evidence.duration_s * config.asr_inference_timeout_rtf,
        )
        payload = _settings_payload()
        response = self._pool.execute(
            {
                "audio_path": audio_path,
                "initial_prompt": initial_prompt,
                "settings": payload,
                "context": self._context,
                "audio_evidence": asdict(evidence),
            },
            timeout_s=budget,
            check_cancelled=self.check_cancelled,
            authorize_remote=asr_call_authorizer(payload) if payload["asr_primary"] == "openai_compatible" else None,
        )
        raw = response["result"]
        # JSON 类型仅位于 IPC 边界，业务层恢复为既有强类型结果。
        raw["segments"] = [
            ASRSegmentResult(**{**segment, "words": [Word(**word) for word in segment["words"]]})
            for segment in raw["segments"]
        ]
        raw["emotions"] = [EmotionEvent(**event) for event in raw["emotions"]]
        return ASRTranscriptResult(**raw)


def preload_isolated_models() -> None:
    """预加载也在持久模型槽内运行，父进程不复制 GPU 模型。"""
    config = effective_settings()
    get_process_pool().execute(
        {"operation": "preload", "context": {"operation": "preload"}, "settings": _settings_payload()},
        timeout_s=config.asr_inference_base_timeout_s,
        check_cancelled=_noop,
    )
