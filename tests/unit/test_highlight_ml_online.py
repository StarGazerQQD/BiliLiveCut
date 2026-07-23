"""在线高光模型适配与安全回退测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.analysis.highlight_ml import online
from app.analysis.highlight_ml.online import OnlinePrediction
from app.db.models import SystemLog
from app.db.session import get_session

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _audio_features() -> AudioFeatures:
    return AudioFeatures(
        sample_rate=16_000,
        hop_s=0.1,
        times=np.asarray([0.0, 0.1]),
        rms=np.asarray([0.25, 1.0]),
        duration_s=1.0,
        silences=[(0.0, 0.2)],
    )


def test_room_mode_inherits_and_validates(monkeypatch: MonkeyPatch) -> None:
    """房间模式可覆盖全局；非法值明确回退全局。"""
    monkeypatch.setattr(online.settings, "highlight_ml_mode", "shadow")
    assert online.resolve_scoring_mode(None) == "shadow"
    assert online.resolve_scoring_mode('{"highlight_ml_mode":"champion"}') == "champion"
    assert online.resolve_scoring_mode('{"highlight_ml_mode":"invalid"}') == "shadow"
    assert online.resolve_scoring_mode("{bad") == "shadow"


def test_effective_primary_score_only_uses_successful_champion() -> None:
    """Shadow 与失败回退不能改变规则主分。"""
    shadow = OnlinePrediction(
        requested_mode="shadow",
        effective_mode="shadow",
        champion_probability=0.91,
    )
    champion = OnlinePrediction(
        requested_mode="champion",
        effective_mode="champion",
        champion_probability=0.91,
    )
    fallback = OnlinePrediction(requested_mode="champion", effective_mode="off", error="missing")
    assert online.effective_primary_score(0.2, shadow) == 0.2
    assert online.effective_primary_score(0.2, champion) == 0.91
    assert online.effective_primary_score(0.2, fallback) == 0.2


def test_predict_online_reuses_audio_and_returns_versions(monkeypatch: MonkeyPatch) -> None:
    """在线推理复用传入音频聚合，并保留双轨版本与概率。"""
    monkeypatch.setattr(online.settings, "highlight_ml_mode", "shadow")
    seen: dict[str, object] = {}

    @contextmanager
    def fake_session() -> Iterator[object]:
        yield object()

    def fake_load_context(db: object, segment_id: int, *, audio_loader: object) -> object:
        del db
        seen["segment_id"] = segment_id
        seen["audio"] = audio_loader("unused")  # type: ignore[operator]
        return object()

    class FakeRecord:
        schema_fingerprint = online.DEFAULT_FEATURE_SCHEMA.fingerprint
        values = {spec.name: 0.0 for spec in online.DEFAULT_FEATURE_SCHEMA.specs}

        def vector(self, schema: object) -> np.ndarray:
            del schema
            return np.zeros(len(online.DEFAULT_FEATURE_SCHEMA.feature_names))

    class FakePredictor:
        def predict(self, vector: np.ndarray) -> SimpleNamespace:
            assert vector.shape == (len(online.DEFAULT_FEATURE_SCHEMA.feature_names),)
            return SimpleNamespace(
                champion_version=4,
                champion_probability=0.8,
                champion_threshold=0.61,
                shadow_version=5,
                shadow_probability=0.75,
                schema_version=online.DEFAULT_FEATURE_SCHEMA.version,
                schema_fingerprint=online.DEFAULT_FEATURE_SCHEMA.fingerprint,
            )

    monkeypatch.setattr(online, "get_session", fake_session)
    monkeypatch.setattr(online, "load_feature_context", fake_load_context)
    monkeypatch.setattr(online, "extract_feature_record", lambda _context: FakeRecord())
    monkeypatch.setattr(online, "_cached_predictor", lambda _root: FakePredictor())

    result = online.predict_online(17, audio_features=_audio_features(), room_config_json=None)
    assert result.effective_mode == "shadow"
    assert result.champion_version == 4
    assert result.shadow_version == 5
    assert result.shadow_probability == pytest.approx(0.75)
    assert seen["segment_id"] == 17
    snapshot = seen["audio"]
    assert snapshot is not None
    assert snapshot.rms_peak == pytest.approx(1.0)  # type: ignore[union-attr]


def test_predict_online_schema_error_falls_back(monkeypatch: MonkeyPatch) -> None:
    """注册表或 Schema 错误转为显式规则回退。"""
    monkeypatch.setattr(online.settings, "highlight_ml_mode", "champion")

    @contextmanager
    def fake_session() -> Iterator[object]:
        yield object()

    class BadRecord:
        schema_fingerprint = "0" * 64

        def vector(self, schema: object) -> np.ndarray:
            del schema
            return np.zeros(len(online.DEFAULT_FEATURE_SCHEMA.feature_names))

    class BadPredictor:
        def predict(self, vector: np.ndarray) -> SimpleNamespace:
            del vector
            return SimpleNamespace(
                champion_version=1,
                champion_probability=0.9,
                champion_threshold=0.5,
                shadow_version=None,
                shadow_probability=None,
                schema_version="old",
                schema_fingerprint="1" * 64,
            )

    monkeypatch.setattr(online, "get_session", fake_session)
    monkeypatch.setattr(online, "load_feature_context", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(online, "extract_feature_record", lambda _context: BadRecord())
    monkeypatch.setattr(online, "_cached_predictor", lambda _root: BadPredictor())

    result = online.predict_online(2, audio_features=_audio_features(), room_config_json=None)
    assert result.requested_mode == "champion"
    assert result.effective_mode == "off"
    assert result.error is not None and "Schema" in result.error


def test_prediction_metadata_and_database_log(temp_db: None) -> None:
    """模型审计信息同时可写入候选 JSON 与现有 SystemLog。"""
    prediction = OnlinePrediction(
        requested_mode="shadow",
        effective_mode="shadow",
        champion_version=2,
        champion_probability=0.7,
        shadow_version=3,
        shadow_probability=0.6,
    )
    merged = online.merge_prediction_metadata('{"features":{"volume":0.4}}', prediction)
    assert '"champion_version": 2' in merged

    with get_session() as db:
        online.add_prediction_log(
            db,
            prediction=prediction,
            segment_id=10,
            session_id=20,
            room_id=None,
            rule_score=0.4,
            final_score=0.4,
        )
    with get_session() as db:
        row = db.exec(select(SystemLog).where(SystemLog.module == "highlight_ml")).one()
    assert row.event == "highlight_ml_prediction"
    assert row.context_json is not None and '"segment_id": 10' in row.context_json
