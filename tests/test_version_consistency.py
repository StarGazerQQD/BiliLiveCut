"""Behavioral checks of release identity across Git, packaging and Git-free runtimes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "packaging/portable/config"
sys.path.insert(0, str(CONFIG))
from version_loader import checkout_identity  # noqa: E402


@pytest.mark.parametrize("annotated", [False, True])
def test_build_identity_follows_tag_and_is_readable_without_git(tmp_path: Path, annotated: bool) -> None:
    checkout = tmp_path / "repo"
    config = checkout / "packaging/portable/config"
    config.mkdir(parents=True)
    for name in ("version_loader.py", "version.json"):
        shutil.copy2(CONFIG / name, config / name)
    (checkout / "app").mkdir()
    for name in ("__init__.py", "_build_identity.py"):
        shutil.copy2(ROOT / "app" / name, checkout / "app" / name)

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=checkout, text=True).strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "source")
    commit = git("rev-parse", "HEAD")
    tag = "v1.2.3.4-RC.2"
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.org",
        "tag",
        *(["-am", "release"] if annotated else []),
        tag,
    )
    assert checkout_identity(checkout, None) == checkout_identity(checkout, tag)

    stage = tmp_path / "distribution"
    shutil.copytree(checkout / "app", stage / "app")
    env = dict(os.environ, BLC_RELEASE_TAG=tag)
    subprocess.run([sys.executable, str(config / "version_loader.py"), "--stamp", str(stage)], env=env, check=True)
    cfg = checkout_identity(checkout, tag)
    frozen = tmp_path / "bundle"
    frozen.mkdir()
    shutil.copy2(CONFIG / "version_loader.py", frozen)
    (frozen / "version.json").write_text(json.dumps(json.loads((CONFIG / "version.json").read_text()) | cfg))
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import app, version_loader as v; print(app.__version__, app.__version_label__, v.SOURCE_COMMIT_FULL)",
        ],
        cwd=stage,
        env=dict(env, PYTHONPATH=str(frozen), PATH=""),
        text=True,
    )
    assert output.strip() == f"1.2.3.4-rc.2 {tag} ({commit[:8]}) {commit}"
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "--allow-empty", "-qm", "another commit")
    with pytest.raises(ValueError, match="does not point to HEAD"):
        checkout_identity(checkout, tag)
    with pytest.raises(ValueError, match="Invalid release tag"):
        checkout_identity(checkout, "main")
