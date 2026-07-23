"""主程序在线评分适配、显式回退和可观测性。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

import numpy as np
from sqlmodel import Session

from app.analysis.audio import AudioFeatures
from app.analysis.highlight_ml.context import load_feature_context
from app.analysis.highlight_ml.features import extract_feature_record
from app.analysis.highlight_ml.registry import ModelRegistry
from app.analysis.highlight_ml.runtime import HotReloadingPredictor
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
from app.analysis.highlight_ml.types import AudioSnapshot
from app.core.config import settings
from app.db.models import SystemLog
from app.db.session import get_session

ScoringMode = Literal["off", "shadow", "champion"]
RoomScoringMode = Literal["inherit", "off", "shadow", "champion"]

_logger = logging.getLogger(__name__)
_VALID_MODES = frozenset({"off", "shadow", "champion"})


@dataclass(frozen=True, slots=True)
class OnlinePrediction:
    """一次在线推理的完整、可序列化结果。"""

    requested_mode: ScoringMode
    effective_mode: ScoringMode
    champion_version: int | None = None
    champion_probability: float | None = None
    champion_threshold: float | None = None
    shadow_version: int | None = None
    shadow_probability: float | None = None
    schema_version: str | None = None
    schema_fingerprint: str | None = None
    feature_values: dict[str, float | None] | None = None
    error: str | None = None

    @property
    def uses_champion(self) -> bool:
        """仅在 Champion 模式且推理成功时改变主评分。"""
        return self.effective_mode == "champion" and self.champion_probability is not None

    @property
    def attempted(self) -> bool:
        """返回本次是否尝试加载模型。"""
        return self.requested_mode != "off"

    def to_dict(self) -> dict[str, object]:
        """返回适合日志与候选特征留存的 JSON 对象。"""
        return asdict(self)


def resolve_scoring_mode(room_config_json: str | None) -> ScoringMode:
    """解析房间覆盖；缺失或 ``inherit`` 时使用全局配置。"""
    override: object = "inherit"
    if room_config_json:
        try:
            payload = json.loads(room_config_json)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            override = payload.get("highlight_ml_mode", "inherit")
    if override == "inherit":
        return settings.highlight_ml_mode
    if isinstance(override, str) and override in _VALID_MODES:
        return cast(ScoringMode, override)
    _logger.warning("invalid_highlight_ml_room_mode mode=%r fallback=global", override)
    return settings.highlight_ml_mode


def _audio_snapshot(features: AudioFeatures) -> AudioSnapshot | None:
    """复用规则评分已经解码的音频，不产生第二次 FFmpeg 调用。"""
    if features.rms.size == 0:
        return None
    duration = max(features.duration_s, 1e-9)
    silence_duration = sum(max(0.0, end - start) for start, end in features.silences)
    return AudioSnapshot(
        rms_peak=float(np.max(features.rms)),
        rms_median=float(np.median(features.rms)),
        rms_std=float(np.std(features.rms)),
        prominence=features.volume_score(),
        silence_ratio=float(np.clip(silence_duration / duration, 0.0, 1.0)),
    )


@lru_cache(maxsize=4)
def _cached_predictor(registry_root: str) -> HotReloadingPredictor:
    """按注册表根目录复用线程安全热加载器。"""
    registry = ModelRegistry(Path(registry_root), schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    return HotReloadingPredictor(registry)


def clear_online_predictor_cache() -> None:
    """清空进程内热加载器缓存，供配置重载与测试使用。"""
    _cached_predictor.cache_clear()


def predict_online(
    segment_id: int,
    *,
    audio_features: AudioFeatures,
    room_config_json: str | None,
) -> OnlinePrediction:
    """提取训练一致特征并执行 Champion/Shadow 推理。

    所有模型、Schema、注册表或可选依赖错误都会转为显式规则回退，
    不得中断直播分析流水线。
    """
    requested_mode = resolve_scoring_mode(room_config_json)
    if requested_mode == "off":
        return OnlinePrediction(requested_mode="off", effective_mode="off")
    try:
        snapshot = _audio_snapshot(audio_features)
        with get_session() as db:
            context = load_feature_context(
                db,
                segment_id,
                audio_loader=lambda _path: snapshot,
            )
        record = extract_feature_record(context)
        vector = record.vector(DEFAULT_FEATURE_SCHEMA)
        pair = _cached_predictor(str(Path(settings.highlight_ml_registry_root).resolve())).predict(vector)
        if pair.schema_fingerprint != record.schema_fingerprint:
            raise ValueError("在线特征 Schema 与 Champion 产物不一致")
        probabilities = (pair.champion_probability, pair.shadow_probability)
        if any(value is not None and (not np.isfinite(value) or not 0.0 <= value <= 1.0) for value in probabilities):
            raise ValueError("模型返回了无效概率")
        return OnlinePrediction(
            requested_mode=requested_mode,
            effective_mode=requested_mode,
            champion_version=pair.champion_version,
            champion_probability=pair.champion_probability,
            champion_threshold=pair.champion_threshold,
            shadow_version=pair.shadow_version,
            shadow_probability=pair.shadow_probability,
            schema_version=pair.schema_version,
            schema_fingerprint=pair.schema_fingerprint,
            feature_values=dict(record.values),
        )
    except (ImportError, OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        _logger.warning(
            "highlight_ml_fallback segment=%s requested_mode=%s error=%s",
            segment_id,
            requested_mode,
            exc,
        )
        return OnlinePrediction(
            requested_mode=requested_mode,
            effective_mode="off",
            error=f"{type(exc).__name__}: {exc}",
        )


def effective_primary_score(rule_score: float, prediction: OnlinePrediction) -> float:
    """返回用于筛选和 LLM 融合的主评分。"""
    if prediction.uses_champion:
        assert prediction.champion_probability is not None
        return prediction.champion_probability
    return rule_score


def merge_prediction_metadata(features_json: str, prediction: OnlinePrediction) -> str:
    """把模型版本、概率与回退原因合入候选特征 JSON。"""
    try:
        payload = json.loads(features_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["highlight_ml"] = prediction.to_dict()
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def add_prediction_log(
    db: Session,
    *,
    prediction: OnlinePrediction,
    segment_id: int,
    session_id: int | None,
    room_id: int | None,
    rule_score: float,
    final_score: float | None,
) -> None:
    """在调用方事务中追加结构化模型预测/回退日志。"""
    if not prediction.attempted:
        return
    context = prediction.to_dict()
    context.update(
        {
            "segment_id": segment_id,
            "session_id": session_id,
            "rule_score": rule_score,
            "final_score": final_score,
        }
    )
    db.add(
        SystemLog(
            level="WARNING" if prediction.error else "INFO",
            module="highlight_ml",
            room_id=room_id,
            event="highlight_ml_fallback" if prediction.error else "highlight_ml_prediction",
            message=(
                "高光模型不可用，已回退规则评分"
                if prediction.error
                else f"高光模型 {prediction.requested_mode} 预测完成"
            ),
            context_json=json.dumps(context, ensure_ascii=False, allow_nan=False),
        )
    )


def get_online_status() -> dict[str, object]:
    """返回全局模式与注册表 Champion/Shadow 状态。"""
    root = Path(settings.highlight_ml_registry_root).resolve()
    base: dict[str, object] = {
        "mode": settings.highlight_ml_mode,
        "registry_root": str(root),
        "schema_version": DEFAULT_FEATURE_SCHEMA.version,
        "schema_fingerprint": DEFAULT_FEATURE_SCHEMA.fingerprint,
        "available": False,
        "generation": 0,
        "champion_version": None,
        "shadow_version": None,
        "champion_model_type": None,
        "champion_threshold": None,
        "error": None,
        "versions": [],
    }
    try:
        registry = ModelRegistry(root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
        snapshot = registry.snapshot()
        artifact = registry.load_artifact(snapshot.champion_version) if snapshot.champion_version is not None else None
        base.update(
            {
                "available": snapshot.champion_version is not None,
                "generation": snapshot.generation,
                "champion_version": snapshot.champion_version,
                "shadow_version": snapshot.shadow_version,
                "champion_model_type": artifact.model_type if artifact is not None else None,
                "champion_threshold": artifact.threshold if artifact is not None else None,
                "versions": [item.to_dict() for item in snapshot.versions],
            }
        )
    except (OSError, RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base
