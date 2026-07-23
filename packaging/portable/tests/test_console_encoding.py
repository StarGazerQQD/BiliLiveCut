"""Portable 命令行入口的 Windows 旧代码页兼容性测试。"""

from __future__ import annotations

import importlib
import io
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
_SRC_DIR = _PORTABLE_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def _install_legacy_console(monkeypatch: MonkeyPatch) -> tuple[io.BytesIO, io.TextIOWrapper, io.TextIOWrapper]:
    """安装严格 cp1252 输出流并返回底层缓冲区。"""
    stdout_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    return stdout_bytes, stdout, stderr


@pytest.mark.parametrize(
    ("module_name", "action_name", "argv", "passes_argv"),
    (
        ("blc_portable.engine_pack.downloader", "show_status", ["download_engines.py", "--status"], False),
        ("blc_portable.builders.lite", "build_exe", ["build_exe.py"], True),
        ("blc_portable.builders.full", "build_full_bundle", ["build_full_bundle.py"], False),
        ("blc_portable.payload.builder", "build_payload", ["build_payload.py"], False),
        ("blc_portable.builders.common", "copy_source", ["build_bundle.py", "--only-source"], False),
    ),
)
def test_portable_cli_entrypoints_reconfigure_legacy_console(
    monkeypatch: MonkeyPatch,
    module_name: str,
    action_name: str,
    argv: list[str],
    passes_argv: bool,
) -> None:
    """全部 Portable CLI 必须先切换 UTF-8，再执行可能输出中文的操作。"""
    module: ModuleType = importlib.import_module(module_name)
    stdout_bytes, stdout, stderr = _install_legacy_console(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)

    def emit_chinese(*_args: object, **_kwargs: object) -> None:
        print("模型状态")

    monkeypatch.setattr(module, action_name, emit_chinese)
    entrypoint: Callable[..., object] = module.main

    result = entrypoint([]) if passes_argv else entrypoint()
    stdout.flush()
    stderr.flush()

    assert result in (None, 0)
    assert stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert stderr.encoding.lower().replace("_", "-") == "utf-8"
    assert "模型状态" in stdout_bytes.getvalue().decode("utf-8")


def test_payload_module_entrypoint_reconfigures_legacy_console(monkeypatch: MonkeyPatch) -> None:
    """Payload 的模块直执行入口也必须保护包含中文路径的构建报告。"""
    from blc_portable.payload import builder

    stdout_bytes, stdout, stderr = _install_legacy_console(monkeypatch)

    def fake_build_payload() -> dict[str, object]:
        return {
            "zip_path": "模型/源码包.zip",
            "manifest_path": "模型/manifest.json",
            "payload_file_count": 1,
            "payload_sha256": "a" * 64,
            "verified_reproducible": True,
        }

    monkeypatch.setattr(builder, "build_payload", fake_build_payload)

    builder._run_build_payload_main()
    stdout.flush()
    stderr.flush()

    assert stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert "模型/源码包.zip" in stdout_bytes.getvalue().decode("utf-8")


def test_full_module_guard_uses_console_safe_main() -> None:
    """Full 模块直执行不得绕过带编码初始化的 callable main。"""
    source = (_SRC_DIR / "blc_portable" / "builders" / "full.py").read_text(encoding="utf-8")

    assert 'if __name__ == "__main__":\n    raise SystemExit(main())' in source
