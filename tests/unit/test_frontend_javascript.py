"""前端静态 JavaScript 的语法回归测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "app" / "web" / "static"
JAVASCRIPT_FILES = tuple(sorted(STATIC_ROOT.rglob("*.js")))


@pytest.mark.parametrize(
    "javascript_path",
    JAVASCRIPT_FILES,
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_static_javascript_has_valid_module_syntax(javascript_path: Path) -> None:
    """每个静态 JavaScript 文件都必须能按 ES Module 语法解析。"""
    node = shutil.which("node")
    assert node is not None, "前端语法检查需要 Node.js"

    result = subprocess.run(
        [node, "--input-type=module", "--check"],
        input=javascript_path.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    relative_path = javascript_path.relative_to(PROJECT_ROOT).as_posix()
    assert result.returncode == 0, f"{relative_path} 不是有效的 ES Module:\n{result.stderr}"
