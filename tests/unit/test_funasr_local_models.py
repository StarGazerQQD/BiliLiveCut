from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from app.analysis.transcription.backends import FunASRBackend

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_load_primary_uses_only_current_paraformer_models(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    models_dir = tmp_path / "models"
    paraformer_dir = models_dir / "paraformer"
    (paraformer_dir / "fsmn-vad").mkdir(parents=True)
    (paraformer_dir / "ct-punc").mkdir()
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
    assert "spk_model" not in captured
    assert "spk_kwargs" not in captured
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
