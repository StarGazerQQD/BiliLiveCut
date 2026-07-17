"""Release fixture isolation tests."""

from __future__ import annotations

from pathlib import Path

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
_PROJ_ROOT = _PORTABLE_DIR.parent.parent


def test_release_workflow_rejects_fixture_build() -> None:
    """Build Lite EXE 步骤不能使用 BLC_FIXTURE_BUILD 绕过校验。Fixture Engine Pack 构建步骤除外。"""
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    lines = content.split("\n")
    in_lite_build = False
    for i, line in enumerate(lines):
        if "Build Lite EXE" in line and "name:" in line:
            in_lite_build = True
        if in_lite_build and "name:" in line and "Build Lite EXE" not in line and "Upload" not in line:
            in_lite_build = False
        if in_lite_build and "BLC_FIXTURE_BUILD" in line:
            raise AssertionError("Build Lite EXE step must not use BLC_FIXTURE_BUILD")


def test_release_job_requires_production_metadata() -> None:
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "engine_pack_info.json" in content
