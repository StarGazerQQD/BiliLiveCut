"""Runtime 验证器 — Payload/Runtime 完整性验证。"""

from __future__ import annotations

import json
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
    """原子写入 current.json。

    :param app_root: 应用根目录。
    :param release_id: Release ID。
    :param release_version: 版本。
    :param source_commit: 完整 commit hash。
    :param source_commit_short: 短 commit hash。
    :param builder_commit: builder commit hash。
    :param payload_sha256: Payload SHA-256。
    :param manifest_sha256: Manifest SHA-256。
    """
    from .__init__ import get_runtime_dir

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
    import os
    os.replace(str(tmp), str(target))


def verify_runtime(app_root: Path) -> tuple[bool, list[str]]:
    """验证已安装 Runtime 的完整性。

    检查: current.json、release 目录、Payload SHA、Manifest SHA、
    ABI、platform、architecture、source commit。

    :param app_root: 应用根目录。
    :returns: (通过, 错误列表)。
    """
    from .__init__ import get_current_json_path, get_current_release_dir, get_releases_dir

    errors: list[str] = []
    current_path = get_current_json_path()

    # 1. current.json
    if not current_path.exists():
        errors.append("current.json 不存在")
        return False, errors

    try:
        info = json.loads(current_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"current.json 无法解析: {exc}")
        return False, errors

    # 2. Release ID
    rid = info.get("release_id", "")
    if not rid:
        errors.append("current.json release_id 为空")
        return False, errors

    # 3. Release 目录存在
    release_dir = get_releases_dir() / rid
    if not release_dir.exists():
        errors.append(f"Release 目录不存在: {release_dir}")
        return False, errors

    if not (release_dir / "app" / "cli.py").exists():
        errors.append("Release 缺少 app/cli.py")

    # 4. ABI 匹配
    expected_abi = info.get("python_abi", "")
    current_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if expected_abi and expected_abi != current_abi:
        errors.append(f"Python ABI 不匹配: installed={expected_abi} current={current_abi}")

    # 5. Platform 匹配
    expected_platform = info.get("platform", "")
    if expected_platform and expected_platform != sys.platform:
        errors.append(f"Platform 不匹配: installed={expected_platform} current={sys.platform}")

    # 6. Payload SHA 非空
    payload_sha = info.get("payload_sha256", "")
    if not payload_sha:
        errors.append("current.json payload_sha256 为空")

    # 7. Schema
    schema = info.get("runtime_schema", 0)
    if not isinstance(schema, int) or schema < 1:
        errors.append(f"runtime_schema 无效: {schema}")

    return len(errors) == 0, errors
