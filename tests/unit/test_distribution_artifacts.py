"""Source and wheel distribution contracts."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_web_extra_contains_all_import_time_dependencies() -> None:
    """Installing ``.[web]`` must be sufficient to import the FastAPI app."""
    web = _pyproject()["project"]["optional-dependencies"]["web"]
    names = {requirement.split(">=", 1)[0].lower() for requirement in web}
    assert {"fastapi", "uvicorn[standard]", "jinja2", "python-multipart"} <= names


def test_dev_extra_executes_optional_image_paths() -> None:
    """The dev environment must execute optional image paths instead of skipping them."""
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    names = {requirement.split(">=", 1)[0].lower() for requirement in dev}
    assert "pillow" in names


def test_wheel_declares_web_assets_as_package_data() -> None:
    """Web and scoring assets are mandatory runtime wheel content."""
    setuptools = _pyproject()["tool"]["setuptools"]
    patterns = set(setuptools["package-data"]["app.web"])
    assert {
        "templates/*.html",
        "static/*.css",
        "static/*.js",
        "static/js/*.js",
        "static/js/*.txt",
    } <= patterns
    assert "config" in setuptools["packages"]["find"]["include"]
    assert {"*.yaml", "*.txt"} <= set(setuptools["package-data"]["config"])


def test_sdist_manifest_contains_runtime_and_audit_inputs() -> None:
    """The source archive must contain Web assets, config, tests and release tooling."""
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for rule in (
        "recursive-include app *.py *.html *.css *.js *.txt",
        "recursive-include config *.yaml *.txt",
        "recursive-include packaging *.py *.json *.yaml *.ini *.lock *.in *.spec *.md *.example",
        "recursive-include packaging/portable/licenses *.txt *.md",
        "recursive-include scripts",
        "recursive-include tests",
        "recursive-include tools",
        "include packaging/docker/Dockerfile",
        "exclude tools/native/cython/_speedups_round2.c",
    ):
        assert rule in manifest
    for prune in (
        "prune packaging/portable/.model_cache",
        "prune packaging/portable/build",
        "prune packaging/portable/dist",
        "prune tools/native/rust/target",
    ):
        assert prune in manifest

    release_workflow = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert ".cursor/rules/*" in release_workflow


def test_local_runtime_and_tool_outputs_are_ignored() -> None:
    """本地 Runtime、覆盖率报告和 IDE 工作区不得进入提交范围。"""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in ("coverage.xml", "/runtime/", "*.code-workspace"):
        assert pattern in gitignore


def test_readme_distinguishes_project_code_from_third_party_licenses() -> None:
    """第三方模型许可证不得被误述为项目代码许可证。"""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    portable_readme = (REPO_ROOT / "packaging" / "portable" / "README.md").read_text(encoding="utf-8")

    for content in (readme, portable_readme):
        assert "不构成 BiliLiveCut 项目代码的开源许可" in content
        assert "未声明" in content and "独立开源许可证" in content
