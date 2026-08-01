from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

from app.analysis.transcription.backends import FunASRBackend, _legacy_campplus_kwargs

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _write_legacy_campplus(model_dir: Path, *, weight_name: str = "campplus_cn_common.bin") -> Path:
    model_dir.mkdir(parents=True)
    metadata = {
        "model": {
            "type": "cam++-sv",
            "model_config": {"sample_rate": 16000, "fbank_dim": 80, "emb_size": 192},
            "pretrained_model": weight_name,
        }
    }
    (model_dir / "configuration.json").write_text(json.dumps(metadata), encoding="utf-8")
    weight_path = model_dir / weight_name
    weight_path.write_bytes(b"model-weight")
    return weight_path


def test_legacy_campplus_metadata_builds_registered_model_arguments(tmp_path: Path) -> None:
    model_dir = tmp_path / "campplus"
    weight_path = _write_legacy_campplus(model_dir)

    kwargs = _legacy_campplus_kwargs(model_dir)

    assert kwargs is not None
    assert kwargs["model_path"] == str(model_dir.resolve())
    assert kwargs["init_param"] == str(weight_path.resolve())
    assert kwargs["frontend"] == "WavFrontend"
    assert kwargs["frontend_conf"] == {"fs": 16000}
    assert kwargs["model_conf"] == {
        "feat_dim": 80,
        "embedding_size": 192,
        "growth_rate": 32,
        "bn_size": 4,
        "init_channels": 128,
        "config_str": "batchnorm-relu",
        "memory_efficient": True,
        "output_level": "segment",
    }


def test_current_campplus_config_uses_native_local_directory(tmp_path: Path) -> None:
    model_dir = tmp_path / "campplus"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("model: CAMPPlus\n", encoding="utf-8")

    assert _legacy_campplus_kwargs(model_dir) is None


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"model": {"type": "unknown", "model_config": {}}}, "Unsupported legacy CAM++"),
        (
            {
                "model": {
                    "type": "cam++-sv",
                    "model_config": {"sample_rate": 16000, "fbank_dim": 80, "emb_size": 192},
                    "pretrained_model": "missing.bin",
                }
            },
            "weight is missing or empty",
        ),
    ],
)
def test_invalid_legacy_campplus_metadata_fails_clearly(
    tmp_path: Path,
    metadata: dict[str, object],
    message: str,
) -> None:
    model_dir = tmp_path / "campplus"
    model_dir.mkdir()
    (model_dir / "configuration.json").write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        _legacy_campplus_kwargs(model_dir)


def test_load_primary_passes_legacy_campplus_arguments_to_funasr(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    models_dir = tmp_path / "models"
    paraformer_dir = models_dir / "paraformer"
    (paraformer_dir / "fsmn-vad").mkdir(parents=True)
    (paraformer_dir / "ct-punc").mkdir()
    weight_path = _write_legacy_campplus(paraformer_dir / "campplus")
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setenv("BLC_MODELS_DIR", str(models_dir))

    backend = FunASRBackend(sensevoice=False, funasr_nano=False)
    loaded = backend._load_primary()

    assert isinstance(loaded, FakeAutoModel)
    assert captured["model"] == str(paraformer_dir)
    assert captured["vad_model"] == str(paraformer_dir / "fsmn-vad")
    assert captured["punc_model"] == str(paraformer_dir / "ct-punc")
    assert captured["spk_model"] == "CAMPPlus"
    spk_kwargs = captured["spk_kwargs"]
    assert isinstance(spk_kwargs, dict)
    assert spk_kwargs["init_param"] == str(weight_path.resolve())
    assert captured["disable_update"] is True
