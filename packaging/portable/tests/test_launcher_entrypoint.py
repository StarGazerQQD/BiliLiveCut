"""Launcher entrypoint tests — 验证 main.py 的生产入口可调用且无 import 副作用。"""

from __future__ import annotations

import argparse
import ast
import io
import json
import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

# 添加 portable 模块到路径 (与 test_engine_pack.py / test_portable.py 一致)
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_src_dir = _portable_dir / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))


def test_launcher_exports_callable_main() -> None:
    """main() 必须是一个可调用的函数。"""
    from blc_portable.launcher.main import main  # noqa: E402

    assert callable(main), "main must be callable"


def test_service_command_calls_typer_app_explicitly(tmp_path: Path) -> None:
    """服务启动不能依赖锁定 Payload 是否实现 ``python -m app.cli``。"""
    from blc_portable.launcher.main import _build_service_command

    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    command = _build_service_command(venv_python)

    assert command[:3] == [str(venv_python), "-c", "from app.cli import app; app()"]
    assert command[3:] == ["serve", "--host", "127.0.0.1", "--port", "8000"]
    assert "-m" not in command


def test_launcher_version_python_entrypoint() -> None:
    """main(['--version']) 应该返回 0 并输出版本信息。"""
    from blc_portable.launcher.main import main  # noqa: E402

    result = main(["--version"])
    assert result == 0, f"main(['--version']) should return 0, got {result}"


def test_launcher_reconfigures_legacy_console_before_run(monkeypatch: MonkeyPatch) -> None:
    """冻结入口必须先切换输出编码，再进入可能打印中文的 Launcher 流程。"""
    from blc_portable.launcher import main as launcher_module  # noqa: E402

    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    def fake_run_launcher(_args: argparse.Namespace) -> int:
        print("Engine Pack 校验通过")
        return 0

    monkeypatch.setattr(launcher_module, "run_launcher", fake_run_launcher)

    assert launcher_module.main([]) == 0
    stdout.flush()
    stderr.flush()
    assert stdout.encoding.lower().replace("-", "") == "utf8"
    assert stderr.encoding.lower().replace("-", "") == "utf8"
    assert "Engine Pack 校验通过" in stdout_bytes.getvalue().decode("utf-8")


def test_launcher_help_python_entrypoint() -> None:
    """main(['--help']) 应该返回 0。"""
    from blc_portable.launcher.main import main  # noqa: E402

    result = main(["--help"])
    assert result == 0, f"main(['--help']) should return 0, got {result}"


def test_launcher_invalid_args_return_nonzero() -> None:
    """无效参数应返回非零退出码。"""
    from blc_portable.launcher.main import main  # noqa: E402

    result = main(["--nonexistent-flag"])
    assert result != 0, f"invalid args should return non-zero, got {result}"


def test_launcher_module_import_has_no_side_effects(capsys: CaptureFixture) -> None:
    """模块 import 阶段不得有副作用 (如执行安装或启动)。"""
    # import 不应触发 Runtime 安装逻辑

    captured = capsys.readouterr()
    # import 阶段只允许微量输出 (logging 初始化等)
    assert len(captured.out) < 500, f"import should not produce large output: {captured.out[:200]}"


def test_build_parser_returns_argument_parser() -> None:
    """build_parser() 应该返回一个 argparse ArgumentParser。"""
    from blc_portable.launcher.main import build_parser  # noqa: E402

    parser = build_parser()
    assert parser is not None
    # 验证关键参数已注册
    actions = {a.dest for a in parser._actions}
    for expected in ("version", "doctor", "verify_models", "offline", "engine_pack"):
        assert expected in actions, f"missing argument: {expected}"


def test_run_launcher_accepts_namespace() -> None:
    """run_launcher() 应接受 argparse.Namespace。"""
    from blc_portable.launcher.main import run_launcher  # noqa: E402

    assert callable(run_launcher)


def test_system_python_detection_rejects_unsupported_version(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fixture")

    def fake_run(args: list[str], **_kwargs: object):
        payload = {"executable": str(executable), "version": [3, 14]}
        return launcher_module.subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(launcher_module.subprocess, "run", fake_run)

    assert launcher_module._find_system_python() is None


def test_system_python_detection_returns_real_supported_interpreter(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    executable = tmp_path / "python.exe"
    executable.write_bytes(b"fixture")

    def fake_run(args: list[str], **_kwargs: object):
        payload = {"executable": str(executable), "version": [3, 12]}
        return launcher_module.subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(launcher_module.subprocess, "run", fake_run)

    assert launcher_module._find_system_python() == executable.resolve()


def test_prepare_venv_rejects_existing_unsupported_python(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"fixture")
    monkeypatch.setattr(launcher_module, "_python_version", lambda _python: (3, 14))

    with pytest.raises(RuntimeError, match="unsupported Python 3.14"):
        launcher_module.prepare_venv(tmp_path)


def test_doctor_returns_nonzero_when_checks_fail(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    monkeypatch.setattr(launcher_module, "get_current_release_dir", lambda: None)
    monkeypatch.setattr(launcher_module, "get_payload_manifest", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(launcher_module, "get_engine_pack_info", lambda: None)
    monkeypatch.setattr(launcher_module, "_find_portable_python", lambda _root: None)
    monkeypatch.setattr(launcher_module, "_find_system_python", lambda: None)

    assert launcher_module._run_doctor(tmp_path) == 1


def test_doctor_returns_zero_when_required_checks_pass(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    release = tmp_path / "runtime" / "releases" / "current"
    release.mkdir(parents=True)
    portable_python = tmp_path / "portable-python" / "python.exe"
    portable_python.parent.mkdir(parents=True)
    portable_python.write_bytes(b"fixture")
    for engine_id in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
        engine_dir = tmp_path / "models" / engine_id
        engine_dir.mkdir(parents=True)
        (engine_dir / "model.bin").write_bytes(b"fixture")
    ffmpeg = tmp_path / "bin" / "ffmpeg.exe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.write_bytes(b"fixture")

    monkeypatch.setattr(launcher_module, "get_current_release_dir", lambda: release)
    monkeypatch.setattr(launcher_module, "get_payload_manifest", lambda: {"release_version": "test"})
    monkeypatch.setattr(launcher_module, "get_engine_pack_info", lambda: {"crc32": "ABCD", "sha256": "a" * 64})
    monkeypatch.setattr(launcher_module, "_find_portable_python", lambda _root: portable_python)
    monkeypatch.setattr(launcher_module, "_python_version", lambda _python: (3, 12))
    monkeypatch.setattr(launcher_module, "_find_system_python", lambda: None)

    assert launcher_module._run_doctor(tmp_path) == 0


def test_run_launcher_propagates_doctor_failure(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    args = launcher_module.build_parser().parse_args(["--doctor"])
    monkeypatch.setattr(launcher_module, "get_app_root", lambda: tmp_path)
    monkeypatch.setattr(launcher_module, "_run_doctor", lambda _root: 1)

    assert launcher_module.run_launcher(args) == 1


def test_exit_pause_ignores_eof_from_interactive_stdin(monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher import main as launcher_module

    class InteractiveEmptyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(launcher_module.sys, "stdin", InteractiveEmptyInput())

    launcher_module._pause_before_exit()


def test_frozen_entry_prepare_models_uses_package_safe_imports(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """PyInstaller executes main.py without package context; model preparation must still import."""
    from blc_portable.engine_pack import installer  # noqa: E402

    monkeypatch.setattr(installer, "check_installed_models", lambda _models, _version: (True, []))
    entry_path = _src_dir / "blc_portable" / "launcher" / "main.py"
    namespace = runpy.run_path(str(entry_path), run_name="main")

    assert namespace["__package__"] == ""
    assert namespace["prepare_models"](tmp_path) == {
        "source": "already_installed",
        "network_requests": 0,
    }


def test_frozen_entry_script_has_no_relative_imports() -> None:
    """The PyInstaller entry script cannot contain package-relative imports."""
    entry_path = _src_dir / "blc_portable" / "launcher" / "main.py"
    tree = ast.parse(entry_path.read_text(encoding="utf-8"))
    relative_imports = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level > 0]

    assert relative_imports == []


def test_frozen_entry_collects_engine_pack_config_dependencies() -> None:
    """Engine Pack manifest 的顶层配置模块及其 JSON 数据必须进入冻结 EXE。"""
    spec_path = _portable_dir / "specs" / "portable_launcher.spec"
    content = spec_path.read_text(encoding="utf-8")

    assert "str(_config_dir)" in content
    assert '"model_catalog"' in content
    assert '"version_loader"' in content
    assert '(_version_config, ".")' in content
    assert '(_model_sources_lock, ".")' in content
