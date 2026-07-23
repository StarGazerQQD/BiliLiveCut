"""感知注册表 generation 的 Champion/Shadow 热加载推理。"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from app.analysis.highlight_ml.models import ArtifactPredictor
from app.analysis.highlight_ml.registry import ModelRegistry


@dataclass(frozen=True, slots=True)
class PredictionPair:
    """同一输入的 Champion 与可选 Shadow 概率。"""

    champion_version: int
    champion_probability: float
    champion_threshold: float
    schema_version: str
    schema_fingerprint: str
    shadow_version: int | None
    shadow_probability: float | None


class HotReloadingPredictor:
    """在 generation 变化时线程安全刷新真实模型产物。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry
        self._lock = threading.RLock()
        self._generation = -1
        self._champion_version: int | None = None
        self._champion_threshold: float | None = None
        self._schema_version: str | None = None
        self._schema_fingerprint: str | None = None
        self._shadow_version: int | None = None
        self._champion: ArtifactPredictor | None = None
        self._shadow: ArtifactPredictor | None = None

    def refresh(self, *, force: bool = False) -> bool:
        """必要时刷新模型，返回本次是否发生替换。"""
        snapshot = self.registry.snapshot()
        if not force and snapshot.generation == self._generation:
            return False
        if snapshot.champion_version is None:
            raise RuntimeError("模型注册表尚无 Champion")
        champion = ArtifactPredictor(self.registry.load_artifact(snapshot.champion_version))
        shadow = (
            ArtifactPredictor(self.registry.load_artifact(snapshot.shadow_version))
            if snapshot.shadow_version is not None
            else None
        )
        with self._lock:
            self._champion = champion
            self._shadow = shadow
            self._champion_version = snapshot.champion_version
            self._champion_threshold = champion.artifact.threshold
            self._schema_version = champion.artifact.schema_version
            self._schema_fingerprint = champion.artifact.schema_fingerprint
            self._shadow_version = snapshot.shadow_version
            self._generation = snapshot.generation
        return True

    def predict(self, vector: np.ndarray) -> PredictionPair:
        """对单条 1D 特征向量执行双轨推理。"""
        self.refresh()
        matrix = np.asarray(vector, dtype=np.float64)
        if matrix.ndim != 1:
            raise ValueError("热加载推理器只接受单条一维特征")
        matrix = matrix.reshape(1, -1)
        with self._lock:
            champion = self._champion
            champion_version = self._champion_version
            champion_threshold = self._champion_threshold
            schema_version = self._schema_version
            schema_fingerprint = self._schema_fingerprint
            shadow = self._shadow
            shadow_version = self._shadow_version
        if (
            champion is None
            or champion_version is None
            or champion_threshold is None
            or schema_version is None
            or schema_fingerprint is None
        ):
            raise RuntimeError("Champion 未加载")
        champion_probability = float(champion.predict_proba(matrix)[0])
        shadow_probability = float(shadow.predict_proba(matrix)[0]) if shadow is not None else None
        return PredictionPair(
            champion_version=champion_version,
            champion_probability=champion_probability,
            champion_threshold=champion_threshold,
            schema_version=schema_version,
            schema_fingerprint=schema_fingerprint,
            shadow_version=shadow_version,
            shadow_probability=shadow_probability,
        )
