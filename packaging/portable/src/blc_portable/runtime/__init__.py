"""Runtime 路径管理 — 统一的路径计算模块。

Launcher、测试、verifier 必须通过此模块获取路径，不得各自硬编码。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────
APP_NAME = "BiliLiveCut"
VERSION = "V0.1.14.11 Alpha"
RELEASE_VERSION = "0.1.14.11-alpha"
SOURCE_COMMIT_SHORT = "731a31c"


def get_app_root() -> Path:
    """获取 Portable 应用根目录。

    :returns: 根目录路径。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def get_runtime_dir() -> Path:
    """获取 runtime 目录。

    :returns: runtime 目录路径。
    """
    return get_app_root() / "runtime"


def get_releases_dir() -> Path:
    """获取 releases 目录。

    :returns: releases 目录。
    """
    return get_runtime_dir() / "releases"


def get_current_json_path() -> Path:
    """获取 current.json 路径。

    :returns: current.json 路径。
    """
    return get_runtime_dir() / "current.json"


def get_current_release_dir() -> Path | None:
    """获取当前激活的 Release 目录，比较嵌入式 identity 与 installed identity。

    使用内容寻址: {version}+{commit}+{payload_hash_prefix}
    任何不一致都返回 None 以触发重新安装。

    :returns: Release 目录，不存在返回 None。
    """
    current_path = get_current_json_path()
    if not current_path.exists():
        return None
    from .activation import read_current_json

    info = read_current_json(app_root=get_app_root())
    if info is None:
        return None
    rid = info.get("release_id", "")
    if not rid:
        return None
    d = get_releases_dir() / rid
    if not d.exists() or not (d / "app" / "cli.py").exists():
        return None

    # Compare embedded payload identity with installed identity
    try:
        embedded_manifest_path = _find_embedded_manifest()
        if embedded_manifest_path:
            embedded = json.loads(embedded_manifest_path.read_text(encoding="utf-8"))
            embedded_sha = embedded.get("payload_sha256", "")
            installed_sha = info.get("payload_sha256", "")
            if embedded_sha and installed_sha and embedded_sha != installed_sha:
                return None
    except (json.JSONDecodeError, OSError):
        pass

    return d


def _find_embedded_manifest() -> Path | None:
    """查找嵌入式 Payload Manifest，兼容 PyInstaller 和源码运行。"""
    import sys as _sys

    if getattr(_sys, "frozen", False):
        base = Path(getattr(_sys, "_MEIPASS", ""))
    else:
        base = Path(__file__).resolve().parent.parent.parent.parent / "dist" / "payload"
    p = base / "payload_manifest.json"
    return p if p.exists() else None


def get_staging_dir() -> Path:
    """获取 Runtime staging 目录。

    :returns: staging 目录路径。
    """
    return get_runtime_dir() / "staging"
