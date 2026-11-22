"""读取 Portable 同步版本、冻结源码身份与构建契约。

构建时从 tag 和 HEAD 解析，冻结启动器读取内嵌的版本清单。
"""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
from pathlib import Path
from typing import Any

_VERSION_PATH = Path(__file__).resolve().parent / "version.json"
_VERSION_FIELDS = {
    "release_version",
    "version_label",
    "source_commit_short",
    "source_commit_full",
    "engine_pack_version",
    "runtime_schema",
    "core_schema",
    "engine_pack_schema",
    "payload_schema",
    "model_lock_schema",
    "python_abis",
    "target_platforms",
    "target_architectures",
    "naming",
}
_NAMING_FIELDS = {"lite_exe", "full_zip", "engine_pack_zip", "payload_zip"}
_CURRENT_SCHEMAS = {
    "runtime_schema": 5,
    "core_schema": 6,
    "engine_pack_schema": 5,
    "payload_schema": 7,
    "model_lock_schema": 5,
}
_CURRENT_NAMING = {
    "lite_exe": "BiliLiveCut-Portable-Lite-v{version}-x64.exe",
    "full_zip": "BiliLiveCut-Portable-Full-{version}-x64.zip",
    "engine_pack_zip": "BiliLiveCut-EnginePack-{version}.zip",
    "payload_zip": "source_payload.zip",
}
_cache: dict[str, Any] | None = None


TAG_PATTERN = re.compile(r"v[0-9]+(?:\.[0-9]+){2,3}(?:-(?:Alpha|Beta|RC)(?:\.[0-9]+)?)?")


def checkout_identity(root: Path, tag: str | None) -> dict[str, str]:
    """Resolve the release tag to the checked-out commit, including annotated tags."""

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

    commit = git("rev-parse", "HEAD^{commit}")
    if tag is None:
        tags = [name for name in git("tag", "--points-at", commit).splitlines() if TAG_PATTERN.fullmatch(name)]
        if len(tags) > 1:
            raise ValueError("Multiple release tags at HEAD; select one with BLC_RELEASE_TAG")
        tag = next(iter(tags), None)
    if tag is not None:
        if not TAG_PATTERN.fullmatch(tag):
            raise ValueError(f"Invalid release tag: {tag!r}")
        if git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") != commit:
            raise ValueError(f"Release tag {tag!r} does not point to HEAD")
        version = tag[1:].lower()
    else:
        # Untagged CI builds exercise packaging without claiming a published tag.
        version = runpy.run_path(str(root / "app/_source_identity.py"))["RELEASE_VERSION"]
    return {
        "release_version": version,
        "version_label": f"{tag or 'dev'} ({commit[:8]})",
        "source_commit_full": commit,
        "source_commit_short": commit[:7],
        "engine_pack_version": version,
    }


def stamp_distribution(directory: Path) -> None:
    """Embed reproducible metadata in a disposable source distribution or Payload staging tree."""
    if directory.resolve() == _VERSION_PATH.parents[3]:
        raise ValueError("Release metadata must be written to a staging tree")
    cfg = get_full_config()
    version = cfg["release_version"]
    module = directory / "app" / "_source_identity.py"
    module.write_text(
        f"RELEASE_VERSION = {version!r}\nVERSION_LABEL = {cfg['version_label']!r}\n"
        f"SOURCE_COMMIT = {cfg['source_commit_full']!r}\n",
        encoding="utf-8",
        newline="\n",
    )


def _load_version_config() -> dict[str, Any]:
    """加载并严格校验当前版本配置（带缓存）。

    :returns: 版本配置字典。
    :raises FileNotFoundError: 配置文件不存在时。
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(_VERSION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载版本配置 {_VERSION_PATH}: {exc}") from exc
    if _VERSION_PATH.parts[-4:] == ("packaging", "portable", "config", "version.json"):
        identity = checkout_identity(_VERSION_PATH.parents[3], os.environ.get("BLC_RELEASE_TAG"))
        if not isinstance(raw, dict) or set(raw) != _VERSION_FIELDS - identity.keys():
            raise RuntimeError("The checkout version config must contain only packaging contracts")
        raw.update(identity)
    if not isinstance(raw, dict) or set(raw) != _VERSION_FIELDS:
        raise RuntimeError("version.json 字段不符合当前格式")
    for field_name in (
        "release_version",
        "version_label",
        "source_commit_short",
        "source_commit_full",
        "engine_pack_version",
    ):
        if not isinstance(raw[field_name], str) or not raw[field_name]:
            raise RuntimeError(f"version.json {field_name} 必须是非空字符串")
    source_full = raw["source_commit_full"]
    source_short = raw["source_commit_short"]
    if re.fullmatch(r"[0-9a-f]{40}", source_full) is None or source_short != source_full[:7]:
        raise RuntimeError("version.json source_commit_short/source_commit_full 无效")
    if raw["engine_pack_version"] != raw["release_version"]:
        raise RuntimeError("version.json engine_pack_version 必须等于 release_version")
    for field_name, expected in _CURRENT_SCHEMAS.items():
        value = raw[field_name]
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            raise RuntimeError(f"version.json {field_name} 必须是当前值 {expected}")
    if raw["python_abis"] != ["cp311", "cp312"]:
        raise RuntimeError("version.json python_abis 必须精确为 cp311/cp312")
    if raw["target_platforms"] != ["windows"] or raw["target_architectures"] != ["x64"]:
        raise RuntimeError("version.json 目标平台必须精确为 windows/x64")
    naming = raw["naming"]
    if not isinstance(naming, dict) or set(naming) != _NAMING_FIELDS or naming != _CURRENT_NAMING:
        raise RuntimeError("version.json naming 不符合当前格式")
    _cache = raw
    return _cache


def get_version() -> str:
    """获取发布版本号。

    :returns: 当前发行版本字符串。
    """
    return _load_version_config()["release_version"]


def get_version_label() -> str:
    """获取版本显示标签。

    :returns: 当前发行版本的显示标签。
    """
    return _load_version_config()["version_label"]


def get_source_commit_short() -> str:
    """获取业务源码基线短 Hash。

    :returns: 当前业务源码冻结提交的短 Hash。
    """
    return _load_version_config()["source_commit_short"]


def get_source_commit_full() -> str:
    """获取业务源码基线完整 Hash。

    :returns: 完整 commit hash。
    """
    return _load_version_config()["source_commit_full"]


def get_engine_pack_version() -> str:
    """获取 Engine Pack 版本。

    :returns: Engine Pack 版本字符串。
    """
    return _load_version_config()["engine_pack_version"]


def get_lite_exe_name() -> str:
    """获取 Lite EXE 文件名模板。

    :returns: 代入当前版本号后的 Lite EXE 文件名。
    """
    template = _load_version_config()["naming"]["lite_exe"]
    return template.format(version=_load_version_config()["release_version"])


def get_full_zip_name() -> str:
    """获取 Full ZIP 文件名模板。

    :returns: 代入当前版本号后的 Full ZIP 文件名。
    """
    template = _load_version_config()["naming"]["full_zip"]
    return template.format(version=_load_version_config()["release_version"])


def get_engine_pack_zip_name() -> str:
    """获取 Engine Pack ZIP 文件名模板。

    :returns: 代入当前版本号后的 Engine Pack ZIP 文件名。
    """
    template = _load_version_config()["naming"]["engine_pack_zip"]
    return template.format(version=_load_version_config()["release_version"])


def get_payload_zip_name() -> str:
    """获取 Payload ZIP 文件名。

    :returns: "source_payload.zip"
    """
    return _load_version_config()["naming"]["payload_zip"]


def get_python_abis() -> tuple[str, ...]:
    """获取当前发行实际构建的 Python ABI。

    :returns: ABI 标识元组。
    """
    return tuple(_load_version_config()["python_abis"])


def get_full_config() -> dict[str, Any]:
    """获取完整版本配置字典（用于工具脚本）。

    :returns: 完整配置字典。
    """
    return _load_version_config()


# 便捷常量 — 供直接 import 使用
RELEASE_VERSION = get_version()
VERSION_LABEL = get_version_label()
SOURCE_COMMIT_SHORT = get_source_commit_short()
SOURCE_COMMIT_FULL = get_source_commit_full()
ENGINE_PACK_VERSION = get_engine_pack_version()

# 确保环境变量也可用
os.environ.setdefault("BLC_VERSION", RELEASE_VERSION)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Resolve the release tag and optionally stamp a staging tree")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--stamp", type=Path)
    operations.add_argument("--docker-context", type=Path, help="将当前提交及发行身份导出到空目录，供 Docker 构建")
    args = parser.parse_args()
    if args.docker_context is not None:
        import sys

        sys.path.insert(0, str(_VERSION_PATH.parents[1] / "src"))
        from blc_portable.payload.source_snapshot import extract_source

        destination = args.docker_context.resolve()
        extract_source(SOURCE_COMMIT_FULL, destination)
        stamp_distribution(destination)
    if args.stamp is not None:
        stamp_distribution(args.stamp)
    print(VERSION_LABEL, SOURCE_COMMIT_FULL)
