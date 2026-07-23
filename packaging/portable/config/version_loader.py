"""BiliLiveCut 统一版本加载器 — 所有版本号的唯一权威来源。

用法:
    from blc_portable.config.version_loader import get_version, RELEASE_VERSION
    print(RELEASE_VERSION)  # "0.1.15.2-alpha"

其他模块不得再硬编码版本号，必须通过此模块获取。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_VERSION_PATH = Path(__file__).resolve().parent / "version.json"
_cache: dict[str, str] | None = None


def _load_version_config() -> dict:
    """加载版本配置（带缓存）。

    :returns: 版本配置字典。
    :raises FileNotFoundError: 配置文件不存在时。
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = json.loads(_VERSION_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法加载版本配置 {_VERSION_PATH}: {exc}") from exc
    return _cache


def get_version() -> str:
    """获取发布版本号。

    :returns: 如 "0.1.15.2-alpha"
    """
    return _load_version_config()["release_version"]


def get_version_label() -> str:
    """获取版本显示标签。

    :returns: 如 "V0.1.15.2 Alpha"
    """
    return _load_version_config()["version_label"]


def get_source_commit_short() -> str:
    """获取业务源码基线短 Hash。

    :returns: 如 "f2c291d"
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


def get_compatible_app_min() -> str:
    """获取兼容的最低 App 版本。

    :returns: 版本字符串。
    """
    return _load_version_config()["compatible_app"]["min"]


def get_compatible_app_max_exclusive() -> str:
    """获取兼容 App 版本上界（不包含）。

    :returns: 版本字符串。
    """
    return _load_version_config()["compatible_app"]["max_exclusive"]


def get_lite_exe_name() -> str:
    """获取 Lite EXE 文件名模板。

    :returns: 如 "BiliLiveCut-Portable-Lite-v0.1.15.2-alpha-x64.exe"
    """
    template = _load_version_config()["naming"]["lite_exe"]
    return template.format(version=_load_version_config()["release_version"])


def get_full_zip_name() -> str:
    """获取 Full ZIP 文件名模板。

    :returns: 如 "BiliLiveCut-Portable-Full-0.1.15.2-alpha-x64.zip"
    """
    template = _load_version_config()["naming"]["full_zip"]
    return template.format(version=_load_version_config()["release_version"])


def get_engine_pack_zip_name() -> str:
    """获取 Engine Pack ZIP 文件名模板。

    :returns: 如 "BiliLiveCut-EnginePack-0.1.15.2-alpha.zip"
    """
    template = _load_version_config()["naming"]["engine_pack_zip"]
    return template.format(version=_load_version_config()["release_version"])


def get_payload_zip_name() -> str:
    """获取 Payload ZIP 文件名。

    :returns: "source_payload.zip"
    """
    return _load_version_config()["naming"]["payload_zip"]


def get_compatible_python_versions() -> tuple[str, str]:
    """获取兼容的 Python 版本范围。

    :returns: (min_version, max_validated_version)
    """
    cfg = _load_version_config()["compatible_python"]
    return cfg["min"], cfg["max_validated"]


def get_full_config() -> dict:
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
