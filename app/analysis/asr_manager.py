"""ASR 模型生命周期管理 (V0.1.12.2)。

提供:
- 统一加载/卸载/状态查询接口
- 并发加载锁, 防止重复初始化
- 空闲超时自动卸载
- 优雅关闭资源释放
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings


@dataclass
class ModelInfo:
    """单个模型的状态快照。"""

    key: str
    model_id: str
    device: str
    is_loaded: bool = False
    loaded_at: float | None = None
    last_used_at: float | None = None
    load_duration: float | None = None
    gpu_memory_mb: float | None = None
    keep_loaded: bool = False
    revision: str | None = None


class ASRModelManager:
    """统一的 ASR 模型管理器。

    用法::

        mgr = ASRModelManager()
        await mgr.load("primary", loader_fn)
        await mgr.unload("auxiliary")
        info = mgr.info("primary")
    """

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._infos: dict[str, ModelInfo] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_access: dict[str, float] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {
            "primary": asyncio.Semaphore(settings.asr_primary_max_concurrency),
            "auxiliary": asyncio.Semaphore(settings.asr_auxiliary_max_concurrency),
            "review": asyncio.Semaphore(settings.asr_review_max_concurrency),
            "fallback": asyncio.Semaphore(settings.asr_fallback_max_concurrency),
        }

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def acquire(self, key: str) -> None:
        """获取模型并发许可。"""
        sem = self._semaphores.get(key)
        if sem is None:
            return
        await sem.acquire()

    def release(self, key: str) -> None:
        """释放模型并发许可。"""
        sem = self._semaphores.get(key)
        if sem is not None:
            sem.release()

    async def load(
        self,
        key: str,
        loader_fn,
        *,
        model_id: str = "",
        device: str = "cpu",
        keep_loaded: bool = False,
        revision: str | None = None,
    ) -> Any:
        """加载模型 (带锁, 避免并发重复加载)。

        :param key: 模型键 (primary/auxiliary/review/fallback)。
        :param loader_fn: 加载函数 async() -> model。
        :param model_id: 模型标识。
        :param device: 设备。
        :param keep_loaded: 是否常驻。
        :param revision: 模型 revision。
        :returns: 已加载的模型。
        """
        if key in self._models and self._models[key] is not None:
            self._last_access[key] = time.time()
            if key in self._infos:
                self._infos[key].last_used_at = time.time()
            return self._models[key]

        lock = self._get_lock(key)
        async with lock:
            # 双重检查: 可能在等待锁期间被其他协程加载
            if key in self._models and self._models[key] is not None:
                return self._models[key]

            logger.info("加载 ASR 模型 key={} id={} device={}", key, model_id, device)
            t0 = time.time()
            try:
                if asyncio.iscoroutinefunction(loader_fn):
                    model = await loader_fn()
                else:
                    # 同步函数在 executor 中运行, 避免阻塞事件循环
                    loop = asyncio.get_event_loop()
                    model = await loop.run_in_executor(None, loader_fn)
            except Exception:
                logger.exception("加载模型失败 key={}", key)
                raise

            elapsed = time.time() - t0
            self._models[key] = model
            self._last_access[key] = time.time()
            self._infos[key] = ModelInfo(
                key=key,
                model_id=model_id,
                device=device,
                is_loaded=True,
                loaded_at=time.time(),
                last_used_at=time.time(),
                load_duration=elapsed,
                keep_loaded=keep_loaded,
                revision=revision,
            )
            logger.info("模型加载完成 key={} 耗时 {:.1f}s", key, elapsed)
            return model

    async def unload(self, key: str) -> None:
        """卸载模型并释放资源。"""
        if key in self._models:
            model = self._models[key]
            logger.info("卸载 ASR 模型 key={}", key)
            # 尝试调用 del / cleanup
            if hasattr(model, "model") and hasattr(model.model, "unload_model"):
                try:
                    model.model.unload_model()
                except Exception:
                    pass
            del self._models[key]
            if key in self._infos:
                self._infos[key].is_loaded = False
            if key in self._last_access:
                del self._last_access[key]

    def is_loaded(self, key: str) -> bool:
        """查询模型是否已加载。"""
        return key in self._models and self._models[key] is not None

    def info(self, key: str) -> ModelInfo | None:
        """获取模型状态信息。"""
        return self._infos.get(key)

    def all_infos(self) -> list[ModelInfo]:
        """获取所有模型状态。"""
        return [
            (
                info
                if info.is_loaded
                else ModelInfo(
                    key=info.key,
                    model_id=info.model_id,
                    device=info.device,
                    is_loaded=False,
                )
            )
            for info in self._infos.values()
        ]

    def get_model(self, key: str) -> Any | None:
        """获取已加载的模型(不更新最后使用时间)。"""
        return self._models.get(key)

    def touch(self, key: str) -> None:
        """更新模型最后使用时间。"""
        self._last_access[key] = time.time()
        if key in self._infos:
            self._infos[key].last_used_at = time.time()

    async def check_idle_unload(self) -> int:
        """检查并卸载空闲超时的模型。返回卸载数量。"""
        threshold = settings.asr_model_idle_unload_seconds
        if threshold <= 0:
            return 0
        now = time.time()
        unloaded = 0
        for key in list(self._models.keys()):
            info = self._infos.get(key)
            if info and info.keep_loaded:
                continue
            last = self._last_access.get(key, 0)
            if now - last > threshold:
                await self.unload(key)
                unloaded += 1
                logger.info("空闲超时卸载模型 key={} idle={:.0f}s", key, now - last)
        return unloaded

    async def warmup(self, key: str) -> bool:
        """预热模型 (触发一次推理以预分配显存)。"""
        model = self._models.get(key)
        if model is None:
            logger.warning("模型未加载, 无法预热 key={}", key)
            return False
        if not hasattr(model, "generate"):
            return False
        try:
            # 生成静音白噪声音频做最小推理
            import tempfile

            import numpy as np

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            sample_rate = 16000
            duration = 1.0
            samples = np.zeros(int(sample_rate * duration), dtype=np.int16)
            import wave

            with wave.open(tmp.name, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(samples.tobytes())

            logger.info("预热模型 key={} ...", key)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: model.generate(input=tmp.name),
            )
            import os

            os.unlink(tmp.name)
            logger.info("模型预热完成 key={}", key)
            return True
        except Exception as exc:
            logger.warning("模型预热失败 key={}: {}", key, exc)
            return False

    async def shutdown(self) -> None:
        """优雅关闭: 卸载所有模型。"""
        for key in list(self._models.keys()):
            await self.unload(key)
        logger.info("ASR 模型管理器已关闭")


# 全局单例
_asr_manager: ASRModelManager | None = None
_lock = threading.Lock()


def get_asr_manager() -> ASRModelManager:
    """获取进程级 ASRModelManager 单例。"""
    global _asr_manager
    if _asr_manager is None:
        with _lock:
            if _asr_manager is None:
                _asr_manager = ASRModelManager()
    return _asr_manager
