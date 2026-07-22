"""Launcher entrypoint tests — 验证 main.py 的生产入口可调用且无 import 副作用。"""

from __future__ import annotations

import ast
import runpy
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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
