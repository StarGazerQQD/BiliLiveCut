"""Release fixture isolation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
_PROJ_ROOT = _PORTABLE_DIR.parent.parent


def test_release_workflow_rejects_fixture_build() -> None:
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    lines = content.split("\n")
    in_env = False
    for line in lines:
        if "env:" in line and "BLC_" not in line:
            in_env = True
            continue
        if line.strip() and not line.strip().startswith("#"):
            if not line.startswith(" "):
                in_env = False
        if in_env and "BLC_FIXTURE_BUILD" in line:
            assert False, "release.yml must not set BLC_FIXTURE_BUILD"


def test_release_job_requires_production_metadata() -> None:
    release_yml = _PROJ_ROOT / ".github" / "workflows" / "release.yml"
    content = release_yml.read_text(encoding="utf-8")
    assert "engine_pack_info.json" in content
