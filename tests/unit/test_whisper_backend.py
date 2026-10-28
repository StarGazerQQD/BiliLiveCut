"""Whisper fallback model loading regression tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.analysis.transcription import backends
from app.core import asr_detection
from tests.unit.test_cuda_libraries import install_boundary


def test_unsupported_compute_type_retries_with_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """CTranslate2 拒绝显式计算类型时应改用设备自适应类型。"""
    calls: list[tuple[str, str, str]] = []
    loaded_model = object()

    def fake_whisper_model(model_size: str, *, device: str, device_index: int, compute_type: str) -> object:
        assert device_index == 0
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

    result = backends._load_whisper_model("whisper-model", "cuda", "int16")
    assert result is loaded_model
    assert calls == [
        ("whisper-model", "cuda", "int16"),
        ("whisper-model", "cuda", "auto"),
    ]


def test_unrelated_value_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """非计算类型错误必须保留原异常，不得被 auto 重试掩盖。"""
    calls: list[str] = []

    def fake_whisper_model(_model_size: str, *, device: str, device_index: int, compute_type: str) -> object:
        assert device_index == 0
        calls.append(f"{device}:{compute_type}")
        raise ValueError("broken model metadata")

    fake_module = ModuleType("faster_whisper")
    fake_module.__dict__["WhisperModel"] = fake_whisper_model
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr(asr_detection, "check_resources_sufficient", lambda *_args: (True, "ok"))

    with pytest.raises(ValueError, match="broken model metadata"):
        backends._load_whisper_model("whisper-model", "cuda", "int16")
    assert calls == ["cuda:int16"]


@pytest.fixture(autouse=True)
def cuda_runtime_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model constructor tests use an available CUDA library boundary."""
    from types import SimpleNamespace

    boundary = install_boundary(tmp_path, monkeypatch)
    boundary.package("nvidia-cublas-cu12")
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(
            get_cuda_device_count=lambda: 1,
            get_supported_compute_types=lambda device, index: {"float32", "int8_float32"},
        ),
    )


@pytest.mark.parametrize("gpu_count", [0, 1])
def test_auto_device_resolves_before_model_constructor(monkeypatch: pytest.MonkeyPatch, gpu_count: int) -> None:
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(
            get_cuda_device_count=lambda: gpu_count, get_supported_compute_types=lambda device, index: {"float32"}
        ),
    )
    monkeypatch.setattr(asr_detection, "check_resources_sufficient", lambda *_args: (True, "ok"))
    calls: list[dict[str, object]] = []

    def create(model: str, **kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=create))
    backends._load_whisper_model("local-model", "auto", "float32")
    assert calls == [
        {"device": "cuda", "device_index": 0, "compute_type": "float32"}
        if gpu_count
        else {"device": "cpu", "compute_type": "float32"}
    ]
