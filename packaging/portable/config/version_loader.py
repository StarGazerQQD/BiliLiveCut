"""BiliLiveCut 统一版本加载器 — 所有版本号的唯一权威来源。

用法:
    from blc_portable.config.version_loader import get_version, RELEASE_VERSION
    print(RELEASE_VERSION)  # "0.1.17.4-alpha"

其他模块不得再硬编码版本号，必须通过此模块获取。
"""

from __future__ import annotations

import json
import os
import re
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

    :returns: 如 "0.1.17.4-alpha"
    """
    return _load_version_config()["release_version"]


def get_version_label() -> str:
    """获取版本显示标签。

    :returns: 如 "V0.1.17.4 Alpha"
    """
    return _load_version_config()["version_label"]


def get_source_commit_short() -> str:
    """获取业务源码基线短 Hash。

    :returns: 如 "97e39df"
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

    :returns: 如 "BiliLiveCut-Portable-Lite-v0.1.17.4-alpha-x64.exe"
    """
    template = _load_version_config()["naming"]["lite_exe"]
    return template.format(version=_load_version_config()["release_version"])


def get_full_zip_name() -> str:
    """获取 Full ZIP 文件名模板。

    :returns: 如 "BiliLiveCut-Portable-Full-0.1.17.4-alpha-x64.zip"
    """
    template = _load_version_config()["naming"]["full_zip"]
    return template.format(version=_load_version_config()["release_version"])


def get_engine_pack_zip_name() -> str:
    """获取 Engine Pack ZIP 文件名模板。

    :returns: 如 "BiliLiveCut-EnginePack-0.1.17.4-alpha.zip"
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
