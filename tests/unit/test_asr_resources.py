from __future__ import annotations

import ctypes
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import psutil
import pytest

from app.core import asr_detection
from app.core.asr_failures import ASRDeviceError


class ResourceBoundary:
    """仅控制驱动、nvidia-smi 和系统内存读取，不替换资源判断。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.identifiers = [UUID(int=1), UUID(int=2)]
        self.logical_order = [1, 0]
        self.free_mb = [9000, 100]
        self.ram_mb = 32768
        self.ordinals: list[int] = []
        self.queries: list[list[str]] = []
        self.driver = SimpleNamespace(
            cuInit=Mock(return_value=0),
            cuDeviceGet=Mock(side_effect=self.get_device),
            cuDeviceGetUuid_v2=Mock(side_effect=self.get_uuid),
        )
        original_cdll = ctypes.CDLL
        original_windll = getattr(ctypes, "WinDLL", None)
        original_run = subprocess.run

        def load_cdll(name: str, **kwargs: object) -> object:
            return self.driver if name == "libcuda.so.1" else original_cdll(name, **kwargs)

        def load_windll(name: str, **kwargs: object) -> object:
            if name == "nvcuda.dll":
                assert kwargs == {"winmode": 0x00000800}
                return self.driver
            assert original_windll is not None
            return original_windll(name, **kwargs)

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] != "nvidia-smi":
                return original_run(command, **kwargs)
            self.queries.append(command)
            assert kwargs == {"capture_output": True, "text": True, "timeout": 10, "check": True}
            indices = [0, 1]
            selections = [argument.removeprefix("--id=GPU-") for argument in command if argument.startswith("--id=")]
            if selections:
                indices = [self.identifiers.index(UUID(selections[0]))]
            return subprocess.CompletedProcess(
                command, 0, "".join(f"GPU {index}, 12000, {self.free_mb[index]}\n" for index in indices), ""
            )

        monkeypatch.setattr(ctypes, "CDLL", load_cdll)
        monkeypatch.setattr(ctypes, "WinDLL", load_windll, raising=False)
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: SimpleNamespace(total=65536 * 1024**2, available=self.ram_mb * 1024**2)
        )

    def get_device(self, pointer: object, ordinal: int) -> int:
        self.ordinals.append(ordinal)
        if ordinal not in range(len(self.logical_order)):
            return 101
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = self.logical_order[ordinal]
        return 0

    def get_uuid(self, pointer: object, device: int) -> int:
        ctypes.memmove(pointer, self.identifiers[device].bytes, 16)
        return 0


@pytest.mark.parametrize("device,expected", [("cuda", False), ("cuda:0", False), ("cuda:1", True)])
def test_selected_cuda_resources_follow_driver_order(
    monkeypatch: pytest.MonkeyPatch, device: str, expected: bool
) -> None:
    boundary = ResourceBoundary(monkeypatch)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1,0")
    sufficient, message = asr_detection.check_resources_sufficient("large-v3", device)
    assert sufficient is expected
    index = 1 if device == "cuda:1" else 0
    assert boundary.ordinals == [index]
    identifier = boundary.identifiers[boundary.logical_order[index]]
    assert boundary.queries[0][-1] == f"--id=GPU-{identifier}"
    assert ("显存不足" in message) is (not expected)


@pytest.mark.parametrize("stage", ["cuInit", "cuDeviceGet", "cuDeviceGetUuid_v2"])
def test_driver_failure_keeps_selected_resources_unknown(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    boundary = ResourceBoundary(monkeypatch)
    setattr(boundary.driver, stage, Mock(return_value=999))
    sufficient, message = asr_detection.check_resources_sufficient("small", "cuda:0")
    assert not sufficient and "cuda:0" in message and "检测失败" in message
    assert boundary.queries == []


def test_unavailable_cuda_index_does_not_query_other_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = ResourceBoundary(monkeypatch)
    sufficient, message = asr_detection.check_resources_sufficient("small", "cuda:7")
    assert not sufficient and "cuda:7" in message
    assert boundary.queries == []


@pytest.mark.parametrize("device", ["cuda:-1", "cuda:abc", "cuda:"])
def test_invalid_cuda_device_is_rejected(monkeypatch: pytest.MonkeyPatch, device: str) -> None:
    boundary = ResourceBoundary(monkeypatch)
    with pytest.raises(ASRDeviceError, match="非负整数"):
        asr_detection.check_resources_sufficient("small", device)
    assert boundary.ordinals == [] and boundary.queries == []


@pytest.mark.parametrize("output", ["", "GPU A, 12000, 9000\nGPU B, 12000, 9000\n", "GPU A, 12000, N/A\n"])
def test_selected_gpu_query_has_no_fallback(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    ResourceBoundary(monkeypatch)
    monkeypatch.setattr(
        subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, output, "")
    )
    sufficient, message = asr_detection.check_resources_sufficient("small", "cuda:0")
    assert not sufficient and "检测失败" in message


@pytest.mark.parametrize(
    "model,vram,ram", [("tiny", 500, 1000), ("small", 1500, 2000), ("medium", 3000, 4000), ("large-v3", 6000, 8000)]
)
@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
def test_known_model_estimates_keep_existing_thresholds(
    monkeypatch: pytest.MonkeyPatch, model: str, vram: int, ram: int, device: str
) -> None:
    boundary = ResourceBoundary(monkeypatch)
    boundary.ram_mb = int(ram * 0.8) - 1
    boundary.free_mb[1] = int(vram * 0.8) - 1
    assert not asr_detection.check_resources_sufficient(model, device)[0]
    boundary.ram_mb += 1
    boundary.free_mb[1] += 1
    assert asr_detection.check_resources_sufficient(model, device)[0]
    if device == "cpu":
        assert boundary.ordinals == []


@pytest.mark.parametrize("model", ["large-v3-turbo", "some/local-model", "unknown"])
def test_unknown_models_never_inherit_small_estimate(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    boundary = ResourceBoundary(monkeypatch)
    sufficient, message = asr_detection.check_resources_sufficient(model, "cuda:0")
    assert not sufficient and "资源需求未知" in message and repr(model) in message
    assert boundary.queries == []


def test_unresolved_auto_device_is_not_treated_as_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = ResourceBoundary(monkeypatch)
    sufficient, message = asr_detection.check_resources_sufficient("small", "auto")
    assert not sufficient and "尚未明确" in message
    assert boundary.queries == []
