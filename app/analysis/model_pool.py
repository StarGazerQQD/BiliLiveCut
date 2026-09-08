"""真实 ASR 推理使用的模型池：独占实例、角色并发和安全空闲回收。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition

from app.core.config import Settings, settings
from app.core.runtime_settings import active_settings_views, effective_settings, process_settings


@dataclass
class _Entry:
    role: str
    identity: str
    fingerprint: tuple[tuple[str, object], ...]
    model: object | None = None
    active: bool = True
    last_used: float = 0.0
    loaded_at: float | None = None
    load_duration: float | None = None
    device: str = "cpu"
    keep_loaded: bool = False


def _fingerprint(config: Settings | None = None) -> tuple[tuple[str, object], ...]:
    config = config if config is not None else effective_settings()
    keys = {
        "whisper_model",
        "whisper_compute_type",
        "whisper_device",
        "asr_vad_max_segment_s",
        "asr_primary",
        "asr_sensevoice",
        "asr_sensevoice_enabled",
        "asr_funasr_review",
        "asr_fallback_whisper",
    }
    keys.update(f"asr_{role}_device" for role in ("primary", "auxiliary", "review", "fallback"))
    return tuple((key, value) for key, value in config.model_dump().items() if key in keys)


class ModelPool:
    """模型由池唯一持有，每次推理独占一个实例直到惰性输出消费结束。"""

    def __init__(self) -> None:
        self._condition = Condition()
        self._entries: list[_Entry] = []

    @contextmanager
    def use(self, role: str, identity: str, loader: Callable[[], object]) -> Iterator[object]:
        """获取推理许可；降并发不取消正在执行的任务，异常也会释放名额。"""
        fingerprint = _fingerprint()
        with self._condition:
            while sum(entry.active for entry in self._entries if entry.role == role) >= getattr(
                process_settings(), f"asr_{role}_max_concurrency"
            ):
                self._condition.wait(timeout=0.5)
            entry = next(
                (
                    item
                    for item in self._entries
                    if not item.active
                    and item.role == role
                    and item.identity == identity
                    and item.fingerprint == fingerprint
                ),
                None,
            )
            if entry is None:
                entry = _Entry(
                    role,
                    identity,
                    fingerprint,
                    device=getattr(settings, f"asr_{role}_device"),
                    keep_loaded=getattr(settings, f"asr_{role}_keep_loaded"),
                )
                self._entries.append(entry)
            else:
                entry.active = True
        try:
            if entry.model is None:
                started = time.monotonic()
                model = loader()
                with self._condition:
                    entry.model = model
                    entry.loaded_at = time.time()
                    entry.load_duration = time.monotonic() - started
            yield entry.model
        finally:
            with self._condition:
                entry.active = False
                entry.last_used = time.monotonic()
                if entry.model is None:
                    self._entries.remove(entry)
                self._condition.notify_all()
            self.cleanup_idle()

    def cleanup_idle(self, *, force: bool = False) -> int:
        """只移除无使用者的退役或超时实例，不调用活动模型的卸载方法。"""
        now = time.monotonic()
        live_fingerprints = {
            _fingerprint(process_settings()),
            *(_fingerprint(config) for config in active_settings_views()),
        }
        timeout = process_settings().asr_model_idle_unload_seconds
        with self._condition:
            removed = [
                entry
                for entry in self._entries
                if not entry.active
                and (
                    force
                    or entry.fingerprint not in live_fingerprints
                    or (
                        not getattr(process_settings(), f"asr_{entry.role}_keep_loaded")
                        and timeout > 0
                        and now - entry.last_used >= timeout
                    )
                )
            ]
            self._entries = [entry for entry in self._entries if entry not in removed]
        return len(removed)

    def infos(self) -> list[dict[str, object]]:
        """返回实际加载和推理状态，不包括文件内容或凭据。"""
        with self._condition:
            return [
                {
                    "key": entry.role,
                    "model_id": entry.identity,
                    "device": entry.device,
                    "is_loaded": entry.model is not None,
                    "loaded_at": entry.loaded_at,
                    "last_used_at": time.time() - max(0, time.monotonic() - entry.last_used)
                    if entry.last_used
                    else None,
                    "load_duration": entry.load_duration,
                    "gpu_memory_mb": None,
                    "keep_loaded": getattr(process_settings(), f"asr_{entry.role}_keep_loaded"),
                    "revision": None,
                    "active_users": int(entry.active),
                }
                for entry in self._entries
            ]


model_pool = ModelPool()


def preload_models() -> None:
    """预加载当前启用链路到共享池，加载失败交给后台任务记录。"""
    from app.analysis.transcription.backends import FasterWhisperBackend, FunASRBackend
    from app.core.runtime_settings import settings_scope

    with settings_scope(fresh=True):
        backend = FunASRBackend()
        loads: list[tuple[str, str, Callable[[], object]]] = []
        if settings.asr_primary == "whisper":
            whisper = FasterWhisperBackend()
            loads.append(("fallback", whisper.model_identity, whisper._load_model))
        else:
            if settings.asr_primary in {"funasr", "funasr_nano", "nano"}:
                loads.append(("primary", backend.nano_identity, lambda: backend._load_funasr(for_primary=True)))
                loads.append(("primary", backend.primary_identity, backend._load_primary))
            else:
                loads.append(("primary", backend.primary_identity, backend._load_primary))
            if settings.asr_sensevoice and settings.asr_sensevoice_enabled:
                loads.append(("auxiliary", backend.sensevoice_identity, backend._load_sensevoice))
            if settings.asr_funasr_review and settings.asr_primary == "paraformer":
                loads.append(("review", backend.nano_identity, backend._load_funasr))
            if settings.asr_fallback_whisper:
                whisper = FasterWhisperBackend()
                loads.append(("fallback", whisper.model_identity, whisper._load_model))
        from loguru import logger

        from app.pipeline.lifecycle import shutdown_event

        for role, identity, loader in loads:
            if shutdown_event.is_set():
                break
            try:
                with model_pool.use(role, identity, loader):
                    pass
            except (RuntimeError, OSError, ValueError, ImportError) as exc:
                logger.warning("ASR 预加载失败 role={} model={} error={}", role, identity, type(exc).__name__)
