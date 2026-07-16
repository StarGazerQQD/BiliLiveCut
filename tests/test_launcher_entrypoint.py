"""测试 Launcher 主入口 — 确保 callable main() 存在且无副作用。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _pytest.capture import CaptureFixture
    from _pytest.monkeypatch import MonkeyPatch

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_launcher_exports_callable_main() -> None:
    """验证 launcher/main.py 提供可调用的 main() 函数。"""
    sys.path.insert(0, str(REPO_ROOT / "packaging" / "portable" / "src"))
    from blc_portable.launcher.main import main  # noqa: E402

    assert callable(main), "main 必须是 callable"


def test_launcher_module_import_has_no_side_effects(monkeypatch: MonkeyPatch) -> None:
    """验证 import launcher 模块本身不触发安装或启动副作用。"""
    monkeypatch.setattr("os.chdir", lambda _: None)

    sys.path.insert(0, str(REPO_ROOT / "packaging" / "portable" / "src"))
    from blc_portable.launcher import main as launch_mod  # noqa: E402

    assert hasattr(launch_mod, "main"), "launcher 模块必须有 main 函数"
    assert hasattr(launch_mod, "build_parser"), "launcher 模块必须有 build_parser 函数"
    assert hasattr(launch_mod, "run_launcher"), "launcher 模块必须有 run_launcher 函数"


def test_launcher_version_python_entrypoint() -> None:
    """验证 --version 通过 Python 入口返回成功。"""
    src_dir = REPO_ROOT / "packaging" / "portable" / "src"
    src = str(src_dir.resolve())
    code = f"import sys; sys.path.insert(0, {src!r}); from blc_portable.launcher.main import main; raise SystemExit(main(['--version']))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"--version 应返回 0，实际 {result.returncode}: {result.stderr}"
    assert "BiliLiveCut" in result.stdout, "--version 应包含 BiliLiveCut"


def test_launcher_help_python_entrypoint() -> None:
    """验证 --help 通过 Python 入口返回成功。"""
    src_dir = REPO_ROOT / "packaging" / "portable" / "src"
    src = str(src_dir.resolve())
    code = f"import sys; sys.path.insert(0, {src!r}); from blc_portable.launcher.main import main; raise SystemExit(main(['--help']))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, f"--help 应返回 0，实际 {result.returncode}: {result.stderr}"
    assert "usage:" in result.stdout.lower() or "--help" in result.stdout, "--help 应包含帮助信息"


def test_launcher_invalid_args_return_nonzero() -> None:
    """验证无效参数返回非零退出码。"""
    src_dir = REPO_ROOT / "packaging" / "portable" / "src"
    src = str(src_dir.resolve())
    code = f"import sys; sys.path.insert(0, {src!r}); from blc_portable.launcher.main import main; "
    code += "exit_code = main(['--nonexistent-flag-xyz']); sys.exit(exit_code if exit_code else 0)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # argparse recognizes unknown args and exits with 2
    assert result.returncode != 0, f"无效参数应返回非零，实际 {result.returncode}"
