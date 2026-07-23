"""模型注册、真实回滚与热加载集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.analysis.highlight_ml.registry import ModelRegistry
from app.analysis.highlight_ml.runtime import HotReloadingPredictor
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
from app.analysis.highlight_ml.training import TrainingConfig, train_candidate_models
from tests.unit.test_highlight_ml_training import _synthetic_bundle


def test_registry_promote_and_rollback_reload_real_artifacts(tmp_path: Path) -> None:
    """角色切换会改变热加载器使用的实体模型，而不只是修改展示元数据。"""
    bundle = _synthetic_bundle()
    comparison = train_candidate_models(bundle, TrainingConfig(include_xgboost=False))
    rules = comparison.candidates["rules"].artifact
    logistic = comparison.candidates["logistic"].artifact
    assert rules is not None and logistic is not None

    registry = ModelRegistry(tmp_path / "models", schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    first = registry.register(rules)
    second = registry.register(logistic, as_shadow=True)
    predictor = HotReloadingPredictor(registry)
    vector = bundle.X[0]

    initial = predictor.predict(vector)
    assert initial.champion_version == first.version
    assert initial.shadow_version == second.version
    assert initial.shadow_probability is not None

    assert registry.promote_shadow() == second.version
    promoted = predictor.predict(vector)
    assert promoted.champion_version == second.version
    assert promoted.shadow_version is None
    assert promoted.champion_probability == pytest.approx(initial.shadow_probability)

    registry.rollback(first.version)
    rolled_back = predictor.predict(vector)
    assert rolled_back.champion_version == first.version
    assert rolled_back.champion_probability == pytest.approx(initial.champion_probability)

    leftovers = [
        path.name for path in (tmp_path / "models").rglob("*") if ".tmp" in path.name or ".staging" in path.name
    ]
    assert leftovers == []


def test_registry_rejects_tampered_artifact(tmp_path: Path) -> None:
    """产物内容被修改后 SHA-256 校验失败关闭。"""
    bundle = _synthetic_bundle()
    artifact = train_candidate_models(bundle, TrainingConfig(include_xgboost=False)).selected_artifact
    registry = ModelRegistry(tmp_path / "models", schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    entry = registry.register(artifact)
    artifact_path = tmp_path / "models" / entry.relative_path / "artifact.json"
    artifact_path.write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="校验和"):
        registry.load_artifact(entry.version)


def test_registry_atomically_checks_model_attachments(tmp_path: Path) -> None:
    """漂移/盲审附件与模型同批发布，并按 Manifest 校验。"""
    bundle = _synthetic_bundle()
    artifact = train_candidate_models(bundle, TrainingConfig(include_xgboost=False)).selected_artifact
    registry = ModelRegistry(tmp_path / "models", schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    entry = registry.register(artifact, attachments={"drift-baseline.json": b'{"ok":true}'})

    assert registry.load_attachment(entry.version, "drift-baseline.json") == b'{"ok":true}'
    attachment = tmp_path / "models" / entry.relative_path / "drift-baseline.json"
    attachment.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="附件校验和"):
        registry.load_attachment(entry.version, "drift-baseline.json")
    with pytest.raises(ValueError, match="附件名"):
        registry.load_attachment(entry.version, "../artifact.json")
