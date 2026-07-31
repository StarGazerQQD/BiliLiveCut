"""前端静态 JavaScript 的语法回归测试。"""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = PROJECT_ROOT / "app" / "web" / "static"
JAVASCRIPT_FILES = tuple(sorted(STATIC_ROOT.rglob("*.js")))
TEMPLATE_FILES = tuple(sorted((PROJECT_ROOT / "app" / "web" / "templates").glob("*.html")))
INTERACTION_CHECK = PROJECT_ROOT / "scripts" / "check_frontend_interactions.mjs"


class _ElementIdCollector(HTMLParser):
    """收集 HTML 元素 ID，用于防止选择器命中错误元素。"""

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """记录当前开始标签的 ID。"""
        for name, value in attrs:
            if name == "id" and value is not None:
                self.ids.append(value)


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


@pytest.mark.parametrize(
    "template_path",
    TEMPLATE_FILES,
    ids=lambda path: path.relative_to(PROJECT_ROOT).as_posix(),
)
def test_html_template_element_ids_are_unique(template_path: Path) -> None:
    """同一页面重复 ID 会使交互代码更新到错误元素。"""
    parser = _ElementIdCollector()
    parser.feed(template_path.read_text(encoding="utf-8"))
    duplicates = sorted({element_id for element_id in parser.ids if parser.ids.count(element_id) > 1})

    assert duplicates == [], f"{template_path.name} 存在重复 ID: {duplicates}"


def test_recording_pipeline_has_visible_switch_and_no_hardcoded_web_override() -> None:
    """Web 录制应采用可见全局开关，不再把 Pipeline 强制写死为开启。"""
    template = (PROJECT_ROOT / "app" / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    recording_js = (STATIC_ROOT / "js" / "recording.js").read_text(encoding="utf-8")

    assert 'id="sw-recording-pipeline"' in template
    assert "RECORDING_PIPELINE_ENABLED" in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "pipeline: true" not in recording_js


def test_frontend_module_graph_and_tab_interaction() -> None:
    """真实加载全部 ES Module，并验证初始刷新、事件绑定和标签切换。"""
    node = shutil.which("node")
    assert node is not None, "前端交互检查需要 Node.js"

    result = subprocess.run(
        [node, str(INTERACTION_CHECK)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: frontend module graph" in result.stdout
