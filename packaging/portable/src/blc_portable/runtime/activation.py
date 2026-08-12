"""Runtime 激活管理 — current.json 的原子读写和切换。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from blc_portable.atomic_fs import replace_with_retry

RUNTIME_SCHEMA_VERSION = 5


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
    """原子写入 current.json（先 .tmp 再有限重试替换）。"""
    from . import get_runtime_dir

    current_info: dict[str, Any] = {
        "runtime_schema": RUNTIME_SCHEMA_VERSION,
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
    tmp = get_runtime_dir(app_root) / "current.json.tmp"
    target = get_runtime_dir(app_root) / "current.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(current_info, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(tmp, target)


def read_current_json(app_root: Path) -> dict[str, Any] | None:
    """读取当前格式的 current.json；旧格式视为未安装。"""
    from . import get_runtime_dir

    p = get_runtime_dir(app_root) / "current.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    required = {
        "runtime_schema",
        "release_id",
        "release_version",
        "source_commit",
        "source_commit_short",
        "builder_commit",
        "payload_sha256",
        "manifest_sha256",
        "python_abi",
        "platform",
        "architecture",
        "activated_at",
    }
    if not isinstance(data, dict) or set(data) != required or data["runtime_schema"] != RUNTIME_SCHEMA_VERSION:
        return None
    for field_name in required - {"runtime_schema"}:
        if not isinstance(data[field_name], str) or not data[field_name]:
            return None
    if re.fullmatch(r"[0-9a-f]{40}", data["source_commit"]) is None:
        return None
    if data["source_commit_short"] != data["source_commit"][:7]:
        return None
    if re.fullmatch(r"[0-9a-f]{40}", data["builder_commit"]) is None:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", data["payload_sha256"]) is None:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", data["manifest_sha256"]) is None:
        return None
    return data


def delete_current_json(app_root: Path) -> None:
    """删除 current.json（触发重新安装）。"""
    from . import get_runtime_dir

    p = get_runtime_dir(app_root) / "current.json"
    if p.exists():
        p.unlink(missing_ok=True)
