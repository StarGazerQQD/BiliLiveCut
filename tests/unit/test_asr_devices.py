"""CUDA checks exercise library boundaries without requiring a physical NVIDIA GPU."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import asr_devices
from app.core.asr_failures import DeviceFailure, device_failure_context, device_failure_scope, permanent_device_error
from tests.unit.test_cuda_libraries import CudaBoundary, install_boundary


@pytest.fixture
def cublas_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CudaBoundary:
    boundary = install_boundary(tmp_path, monkeypatch)
    boundary.package("nvidia-cublas-cu12")
    return boundary


class Tensor:
    def __init__(self, device: str) -> None:
        self.device = device
        self.added = False

    def add_(self, value: int) -> Tensor:
        assert value == 1
        self.added = True
        return self


class Module:
    def __init__(self, device: str) -> None:
        self.device = device

    def parameters(self) -> Iterator[Tensor]:
        yield Tensor(self.device)


@pytest.fixture
def torch_boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    calls: list[str] = []

    def zeros(size: int, *, device: str) -> Tensor:
        assert size == 1
        calls.append(device)
        return Tensor(device)

    fake = SimpleNamespace(
        __version__="2.13.0+cu126",
        version=SimpleNamespace(cuda="12.6"),
        cuda=SimpleNamespace(
            is_available=lambda: True, device_count=lambda: 2, synchronize=lambda index: calls.append(f"sync:{index}")
        ),
        zeros=zeros,
        calls=calls,
    )
    monkeypatch.setitem(sys.modules, "torch", fake)
    return fake


def test_cpu_does_not_import_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)
    asr_devices.require_torch_device("cpu", role="primary")


@pytest.mark.parametrize("device", ["cuda:-1", "cuda:x", "cuda:1.0", "cuda:１", "cudafoo", "cuda:"])
def test_bad_cuda_device_is_rejected(device: str) -> None:
    with pytest.raises(asr_devices.ASRDeviceError, match="非负整数"):
        asr_devices.cuda_device_index(device)


def test_cpu_build_fails_before_model_download(torch_boundary: SimpleNamespace) -> None:
    torch_boundary.version.cuda = None
    torch_boundary.__version__ = "2.13.0+cpu"
    with pytest.raises(asr_devices.ASRDeviceError, match="CPU 版.*--torch-runtime"):
        asr_devices.require_torch_device("cuda", role="primary")
    assert torch_boundary.calls == []


def test_missing_driver_is_distinct_from_cpu_build(torch_boundary: SimpleNamespace) -> None:
    torch_boundary.cuda.is_available = lambda: False
    with pytest.raises(asr_devices.ASRDeviceError, match="NVIDIA 驱动"):
        asr_devices.require_torch_device("cuda", role="auxiliary")
    assert torch_boundary.calls == []


def test_missing_device_index_does_not_use_another_gpu(torch_boundary: SimpleNamespace) -> None:
    with pytest.raises(asr_devices.ASRDeviceError, match="对应 GPU"):
        asr_devices.require_torch_device("cuda:2", role="review")
    assert torch_boundary.calls == []


def test_cuda_request_executes_on_requested_index(torch_boundary: SimpleNamespace) -> None:
    asr_devices.require_torch_device("cuda:1", role="review")
    assert torch_boundary.calls == ["cuda:1", "sync:1"]


def test_kernel_failure_retains_cause(torch_boundary: SimpleNamespace) -> None:
    original = RuntimeError("no kernel image is available for execution on the device")

    def fail(size: int, *, device: str) -> Tensor:
        raise original

    torch_boundary.zeros = fail
    with pytest.raises(asr_devices.ASRDeviceError) as error:
        asr_devices.require_torch_device("cuda", role="primary")
    assert error.value.__cause__ is original


@pytest.mark.parametrize("component", ["model", "vad_model", "punc_model"])
def test_sdk_cpu_fallback_is_rejected_for_each_component(component: str) -> None:
    model = SimpleNamespace(model=Module("cuda:0"), vad_model=Module("cuda:0"), punc_model=Module("cuda:0"))
    setattr(model, component, Module("cpu"))
    with pytest.raises(asr_devices.ASRDeviceError, match=component + ".*实际参数设备为 cpu"):
        asr_devices.confirm_torch_model_device(model, "cuda", role="primary")


def test_loaded_models_all_use_requested_gpu() -> None:
    model = SimpleNamespace(model=Module("cuda:1"), vad_model=Module("cuda:1"))
    asr_devices.confirm_torch_model_device(model, "cuda:1", role="primary")


def test_opaque_model_cannot_claim_cuda_success() -> None:
    with pytest.raises(asr_devices.ASRDeviceError, match="不可核实"):
        asr_devices.confirm_torch_model_device(object(), "cuda", role="primary")


def test_whisper_cuda_does_not_depend_on_torch(monkeypatch: pytest.MonkeyPatch, cublas_boundary: CudaBoundary) -> None:
    monkeypatch.setitem(sys.modules, "torch", None)
    calls: list[tuple[str, int]] = []

    def supported(device: str, index: int) -> set[str]:
        calls.append((device, index))
        return {"float16"}

    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(get_cuda_device_count=lambda: 2, get_supported_compute_types=supported),
    )
    assert asr_devices.require_whisper_device("cuda:1") == 1
    assert calls == [("cuda", 1)]


def test_whisper_unavailable_gpu_has_no_cpu_fallback(
    monkeypatch: pytest.MonkeyPatch, cublas_boundary: CudaBoundary
) -> None:
    monkeypatch.setitem(sys.modules, "ctranslate2", SimpleNamespace(get_cuda_device_count=lambda: 0))
    with pytest.raises(asr_devices.ASRDeviceError, match="CTranslate2"):
        asr_devices.require_whisper_device("cuda")


@pytest.mark.parametrize("gpu_count", [0, 1])
def test_whisper_auto_prepares_libraries_only_when_cuda_is_selected(
    monkeypatch: pytest.MonkeyPatch, cublas_boundary: CudaBoundary, gpu_count: int
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(
            get_cuda_device_count=lambda: gpu_count, get_supported_compute_types=lambda device, index: {"float32"}
        ),
    )
    assert asr_devices.require_whisper_device("auto") == (0 if gpu_count else None)
    assert bool(cublas_boundary.loaded) == bool(gpu_count)


@pytest.mark.parametrize(
    "loader,field",
    [
        ("_load_primary", "asr_primary_device"),
        ("_load_sensevoice", "asr_auxiliary_device"),
        ("_load_funasr", "asr_review_device"),
    ],
)
def test_funasr_loaders_reject_cpu_wheel_before_sdk(
    monkeypatch: pytest.MonkeyPatch, torch_boundary: SimpleNamespace, loader: str, field: str
) -> None:
    from app.analysis.transcription.backends import FunASRBackend, settings

    torch_boundary.version.cuda = None
    monkeypatch.setattr(settings, field, "cuda")
    monkeypatch.setitem(sys.modules, "funasr", None)
    with pytest.raises(asr_devices.ASRDeviceError, match="CPU 版"):
        getattr(FunASRBackend(), loader)()


def test_diagnostic_exposes_build_and_requested_device(torch_boundary: SimpleNamespace) -> None:
    torch_boundary.version.cuda = None
    report = asr_devices.device_report("torch", "cuda")
    assert report["requested"] == "cuda" and report["cuda_build"] is None
    assert report["ok"] is False and "CPU 版" in str(report["error"])


def test_nano_primary_uses_primary_device_before_loading(
    monkeypatch: pytest.MonkeyPatch, torch_boundary: SimpleNamespace
) -> None:
    from app.analysis.transcription.backends import FunASRBackend, settings

    torch_boundary.version.cuda = None
    monkeypatch.setattr(settings, "asr_primary_device", "cuda")
    monkeypatch.setattr(settings, "asr_review_device", "cpu")
    monkeypatch.setitem(sys.modules, "funasr", None)
    with pytest.raises(asr_devices.ASRDeviceError, match="primary.*CPU 版"):
        FunASRBackend()._load_funasr(for_primary=True)


def test_confirmed_kernel_failure_is_reused_across_roles_and_requests(torch_boundary: SimpleNamespace) -> None:
    recorded: list[DeviceFailure] = []
    calls: list[str] = []

    def fail(size: int, *, device: str) -> Tensor:
        calls.append(device)
        raise RuntimeError("no kernel image is available for execution on the device")

    torch_boundary.zeros = fail
    with device_failure_context([], recorded.append):
        for role in ("primary", "review"):
            with pytest.raises(asr_devices.ASRDeviceError) as caught:
                asr_devices.require_torch_device("cuda", role=role)
            assert permanent_device_error(caught.value)
    assert calls == ["cuda:0"] and len(recorded) == 1
    with device_failure_context(recorded[:], recorded.append):
        with pytest.raises(asr_devices.ASRDeviceError) as caught:
            asr_devices.require_torch_device("cuda:0", role="primary")
        assert caught.value.__cause__ is None
        torch_boundary.__version__ = "2.13.0+cu130"
        with pytest.raises(asr_devices.ASRDeviceError):
            asr_devices.require_torch_device("cuda:0", role="primary")
        with pytest.raises(asr_devices.ASRDeviceError):
            asr_devices.require_torch_device("cuda:1", role="primary")
    assert calls == ["cuda:0", "cuda:0", "cuda:1"]


@pytest.mark.parametrize("message", ["CUDA out of memory", "CUDA initialization error"])
def test_transient_kernel_failure_is_not_cached(torch_boundary: SimpleNamespace, message: str) -> None:
    original = torch_boundary.zeros
    recorded: list[DeviceFailure] = []

    def fail(size: int, *, device: str) -> Tensor:
        raise RuntimeError(message)

    torch_boundary.zeros = fail
    with device_failure_context([], recorded.append):
        with pytest.raises(asr_devices.ASRDeviceError) as caught:
            asr_devices.require_torch_device("cuda", role="primary")
        assert not permanent_device_error(caught.value)
        torch_boundary.zeros = original
        asr_devices.require_torch_device("cuda", role="primary")
    assert recorded == [] and torch_boundary.calls == ["cuda:0", "sync:0"]


def test_torch_failure_does_not_disable_whisper(
    torch_boundary: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, cublas_boundary: CudaBoundary
) -> None:
    recorded: list[DeviceFailure] = []
    torch_boundary.version.cuda = None
    monkeypatch.setitem(
        sys.modules,
        "ctranslate2",
        SimpleNamespace(get_cuda_device_count=lambda: 1, get_supported_compute_types=lambda *args: {"float16"}),
    )
    with device_failure_context([], recorded.append):
        with pytest.raises(asr_devices.ASRDeviceError):
            asr_devices.require_torch_device("cuda", role="primary")
        assert asr_devices.require_whisper_device("cuda") == 0
    assert len(recorded) == 1 and recorded[0].code == "cpu_build"


def test_failure_scope_changes_with_configuration_and_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"asr_primary_device": "cuda", "database_url": "ignored"}
    before = device_failure_scope(config)
    assert before == device_failure_scope({**config, "database_url": "different"})
    assert before != device_failure_scope({**config, "asr_primary_device": "cpu"})
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "another-device")
    assert before != device_failure_scope(config)


def test_failure_scope_changes_when_installed_runtime_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import asr_failures

    metadata = tmp_path / "__init__.py"
    metadata.write_text("", encoding="utf-8")
    package = SimpleNamespace(version="2.13.0+cu126", locate_file=lambda name: metadata)
    monkeypatch.setattr(asr_failures, "distribution", lambda name: package)
    config = {"asr_primary_device": "cuda"}
    before = device_failure_scope(config)
    package.version = "2.13.0+cu130"
    assert before != device_failure_scope(config)
