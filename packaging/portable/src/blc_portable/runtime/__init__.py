"""Runtime 路径管理 — 统一的路径计算模块。

Launcher、测试、verifier 必须通过此模块获取路径，不得各自硬编码。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.18.1 Alpha"
RELEASE_VERSION = "0.1.18.1-alpha"
SOURCE_COMMIT_SHORT = "c435147"


def get_app_root() -> Path:
    """获取 Portable 应用根目录。

    :returns: 根目录路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def get_runtime_dir(app_root: Path | None = None) -> Path:
    """获取 runtime 目录。

    :returns: runtime 目录路径。
    """
    return (app_root if app_root is not None else get_app_root()) / "runtime"


def get_releases_dir(app_root: Path | None = None) -> Path:
    """获取 releases 目录。

    :returns: releases 目录。
    """
    return get_runtime_dir(app_root) / "releases"


def get_current_json_path(app_root: Path | None = None) -> Path:
    """获取 current.json 路径。

    :returns: current.json 路径。
    """
    return get_runtime_dir(app_root) / "current.json"


def get_current_release_dir(app_root: Path | None = None) -> Path | None:
    """获取当前激活的 Release 目录，比较嵌入式 identity 与 installed identity。

    使用内容寻址: {version}+{commit}+{payload_hash_prefix}
    任何不一致都返回 None 以触发重新安装。

    :returns: Release 目录，不存在返回 None。
    """
    root = app_root if app_root is not None else get_app_root()
    current_path = get_current_json_path(root)
    if not current_path.exists():
        return None
    from .activation import read_current_json

    info = read_current_json(app_root=root)
    if info is None:
        return None
    rid = info["release_id"]
    if not rid:
        return None
    d = get_releases_dir(root) / rid
    if not d.exists() or not (d / "app" / "cli.py").exists():
        return None

    # Compare embedded payload identity with installed identity — fail-closed
    # Any mismatch, missing manifest, empty hash, or parse error → reinstall
    embedded_manifest_path = _find_embedded_manifest()
    if embedded_manifest_path is None:
        return None
    try:
        embedded = json.loads(embedded_manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    from blc_portable.payload.manifest import MANIFEST_FORMAT_VERSION, validate_manifest_schema

    if validate_manifest_schema(embedded) or embedded["format_version"] != MANIFEST_FORMAT_VERSION:
        return None
    embedded_sha = embedded["payload_sha256"]
    installed_sha = info["payload_sha256"]
    if not embedded_sha or not installed_sha:
        return None
    if embedded_sha != installed_sha:
        return None

    return d


def _find_embedded_manifest() -> Path | None:
    """按当前运行形态查找嵌入式 Payload Manifest。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        base = Path(getattr(_sys, "_MEIPASS", ""))
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist" / "payload"
    p = base / "payload_manifest.json"
    return p if p.exists() else None


def get_staging_dir(app_root: Path | None = None) -> Path:
    """获取 Runtime staging 目录。

    :returns: staging 目录路径。
    """
    return get_runtime_dir(app_root) / "staging"
