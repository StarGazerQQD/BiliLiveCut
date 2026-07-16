"""Runtime 激活管理 — current.json 的原子读写和切换。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def write_current_json(
    app_root: Path,
    release_id: str,
    release_version: str,
    source_commit: str,
    source_commit_short: str,
    builder_commit: str,
    payload_sha256: str,
    manifest_sha256: str,
) -> None:
    """原子写入 current.json（先 .tmp 再 os.replace）。"""
    from . import get_runtime_dir

    current_info: dict[str, Any] = {
        "runtime_schema": 3,
        "release_id": release_id,
        "release_version": release_version,
        "source_commit": source_commit,
        "source_commit_short": source_commit_short,
        "builder_commit": builder_commit,
        "payload_sha256": payload_sha256,
        "manifest_sha256": manifest_sha256,
        "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "platform": sys.platform,
        "architecture": "x64" if sys.maxsize > 2**32 else "x86",
        "activated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    tmp = get_runtime_dir() / "current.json.tmp"
    target = get_runtime_dir() / "current.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(current_info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(target))


def read_current_json(app_root: Path) -> dict[str, Any] | None:
    """安全读取 current.json。"""
    from . import get_runtime_dir

    p = get_runtime_dir() / "current.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_current_json(app_root: Path) -> None:
    """删除 current.json（触发重新安装）。"""
    from . import get_runtime_dir

    p = get_runtime_dir() / "current.json"
    if p.exists():
        p.unlink(missing_ok=True)
