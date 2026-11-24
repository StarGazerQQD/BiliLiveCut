"""Check release identity and the independently versioned Rust package lock."""

from __future__ import annotations

import runpy
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging/portable/config"))
from version_loader import get_full_config  # noqa: E402


def main() -> int:
    """Validate the package version source, selected release and native lockfile."""
    cfg = get_full_config()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if project["tool"]["setuptools"]["dynamic"]["version"]["attr"] != "app._packaging_identity.RELEASE_VERSION":
        raise ValueError("Python package version must use the build identity")
    package = runpy.run_path(str(ROOT / "app/_packaging_identity.py"))
    if (package["RELEASE_VERSION"], package["SOURCE_COMMIT"]) != (cfg["release_version"], cfg["source_commit_full"]):
        raise ValueError("Python package version differs from the selected release")
    rust = ROOT / "tools/native/rust"
    crate = tomllib.loads((rust / "Cargo.toml").read_text(encoding="utf-8"))["package"]
    lock = tomllib.loads((rust / "Cargo.lock").read_text(encoding="utf-8"))
    if not any(p["name"] == crate["name"] and p["version"] == crate["version"] for p in lock["package"]):
        raise ValueError("Rust crate version differs from Cargo.lock")
    print(cfg["version_label"], cfg["source_commit_full"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
