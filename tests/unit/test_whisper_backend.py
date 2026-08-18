"""Whisper fallback model loading regression tests."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from app.analysis.transcription import backends
from app.core import asr_detection


def test_unsupported_compute_type_retries_with_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """CTranslate2 拒绝显式计算类型时应改用设备自适应类型。"""
    calls: list[tuple[str, str, str]] = []
    loaded_model = object()

    def fake_whisper_model(model_size: str, *, device: str, compute_type: str) -> object:
        calls.append((model_size, device, compute_type))
        if compute_type == "int16":
            raise ValueError(
                "Requested int16 compute type, but the target device or backend "
                "do not support efficient int16 computation"
            )
        return loaded_model

    fake_module = ModuleType("faster_whisper")
    fake_module.__dict__["WhisperModel"] = fake_whisper_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr(asr_detection, "check_resources_sufficient", lambda *_args: (True, "ok"))
    backends._load_whisper_model.cache_clear()

    try:
        result = backends._load_whisper_model("whisper-model", "cuda", "int16")
    finally:
        backends._load_whisper_model.cache_clear()

    assert result is loaded_model
    assert calls == [
        ("whisper-model", "cuda", "int16"),
        ("whisper-model", "cuda", "auto"),
    ]


def test_unrelated_value_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """非计算类型错误必须保留原异常，不得被 auto 重试掩盖。"""
    calls: list[str] = []

    def fake_whisper_model(_model_size: str, *, device: str, compute_type: str) -> object:
        calls.append(f"{device}:{compute_type}")
        raise ValueError("broken model metadata")

    fake_module = ModuleType("faster_whisper")
    fake_module.__dict__["WhisperModel"] = fake_whisper_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr(asr_detection, "check_resources_sufficient", lambda *_args: (True, "ok"))
    backends._load_whisper_model.cache_clear()

    try:
        with pytest.raises(ValueError, match="broken model metadata"):
            backends._load_whisper_model("whisper-model", "cuda", "int16")
    finally:
        backends._load_whisper_model.cache_clear()

    assert calls == ["cuda:int16"]
