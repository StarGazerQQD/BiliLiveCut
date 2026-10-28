# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, distribution
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DeviceFailure(BaseModel):
    """可跨进程复用的确定性设备故障，不持有异常或模型对象。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    backend: Literal["torch"] = "torch"
    device: str = Field(pattern=r"^cuda:[0-9]+$")
    build: str = Field(min_length=1, max_length=128)
    code: Literal["cpu_build", "unsupported_kernel"]
    message: str = Field(min_length=1, max_length=1000)


class ASRDeviceError(RuntimeError):
    """设备不可用；仅携带已确认故障记录的异常可以跳过重复重试。"""

    def __init__(self, message: str, *, failure: DeviceFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure


class ASRUnavailableError(RuntimeError):
    """全部已启用的识别路径被确定性设备错误阻断。"""


def permanent_device_error(error: BaseException) -> bool:
    """只检查当前终态异常，不能把已恢复的上游故障归给后续临时故障。"""
    return isinstance(error, ASRUnavailableError) or isinstance(error, ASRDeviceError) and error.failure is not None


def device_failure_scope(config: Mapping[str, object]) -> str:
    """配置或已安装包变化时废弃旧记录，父进程不导入 GPU 库。"""
    packages: list[tuple[str, str, int]] = []
    for name in ("torch", "ctranslate2"):
        try:
            package = distribution(name)
        except PackageNotFoundError:
            packages.append((name, "missing", 0))
        else:
            metadata = package.locate_file(f"{name}/__init__.py")
            packages.append((name, package.version, metadata.stat().st_mtime_ns if metadata.is_file() else 0))
    values = {
        "settings": {key: value for key, value in config.items() if key.startswith(("asr_", "whisper_"))},
        "python": sys.executable,
        "packages": packages,
        "visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@dataclass
class _DeviceFailures:
    notify: Callable[[DeviceFailure], None]
    failures: dict[tuple[str, str, str], DeviceFailure] = field(default_factory=dict)


_current: ContextVar[_DeviceFailures | None] = ContextVar("asr_device_failures", default=None)


@contextmanager
def device_failure_context(known: list[DeviceFailure], notify: Callable[[DeviceFailure], None]) -> Iterator[None]:
    """将同配置的父进程记录应用于本次请求，退出后释放请求回调。"""
    state = _DeviceFailures(notify, {(item.backend, item.device, item.build): item for item in known})
    token = _current.set(state)
    try:
        yield
    finally:
        _current.reset(token)


def cached_device_failure(device: str, build: str) -> DeviceFailure | None:
    """仅复用同一 Torch 构建与设备的确定性失败。"""
    state = _current.get()
    return state.failures.get(("torch", device, build)) if state is not None else None


def remember_device_failure(failure: DeviceFailure) -> None:
    """首次确认失败时同步父进程，成功回退也保留设备诊断。"""
    state = _current.get()
    if state is None:
        return
    key = (failure.backend, failure.device, failure.build)
    if key not in state.failures:
        state.failures[key] = failure
        state.notify(failure)
