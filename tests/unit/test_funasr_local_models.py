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


def test_load_nano_attaches_local_vad_with_bounded_segments(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Engine Pack 的 Nano 必须复用本地 FSMN-VAD，并限制单段时长。"""
    models_dir = tmp_path / "models"
    (models_dir / "funasr_nano").mkdir(parents=True)
    vad_dir = models_dir / "paraformer" / "fsmn-vad"
    vad_dir.mkdir(parents=True)
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setenv("BLC_MODELS_DIR", str(models_dir))

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    loaded = backend._load_funasr(for_primary=True)

    assert isinstance(loaded, FakeAutoModel)
    assert captured["model"] == str(models_dir / "funasr_nano")
    assert captured["vad_model"] == str(vad_dir)
    assert captured["vad_kwargs"] == {"max_single_segment_time": 30_000}


def test_nano_generate_uses_batched_input_cache_and_sentence_timestamps(monkeypatch: MonkeyPatch) -> None:
    """Nano 推理参数应适配 VAD 长音频，并解析句级时间戳。"""
    captured: dict[str, object] = {}

    class FakeModel:
        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return [
                {
                    "text": "第一句。第二句。",
                    "sentence_info": [
                        {"text": "第一句。", "start": 1000, "end": 2500, "confidence": 0.9},
                        {"text": "第二句。", "start": 2500, "end": 4000, "confidence": 0.8},
                    ],
                }
            ]

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    monkeypatch.setattr(backend, "_load_funasr", lambda *, for_primary=False: FakeModel())
    result = backend._transcribe_funasr_audio("normalized.wav", 10.0, 20.0, for_primary=True, audio_duration=10.0)

    assert captured == {
        "input": ["normalized.wav"],
        "cache": {},
        "batch_size_s": 0,
        "language": "中文",
        "sentence_timestamp": True,
    }
    assert [(item.start, item.end, item.text) for item in result.segments] == [
        (11.0, 12.5, "第一句。"),
        (12.5, 14.0, "第二句。"),
    ]


def test_nano_generate_receives_room_hotwords(monkeypatch: MonkeyPatch) -> None:
    """Fun-ASR-Nano 主引擎也必须接收直播间专属词典。"""
    captured: dict[str, object] = {}

    class FakeModel:
        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return [{"text": "专属词"}]

    backend = FunASRBackend(sensevoice=False, funasr_nano=True)
    monkeypatch.setattr(backend, "_load_funasr", lambda *, for_primary=False: FakeModel())
    monkeypatch.setattr("app.analysis.transcription.backends._probe_audio_duration", lambda _path: 3.0)

    backend.transcribe_funasr("normalized.wav", "查理斯, 亚运冠军")

    assert captured["hotword"] == "查理斯, 亚运冠军"
